import asyncio
import json
import struct
from urllib.parse import parse_qs

import httpx
import pytest

from agent_harness import jupyterhub as hub


BASE = "https://hub.example/prefix/"
HUB = "/prefix/hub/"
SERVER = "/prefix/user/demo/"


class Service:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.paths = []
        self.folder = "project space"
        self.server = SERVER
        self.specs = {"python3": {"spec": {"language": "python"}}}
        self.form_login = False
        self.redirect = None

    def handle(self, request):
        path = request.url.path
        self.paths.append(path)
        cookie = request.headers.get("cookie", "")
        if self.redirect:
            return httpx.Response(302, headers={"location": self.redirect})
        if self.form_login:
            if path == HUB + "login" and request.method == "GET":
                return httpx.Response(200, text='<input name="_xsrf" value="login-xsrf">', headers={
                    "set-cookie": "_xsrf=login-xsrf; Path=/prefix/hub/; Secure",
                })
            if path == HUB + "login":
                assert parse_qs(request.content.decode()) == {
                    "username": ["demo"], "password": ["form-secret"], "_xsrf": ["login-xsrf"]}
                return httpx.Response(302, headers=[
                    ("location", HUB + "home"),
                    ("set-cookie", "hub-session=valid; Path=/prefix/hub/; Secure"),
                ])
            if path == HUB + "home":
                assert "hub-session=valid" in cookie
                return httpx.Response(200, headers={
                    "set-cookie": "_xsrf=hub-xsrf; Path=/prefix/hub/; Secure"})
            if path == SERVER and "server-session=valid" not in cookie:
                return httpx.Response(302, headers={"location": HUB + "api/oauth2/authorize?state=tmp"})
            if path == HUB + "api/oauth2/authorize":
                assert "hub-session=valid" in cookie
                return httpx.Response(302, headers={"location": SERVER + "oauth_callback?code=short-lived"})
            if path == SERVER + "oauth_callback":
                return httpx.Response(302, headers=[
                    ("location", SERVER),
                    ("set-cookie", "server-session=valid; Path=/prefix/user/demo/; Secure")])
            if path == SERVER:
                return httpx.Response(200, headers={
                    "set-cookie": "_xsrf=server-xsrf; Path=/prefix/user/demo/; Secure"})
            if path.startswith(SERVER):
                assert "server-session=valid" in cookie
                assert request.headers["x-xsrftoken"] == "server-xsrf"
            else:
                assert request.headers["x-xsrftoken"] == "hub-xsrf"
        else:
            assert request.headers["authorization"] == "token example-token"

        if path == HUB + "api/user":
            return httpx.Response(200, json={
                "name": "demo", "kind": "user", "server": self.server,
                "servers": {"": {"name": "", "ready": True, "url": self.server}} if self.server else {},
            })
        if path == SERVER + "api/kernelspecs":
            return httpx.Response(200, json={"default": "python3", "kernelspecs": self.specs})
        if path == SERVER + "api/contents/" + self.folder:
            return httpx.Response(200, json={"type": "directory", "path": self.folder})
        if path == SERVER + "api/kernels" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "owned-1", "name": "python3"})
        if path == SERVER + "api/kernels/owned-1" and request.method == "DELETE":
            self.deleted.append("owned-1")
            return httpx.Response(204)
        return httpx.Response(404, text="NEVER expose upstream bodies: form-secret/example-token")


