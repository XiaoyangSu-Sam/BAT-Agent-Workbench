"""Per-chat JupyterHub connections. Only the execution target kind survives restart."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr

from .errors import HarnessError, clean
from .jupyterhub import HubClient, HubError


class HubLogin(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    username: str = Field(default="", max_length=512)
    password: SecretStr = Field(default=SecretStr(""), max_length=8192)
    token: SecretStr = Field(default=SecretStr(""), max_length=8192)


class HubSelection(BaseModel):
    kernel: str = Field(min_length=1, max_length=256)
    folder: str = Field(default="", max_length=4096)


@dataclass
class Connection:
    client: HubClient
    workspace: Path
    kernels: list[str]
    secrets: tuple[str, ...] = field(repr=False)
    kernel: str = ""
    folder: str = ""


class HubSessions:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.active: dict[str, Connection] = {}
        self.pending: dict[str, Connection] = {}
        self.changing = False

    @contextmanager
    def change(self, manager):
        if manager.busy():
            raise HarnessError("workspace_busy", "Wait for the current operation or stop the active run.")
        self.changing = True
        try:
            yield
        finally:
            self.changing = False

    def current(self, key):
        active = self.active.get(key)
        if active and active.workspace == self.settings.workspace:
            return active
        return None

    def status(self, key):
        target = self.store.session(key)["execution_target"]
        connection = self.current(key) if target == "jupyterhub" else None
        return {"target": target, "connected": connection is not None,
                "kernel": connection.kernel if connection else "", "folder": connection.folder if connection else ""}

    def mark(self, key, target):
        self.store.set_execution_target(key, target, self.settings.execution.approval_mode)

    @staticmethod
    async def close_clients(*connections):
        results = await asyncio.gather(*(c.client.close() for c in connections if c), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise HarnessError("hub_cleanup_failed", "Could not confirm kernel cleanup. Check your running sessions in JupyterLab.", "jupyterhub")

    async def close_all(self):
        connections = [*self.pending.values(), *self.active.values()]
        self.pending.clear()
        self.active.clear()
        await self.close_clients(*connections)

    async def discover(self, key, login):
        self.store.session(key)
        await self.close_clients(self.pending.pop(key, None))
        connection = None
        try:
            password, token = login.password.get_secret_value(), login.token.get_secret_value()
            client = HubClient(login.url, username=login.username, password=password, token=token)
            connection = Connection(client, self.settings.workspace, [], (password, token))
            connection.kernels = await client.discover()
            self.pending[key] = connection
            return {"kernels": connection.kernels}
        except HubError as exc:
            raise HarnessError("hub_discovery_failed", str(exc), "jupyterhub") from None
        except Exception:
            raise HarnessError("hub_discovery_failed", "Could not discover kernels on your JupyterHub server.", "jupyterhub") from None
        finally:
            if connection and key not in self.pending:
                await self.close_clients(connection)

    async def connect(self, key, selection):
        self.store.session(key)
        candidate = self.pending.get(key)
        if not candidate or candidate.workspace != self.settings.workspace or selection.kernel not in candidate.kernels:
            raise HarnessError("hub_discovery_required", "Discover kernels in this chat and select one from the list.", "jupyterhub")
        self.mark(key, "jupyterhub")
        self.pending.pop(key)
        try:
            await self.close_clients(self.active.pop(key, None))
            await candidate.client.start(selection.kernel, selection.folder)
            candidate.kernel, candidate.folder = selection.kernel, selection.folder
            self.active[key] = candidate
        except HubError as exc:
            raise HarnessError("hub_connect_failed", str(exc), "jupyterhub") from None
        except HarnessError:
            raise
        except Exception:
            raise HarnessError("hub_connect_failed", "Could not start the selected kernel. Discover kernels again.", "jupyterhub") from None
        finally:
            if key not in self.active:
                await self.close_clients(candidate)
        return self.status(key)

    async def disconnect(self, key):
        session = self.store.session(key)
        if session["execution_target"] != "local":
            self.mark(key, "jupyterhub")
        await self.close_clients(self.pending.pop(key, None), self.active.pop(key, None))
        if session["execution_target"] != "local":
            self.mark(key, "local")
        return self.status(key)

    async def invalidate(self, key):
        connection = self.active.pop(key, None)
        self.mark(key, "jupyterhub")
        try:
            await self.close_clients(connection)
        except HarnessError:
            pass  # The activity's outcome is already being reported as uncertain.

    async def execute(self, key, code, *, max_output=None, decode=None):
        connection = self.current(key)
        if connection is None:
            raise HarnessError("hub_disconnected", "Reconnect this chat to JupyterHub before executing code.", "jupyterhub")
        config = self.settings.execution
        try:
            result = await connection.client.execute(code, config.timeout, max_output or config.max_output_chars)
            if decode is not None:
                result = decode(result)
            return clean(result, connection.secrets)
        except (Exception, asyncio.CancelledError) as exc:
            await self.invalidate(key)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HarnessError("hub_execution_unknown",
                               "Kernel execution was interrupted. Check results in JupyterLab before reconnecting or rerunning code.",
                               "jupyterhub") from None


def register_hub_routes(app, manager):
    hubs = manager.hubs
    prefix = "/api/sessions/{session_id}/hub"

    @app.get(prefix)
    async def hub_status(session_id: str):
        return hubs.status(session_id)

    @app.post(prefix + "/discover")
    async def hub_discover(session_id: str, value: HubLogin):
        with hubs.change(manager):
            return await hubs.discover(session_id, value)

    @app.post(prefix + "/connect")
    async def hub_connect(session_id: str, value: HubSelection):
        with hubs.change(manager):
            return await hubs.connect(session_id, value)

    @app.post(prefix + "/disconnect")
    async def hub_disconnect(session_id: str):
        with hubs.change(manager):
            return await hubs.disconnect(session_id)
