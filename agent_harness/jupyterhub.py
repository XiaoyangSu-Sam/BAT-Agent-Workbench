"""A session-owned kernel on an existing, running JupyterHub user server."""
import asyncio
import json
import logging
import re
import ssl
import struct
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from uuid import uuid4

import httpx


_hub_http_active = ContextVar("hub_http_active", default=False)

# HTTPX logs callback URLs at INFO. HTTP Core logs Set-Cookie headers at DEBUG.
# Filter only this task's Hub traffic and preserve ordinary model/API logging.
def _public_http_log(record):
    return not _hub_http_active.get()


for _name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection",
              "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_name).addFilter(_public_http_log)


class HubError(RuntimeError):
    """A transport error safe to show without server responses or credentials."""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def _check_path(path):
    # Reverse proxies and frameworks may decode a path at different stages.
    for _ in range(3):
        if ("\\" in path or "//" in path or re.search(r"[\x00-\x1f\x7f]", path)
                or any(part in {".", ".."} for part in path.split("/"))):
            raise HubError("Use an unambiguous JupyterHub URL or server-relative folder.")
        decoded = unquote(path)
        if decoded == path:
            return
        path = decoded
    raise HubError("The JupyterHub path is encoded too many times.")


def _xsrf(request):
    # The Hub and user server set different cookies with the same name.
    for part in request.headers.get("cookie", "").split(";"):
        if part.strip().startswith("_xsrf="):
            value = SimpleCookie(part.strip()).get("_xsrf")
            if value:
                return value.value
    return ""


def _decode_frame(raw):
    if isinstance(raw, bytes):
        # Default Jupyter wire protocol: big-endian offsets, then JSON and optional buffers.
        if len(raw) < 8:
            raise ValueError("Invalid binary frame")
        count, start = struct.unpack_from("!II", raw)
        if not count or start != 4 * (count + 1) or start > len(raw):
            raise ValueError("Invalid binary frame offsets")
        end = struct.unpack_from("!I", raw, 8)[0] if count > 1 else len(raw)
        if not start < end <= len(raw):
            raise ValueError("Invalid JSON frame length")
        raw = raw[start:end]
    return json.loads(raw)