class Channels:
    def __init__(self):
        self.messages = []
        self.writes = []
        self.closed = False
        self.wait = False

    async def write_message(self, raw):
        request = json.loads(raw)
        self.writes.append(request)
        msg_id = request["header"]["msg_id"]
        def frame(channel, kind, content, parent=msg_id):
            return json.dumps({"channel": channel, "header": {"msg_type": kind},
                               "parent_header": {"msg_id": parent}, "content": content})
        if self.wait:
            return
        self.messages = [
            frame("shell", "execute_reply", {"status": "error"}, "another-request"),
            frame("iopub", "stream", {"name": "stdout", "text": "ignore"}, "another-request"),
            frame("iopub", "stream", {"name": "stdout", "text": "hello"}),
        ]
        if request["content"]["code"] == "fail":
            error = {"ename": "ValueError", "evalue": "invalid", "traceback": ["ValueError: invalid"]}
            self.messages += [
                frame("shell", "execute_reply", {"status": "error", "execution_count": 7, **error}),
                frame("iopub", "error", error),
                frame("iopub", "status", {"execution_state": "idle"}),
            ]
        else:
            result = frame("iopub", "execute_result", {
                "execution_count": 7, "data": {"text/plain": "42", "text/html": "<script/>"}}).encode()
            self.messages += [
                struct.pack("!III", 2, 12, 12 + len(result)) + result + b"binary attachment",
                frame("iopub", "status", {"execution_state": "idle"}),
                frame("shell", "execute_reply", {"status": "ok", "execution_count": 7}),
            ]

    async def read_message(self):
        if self.wait:
            await asyncio.Event().wait()
        return self.messages.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def endpoint(monkeypatch):
    service, channels = Service(), Channels()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(service.handle), **kw))
    websocket = pytest.importorskip("tornado.websocket")
    async def connect(request, **kwargs):
        assert request.url.startswith("wss://hub.example/prefix/user/demo/api/kernels/owned-1/channels?")
        assert request.validate_cert is not False and request.follow_redirects is False
        if service.form_login:
            assert "server-session=valid" in request.headers["Cookie"]
            assert "hub-session" not in request.headers["Cookie"]
            assert request.headers["X-XSRFToken"] == "server-xsrf"
        else:
            assert request.headers["Authorization"] == "token example-token"
            assert "example-token" not in request.url
        return channels
    monkeypatch.setattr(websocket, "websocket_connect", connect)
    return service, channels


@pytest.mark.parametrize("use_form", [False, True])
def test_discovers_and_owns_kernel_in_verified_folder(endpoint, use_form):
    service, channels = endpoint
    service.form_login = use_form
    async def run():
        credentials = {"username": "demo", "password": "form-secret"} if use_form else {"token": "example-token"}
        client = hub.HubClient(BASE + "hub/", **credentials)
        assert await client.discover() == ["python3"]
        await client.start("python3", "/project space/")
        assert service.created == [{"name": "python3", "path": "project space"}]
        result = await client.execute("6*7", 2, 100)
        assert result["ok"] and result["execution_count"] == 7 and result["stdout"] == "hello"
        assert result["outputs"][-1]["data"] == {"text/plain": "42"}
        assert channels.writes[0]["content"]["allow_stdin"] is False
        result = await client.execute("fail", 2, 100)
        assert result["ok"] is False and result["outputs"][-1]["ename"] == "ValueError"
        result = await client.execute("6*7", 2, 3)
        assert result["truncated"] and result["stdout"] == "hel"
        await client.close()
        await client.close()
        assert service.deleted == ["owned-1"] and channels.closed
    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://hub.example", "https://pw:secret@hub.example",
                                  "https://hub.example/?token=secret", "https://hub.example/#secret"])
def test_rejects_unsafe_entrypoint(url):
    with pytest.raises(hub.HubError) as error:
        hub.HubClient(url, token="example-token")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("redirect", ["https://external.example/?token=example-token", "/prefix/user/other/lab"])
def test_does_not_forward_credentials_on_untrusted_redirect(endpoint, redirect):
    service, _ = endpoint
    service.redirect = redirect
    async def run():
        client = hub.HubClient(BASE, token="example-token")
        with pytest.raises(hub.HubError) as error:
            await client.discover()
        assert "example-token" not in str(error.value)
        assert service.paths == [HUB + "api/user"]
        await client.close()
    asyncio.run(run())


def test_rejects_stopped_server_and_invalid_folder_without_creating_kernel(endpoint):
    service, _ = endpoint
    async def run():
        client = hub.HubClient(BASE, token="example-token")
        service.server = None
        with pytest.raises(hub.HubError, match="running|start"):
            await client.discover()
        service.server = SERVER
        await client.discover()
        for folder in ("../other", "missing"):
            with pytest.raises(hub.HubError) as error:
                await client.start("python3", folder)
            assert "example-token" not in str(error.value)
        assert not service.created
        await client.close()
    asyncio.run(run())


def test_personal_lab_link_and_limited_token(monkeypatch):
    async def run():
        calls = []
        def identity_only(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"name": "demo", "kind": "user"})
        factory = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(identity_only), **kw))
        client = hub.HubClient(BASE + "user/demo/lab/tree/report.ipynb", token="example-token")
        try:
            with pytest.raises(hub.HubError, match="permissions"):
                await client.discover()
            assert calls == [HUB + "api/user"]
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_uncertain_execution_terminates_kernel_and_never_replays(endpoint, cancel):
    service, channels = endpoint
    channels.wait = True
    async def run():
        client = hub.HubClient(BASE, token="example-token")
        await client.start("python3", service.folder)
        task = asyncio.create_task(client.execute("side_effect()", 30 if cancel else .01, 100))
        while not channels.writes:
            await asyncio.sleep(0)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else hub.HubError):
            await task
        assert len(channels.writes) == 1 and service.deleted == ["owned-1"]
        with pytest.raises(hub.HubError):
            await client.execute("more()", 2, 100)
        await client.close()
    asyncio.run(run())

def test_oauth_codes_and_session_cookies_are_absent_from_http_logs(endpoint, caplog):
    service, _ = endpoint
    service.form_login = True
    caplog.set_level("DEBUG")
    async def run():
        client = hub.HubClient(BASE, username="demo", password="form-secret")
        try:
            await client.discover()
        finally:
            await client.close()
    asyncio.run(run())
    assert "short-lived" not in caplog.text
    assert "form-secret" not in caplog.text


def test_failed_password_is_reported_without_upstream_details(endpoint, monkeypatch):
    service, _ = endpoint
    handle = service.handle
    def reject(request):
        if request.method == "POST":
            return httpx.Response(200, text="Invalid form-secret.")
        return handle(request)
    service.form_login = True
    monkeypatch.setattr(service, "handle", reject)
    async def run():
        client = hub.HubClient(BASE, username="demo", password="form-secret")
        try:
            with pytest.raises(hub.HubError, match="sign-in") as error:
                await client.discover()
            assert "form-secret" not in str(error.value)
            assert HUB + "api/user" not in service.paths
        finally:
            await client.close()
    asyncio.run(run())


def test_cancel_during_kernel_creation_collects_id_and_deletes_owned_kernel(endpoint, monkeypatch):
    service, _ = endpoint
    handle = service.handle
    async def run():
        accepted, release = asyncio.Event(), asyncio.Event()
        async def accept_then_wait(request):
            response = handle(request)
            if request.method == "POST" and request.url.path == SERVER + "api/kernels":
                accepted.set()
                await release.wait()
            return response
        monkeypatch.setattr(service, "handle", accept_then_wait)
        client = hub.HubClient(BASE, token="example-token")
        task = asyncio.create_task(client.start("python3", service.folder))
        await asyncio.wait_for(accepted.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(service.created) == 1 and service.deleted == ["owned-1"]
        await client.close()
    asyncio.run(run())


def test_lost_creation_response_identifies_uncertain_kernel_without_retry(endpoint, monkeypatch):
    service, _ = endpoint
    handle = service.handle
    def lose_reply(request):
        response = handle(request)
        if request.method == "POST" and request.url.path == SERVER + "api/kernels":
            raise httpx.ReadError("example-token")
        return response
    monkeypatch.setattr(service, "handle", lose_reply)
    async def run():
        client = hub.HubClient(BASE, token="example-token")
        with pytest.raises(hub.HubError, match="running kernels") as error:
            await client.start("python3", service.folder)
        assert "example-token" not in str(error.value) and len(service.created) == 1
        await client.close()
    asyncio.run(run())

def test_only_python_language_kernels_can_be_selected(endpoint):
    service, _ = endpoint
    service.specs = {"analysis": {"spec": {"language": "Python3"}}, "python3": {"spec": {"language": "R"}},
                     "no-language": {"spec": {}}, "invalid": None}
    async def run():
        client = hub.HubClient(BASE, token="example-token")
        assert await client.discover() == ["analysis"]
        with pytest.raises(hub.HubError, match="kernel"):
            await client.start("python3", "")
        assert not service.created
        await client.close()
    asyncio.run(run())