class HubClient:
    def __init__(self, url, username="", password="", token=""):
        try:
            entry = urlsplit(url.strip())
            _check_path(entry.path)
            if (entry.scheme != "https" or not entry.hostname or entry.username is not None
                    or entry.password is not None or entry.query or entry.fragment
                    or "\\" in entry.netloc or re.search(r"[\x00-\x20\x7f]", entry.netloc)):
                raise ValueError
            self._origin_key = (entry.hostname, entry.port or 443)
        except (ValueError, AttributeError):
            raise HubError("Enter an HTTPS Hub URL without credentials, query or fragment.") from None
        if not token and not (username and password):
            raise HubError("Enter your JupyterHub username and password or an API token.")
        self._origin = "https://" + entry.netloc
        # A pasted JupyterLab page also identifies the Hub's deployment prefix.
        # Always discover the server belonging to the credentials below.
        prefix = re.sub(r"/(?:hub(?:/(?:home|login))?|user/[^/]+/(?:lab|tree)(?:/.*)?)$",
                        "", entry.path.rstrip("/"))
        self._hub_path = prefix + "/hub/"
        self._users_path = prefix + "/user/"
        self._server_path = ""
        self._username, self._password, self._token = username, password, token
        self._signed_in = False
        self._specs = []
        self._kernel_id = ""
        self._socket = None
        self._session_id = str(uuid4())
        self._executing = False
        self._close_lock = asyncio.Lock()
        self._tls = ssl.create_default_context()
        self._http = httpx.AsyncClient(verify=self._tls, timeout=20.0, follow_redirects=False)

    def _target(self, url):
        try:
            parsed = urlsplit(url)
            _check_path(parsed.path)
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or (parsed.hostname, parsed.port or 443) != self._origin_key
                    or not (parsed.path.startswith(self._hub_path)
                            or self._server_path and parsed.path.startswith(self._server_path))):
                raise ValueError
        except ValueError:
            raise HubError("JupyterHub redirected outside the selected Hub or user server. "
                           "For an external sign-in provider, use an API token.") from None
        return url

    def _request_for(self, method, url, **kwargs):
        request = self._http.build_request(method, self._target(url), **kwargs)
        request.headers["Referer"] = url.split("?", 1)[0]
        if self._token:
            request.headers["Authorization"] = "token " + self._token
        else:
            xsrf = _xsrf(request)
            if xsrf:
                request.headers["X-XSRFToken"] = xsrf
        return request

    async def _request(self, method, url, *, navigate=False, login=False, **kwargs):
        for _ in range(12):
            request = self._request_for(method, url, **kwargs)
            request.headers["Sec-Fetch-Mode"] = "navigate" if navigate else "cors"
            request.headers["Sec-Fetch-Site"] = "same-origin"
            log_scope = _hub_http_active.set(True)
            try:
                response = await self._http.send(request)
            except (httpx.HTTPError, ValueError, UnicodeError):
                raise HubError("Could not reach JupyterHub. Check the connection and trusted TLS certificate.") from None
            finally:
                _hub_http_active.reset(log_scope)
            if response.is_redirect and response.headers.get("location"):
                next_url = self._target(urljoin(url, response.headers["location"]))
                if method != "GET":
                    if not login or response.status_code not in {302, 303}:
                        raise HubError("JupyterHub redirected a write request. Its outcome is uncertain.")
                    method, kwargs, login = "GET", {}, False
                url = next_url
                continue
            if response.status_code in {401, 403} or login:
                raise HubError("JupyterHub rejected sign-in or access. Check credentials and server permissions. "
                               "For SSO or MFA, use an API token.", status=response.status_code)
            if not response.is_success:
                raise HubError(f"JupyterHub returned HTTP {response.status_code}.", status=response.status_code)
            return response
        raise HubError("JupyterHub sign-in exceeded its redirect limit. For SSO or MFA, use an API token.")

    async def _model(self, method, url, **kwargs):
        response = await self._request(method, url, **kwargs)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise HubError("Expected a Jupyter API response. Check the Hub URL or use an API token.") from None
        return data

    async def discover(self):
        if self._http.is_closed:
            raise HubError("This JupyterHub connection has closed.")
        hub = self._origin + self._hub_path
        if not self._token and not self._signed_in:
            # The home page issues a fresh authenticated Hub XSRF cookie without spawning a server.
            login_url = hub + "login?" + urlencode({"next": self._hub_path + "home"})
            await self._request("GET", login_url, navigate=True)
            form = {"username": self._username, "password": self._password}
            xsrf = _xsrf(self._request_for("GET", login_url))
            if xsrf:
                form["_xsrf"] = xsrf
            await self._request("POST", login_url, data=form, navigate=True, login=True)
            self._signed_in, self._password = True, ""
        user = await self._model("GET", hub + "api/user")
        name = user.get("name")
        if not isinstance(name, str) or not name or user.get("kind", "user") != "user":
            raise HubError("JupyterHub did not identify a user for these credentials.")
        if "server" not in user and "servers" not in user:
            raise HubError("Signed in, but JupyterHub did not expose your server details. "
                           "Check permissions to read and access your own server.")
        servers = user.get("servers") or {}
        default = servers.get("", {}) if isinstance(servers, dict) else {}
        if not isinstance(default, dict):
            raise HubError("JupyterHub returned an invalid user server.")
        server = default.get("url") or user.get("server")
        if not isinstance(server, str) or not server or default.get("pending") or default.get("ready") is False:
            raise HubError("Start your default server in JupyterLab, wait until it is running, then reconnect.")
        parsed = urlsplit(urljoin(self._origin + "/", server))
        expected = unquote(self._users_path) + name + "/"
        if (unquote(parsed.path.rstrip("/") + "/") != expected or "/" in name or parsed.query):
            raise HubError("JupyterHub returned a server outside the signed-in user's default server.")
        self._server_path = parsed.path.rstrip("/") + "/"
        self._target(parsed.geturl().split("?", 1)[0].rstrip("/") + "/")
        base = self._origin + self._server_path
        if not self._token:
            # Establish the user-server OAuth cookie on a navigable page before calling its API.
            await self._request("GET", base, navigate=True)
        model = await self._model("GET", base + "api/kernelspecs")
        specs = model.get("kernelspecs")
        self._specs = []
        if isinstance(specs, dict):
            for name, model in specs.items():
                spec = model.get("spec") if isinstance(model, dict) else None
                if isinstance(spec, dict) and str(spec.get("language", "")).lower() in {"python", "python3"}:
                    self._specs.append(name)
        self._specs.sort()
        if not self._specs:
            raise HubError("This Jupyter server did not return a Python kernel.")
        return list(self._specs)

    def _kernel_url(self):
        return self._origin + self._server_path + "api/kernels/" + self._kernel_id

    async def _open_socket(self):
        try:
            from tornado.httpclient import HTTPRequest
            from tornado.websocket import websocket_connect
        except ImportError:
            raise HubError("Remote kernels require tornado in the existing BAT Python environment.") from None
        url = self._kernel_url() + "/channels?session_id=" + self._session_id
        headers = self._request_for("GET", url).headers
        handshake = {key: headers[key] for key in ("Cookie", "Authorization", "X-XSRFToken") if key in headers}
        handshake.update({"Origin": self._origin, "Sec-Fetch-Mode": "websocket", "Sec-Fetch-Site": "same-origin"})
        request = HTTPRequest(url.replace("https:", "wss:", 1), headers=handshake, ssl_options=self._tls,
                              validate_cert=True, follow_redirects=False, connect_timeout=20, request_timeout=20)
        try:
            return await websocket_connect(request, max_message_size=16 * 1024 * 1024)
        except Exception:
            raise HubError("Could not open the secure Jupyter kernel channel. Check WebSocket access "
                           "and try an API token if cookie access is disabled.") from None

    async def start(self, kernel, folder):
        if self._kernel_id or self._http.is_closed:
            raise HubError("Start a new connection before selecting another Jupyter kernel.")
        if not self._specs:
            await self.discover()
        if kernel not in self._specs:
            raise HubError("Choose a kernel offered by this Jupyter server.")
        _check_path(folder)
        path = folder.strip("/")
        base = self._origin + self._server_path + "api/"
        contents = await self._model("GET", base + "contents/" + quote(path, safe="/") + "?content=0")
        if contents.get("type") != "directory" or contents.get("path") != path:
            raise HubError("Choose an existing directory relative to the Jupyter server root.")

        async def create():
            try:
                model = await self._model("POST", base + "kernels", json={"name": kernel, "path": path})
            except HubError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    raise
                raise HubError("Could not confirm kernel creation. Check running kernels in JupyterLab.") from None
            owned = model.get("id")
            if not isinstance(owned, str) or not re.fullmatch(r"[\w-]+", owned):
                raise HubError("The server did not identify the new kernel. Check running kernels in JupyterLab.")
            self._kernel_id = owned

        creation = asyncio.create_task(create())
        try:
            # If cancelled during POST, collect its result so we can delete the kernel we just created.
            await asyncio.shield(creation)
            self._socket = await self._open_socket()
        except BaseException:
            with suppress(Exception):
                await creation
            with suppress(HubError):
                await self.close()
            raise

    async def execute(self, code, timeout, max_output):
        if self._socket is None or self._executing:
            raise HubError("The Jupyter kernel is disconnected or busy.")
        self._executing = True
        message_id = str(uuid4())
        message = {
            "header": {"msg_id": message_id, "msg_type": "execute_request", "session": self._session_id,
                       "username": "bat", "date": datetime.now(timezone.utc).isoformat(), "version": "5.3"},
            "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
            "content": {"code": code, "silent": False, "store_history": True, "user_expressions": {},
                        "allow_stdin": False, "stop_on_error": True},
        }
        try:
            async with asyncio.timeout(timeout):
                await self._socket.write_message(json.dumps(message))
                return await _collect(self._socket, message_id, max_output)
        except asyncio.CancelledError:
            with suppress(HubError):
                await self.close()
            raise
        except Exception as exc:
            with suppress(HubError):
                await self.close()
            reason = "timed out" if isinstance(exc, TimeoutError) else "lost its connection or received an invalid message"
            raise HubError(f"Jupyter execution {reason}. Its outcome is uncertain. "
                           "Inspect remote changes before reconnecting.") from None
        finally:
            self._executing = False

    async def close(self):
        async with self._close_lock:
            if self._http.is_closed:
                return
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            try:
                if self._kernel_id:
                    try:
                        await self._request("DELETE", self._kernel_url())
                    except HubError as exc:
                        if exc.status != 404:
                            raise HubError("Could not confirm kernel shutdown. Check running kernels in JupyterLab.") from None
            finally:
                self._kernel_id = self._token = self._password = self._username = ""
                self._http.cookies.clear()
                await self._http.aclose()


async def _collect(socket, message_id, max_output):
    outputs = []
    remaining = min(max(0, max_output), 1_000_000)
    truncated = idle = False
    reply = None

    def take(text):
        nonlocal remaining, truncated
        text = str(text)
        truncated |= len(text) > remaining
        text = text[:remaining]
        remaining -= len(text)
        return text

    def add_error(content):
        outputs.append({"output_type": "error", "ename": take(content.get("ename", "Error")),
                        "evalue": take(content.get("evalue", "")),
                        "traceback": [take(line) for line in content.get("traceback", [])[:64] if remaining]})

    while reply is None or not idle:
        raw = await socket.read_message()
        if raw is None:
            raise ConnectionError
        msg = _decode_frame(raw)
        if msg.get("parent_header", {}).get("msg_id") != message_id:
            continue
        kind, content = msg["header"]["msg_type"], msg["content"]
        if msg["channel"] == "shell" and kind == "execute_reply":
            reply = content
            continue
        if msg["channel"] != "iopub":
            continue
        if kind == "status":
            idle |= content.get("execution_state") == "idle"
        elif kind in {"stream", "display_data", "execute_result", "error"}:
            if not remaining or len(outputs) >= 255:
                truncated = True
                continue
            if kind == "stream":
                name = "stderr" if content.get("name") == "stderr" else "stdout"
                outputs.append({"output_type": "stream", "name": name, "text": take(content["text"])})
            elif kind == "error":
                add_error(content)
            else:
                data = content.get("data", {})
                safe = {}
                if isinstance(data.get("text/plain"), str):
                    safe["text/plain"] = take(data["text/plain"])
                png = data.get("image/png")
                if isinstance(png, str) and png:
                    if len(png) <= remaining:
                        safe["image/png"] = take(png)
                    else:
                        truncated = True  # A partial base64 image is unusable.
                if safe:
                    item = {"output_type": kind, "data": safe, "metadata": {}}
                    if kind == "execute_result":
                        item["execution_count"] = content.get("execution_count")
                    outputs.append(item)
    ok = reply.get("status") == "ok"
    error = next((o for o in outputs if o["output_type"] == "error"), None)
    if not ok and error is None:
        add_error(reply)
        error = outputs[-1]
    result = {"ok": ok, "outputs": outputs, "execution_count": reply.get("execution_count"), "truncated": truncated}
    for name in ("stdout", "stderr"):
        result[name] = "".join(o["text"] for o in outputs if o.get("name") == name)
    if not ok:
        result["error"] = (error["ename"] + ": " + error["evalue"]).strip(": ") or "Jupyter execution failed."
    return result
