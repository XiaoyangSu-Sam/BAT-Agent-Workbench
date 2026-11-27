import asyncio
import io
import json
import time
from contextlib import redirect_stdout

import pytest

from agent_harness.runtime.paths import digest
from agent_harness.tests.test_workbench import client_for


class FakeHub:
    def __init__(self, url, username="", password="", token=""):
        self.closed = False
        self.calls = []
        self.after_execute = None

    async def discover(self):
        return ["python3"]

    async def start(self, kernel, folder):
        if folder == "missing":
            raise RuntimeError("synthetic private upstream detail")

    async def execute(self, code, timeout, max_output):
        self.calls.append(code)
        if self.after_execute:
            self.after_execute()
        return {"ok": True, "stdout": "42\n", "stderr": "", "execution_count": 3,
                "outputs": [{"output_type": "stream", "name": "stdout", "text": "42\n"}]}

    async def close(self):
        self.closed = True


@pytest.fixture
def hubs(monkeypatch):
    from agent_harness import hub_routes
    created = []

    def make(*args, **kwargs):
        created.append(FakeHub(*args, **kwargs))
        return created[-1]

    monkeypatch.setattr(hub_routes, "HubClient", make)
    return created


def model_call(name, args, observed):
    class ScriptedModel:
        def __init__(self, config):
            self.first = True

        async def complete(self, messages, schemas, emit, tools=True):
            observed.append(({s["function"]["name"] for s in schemas}, messages))
            if self.first:
                self.first = False
                return {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_example", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}
            return {"role": "assistant", "content": "Finished."}
    return ScriptedModel


def new(client):
    return "/api/sessions/" + client.post("/api/sessions", json={}).json()["id"]


def discover(client, session):
    return client.post(session + "/hub/discover", json={"url": "https://hub.example.test",
                       "username": "sample-user", "password": "sample-private-password"})


def connect(client, session):
    assert discover(client, session).json() == {"kernels": ["python3"]}
    status = client.post(session + "/hub/connect", json={"kernel": "python3", "folder": "analysis"})
    assert status.json() == {"target": "jupyterhub", "kernel": "python3", "folder": "analysis", "connected": True}


def wait_status(client, run, *expected):
    for _ in range(300):
        report = client.get(run).json()
        if report["status"] in expected:
            return report
        time.sleep(.01)
    pytest.fail(str(report))


def start(client, session):
    return "/api/runs/" + client.post(session + "/runs", json={"prompt": "Run analysis in this chat"}).json()["run_id"]


def decide(client, run, accepted, allow_all=False):
    report = wait_status(client, run, "awaiting_approval")
    event = next(e["data"] for e in report["events"] if e["type"] == "approval_required")
    assert event["execution_target"] == "JupyterHub" and event["kernel"] == "python3" and event["folder"] == "analysis"
    assert client.post(run + "/approval", json={"approval_id": event["approval_id"], "approved": accepted,
                       "allow_all_execution": allow_all}).status_code == 200
    return wait_status(client, run, "completed", "failed")


def test_session_target_approval_and_run_round_trip(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    observed = []
    with client_for(settings.root, model_call("run_jupyter_cell", {"code": "print(42)"}, observed)) as client:
        session, other = new(client), new(client)
        client.app.state.store.set_execution_approval(session.split("/")[-1], True)
        connect(client, session)
        assert client.get(other + "/hub").json()["target"] == "local"
        assert client.post(other + "/hub/connect", json={"kernel": "python3", "folder": "."}).status_code == 400
        assert not client.get(session).json()["allow_all_execution"]

        run = start(client, session)
        wait_status(client, run, "awaiting_approval")
        assert client.post(session + "/hub/disconnect", json={}).status_code == 409
        assert client.post("/api/workspace/open", json={"path": str(settings.root)}).status_code == 409
        assert not hubs[0].calls
        assert decide(client, run, False)["error"]["code"] == "approval_denied"
        assert not hubs[0].calls

        report = decide(client, start(client, session), True, allow_all=True)
        assert report["status"] == "completed" and hubs[0].calls == ["print(42)"]
        assert '42' in observed[-1][1][-1]["content"]
        names = observed[0][0]
        assert {"read_file", "edit_file", "run_jupyter_script", "run_jupyter_notebook_cell"} <= names
        assert not names.intersection({"run_command", "run_python_script", "run_notebook"})
        assert "sample-private-password" not in str(observed) + str(report) + client.get(session).text
        assert client.get(session).json()["allow_all_execution"]
        assert client.post(session + "/hub/disconnect", json={}).json()["target"] == "local"
        assert hubs[0].closed and not client.get(session).json()["allow_all_execution"]


def test_failed_disconnect_keeps_remote_marker_and_revokes_grant(settings, hubs):
    with client_for(settings.root) as client:
        session = new(client)
        connect(client, session)
        client.app.state.store.set_execution_approval(session.split("/")[-1], True)
        async def fail_cleanup():
            raise RuntimeError("private upstream detail")
        hubs[0].close = fail_cleanup
        error = client.post(session + "/hub/disconnect", json={})
        assert error.json()["error"]["code"] == "hub_cleanup_failed" and "private upstream" not in error.text
        assert client.get(session + "/hub").json()["target"] == "jupyterhub"
        assert not client.get(session + "/hub").json()["connected"]
        assert not client.get(session).json()["allow_all_execution"]


def test_restart_workspace_change_and_failed_reconnect_never_restore_local_execution(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    observed = []
    factory = model_call("run_command", {"argv": ["bogus"]}, observed)
    def assert_offline(client, session):
        assert client.get(session + "/hub").json() == {
            "target": "jupyterhub", "connected": False, "kernel": "", "folder": ""}
        report = wait_status(client, start(client, session), "completed", "failed")
        assert not observed[-1][0].intersection({"run_command", "run_python_script", "run_notebook", "run_jupyter_cell"})
        assert any(e["data"].get("error", {}).get("code") == "unknown_tool" for e in report["events"])
    with client_for(settings.root, factory) as client:
        session = new(client)
        connect(client, session)
    assert hubs[0].closed
    with client_for(settings.root, factory) as client:
        assert_offline(client, session)
        connect(client, session)
        other = settings.root / "other"
        other.mkdir()
        assert client.post("/api/workspace/open", json={"path": str(other)}).status_code == 200
        assert hubs[-1].closed
        assert_offline(client, session)
        discover(client, session)
        error = client.post(session + "/hub/connect", json={"kernel": "python3", "folder": "missing"})
        assert error.status_code == 400 and "private upstream" not in error.text
        assert_offline(client, session)


def test_workspace_switch_reports_new_root_and_cleanup_warning_if_kernel_deletion_fails(settings, hubs):
    destination = settings.root / "new-workspace"
    destination.mkdir()
    (destination / "current.txt").write_text("selected workspace", encoding="utf-8")
    with client_for(settings.root) as client:
        session = new(client)
        connect(client, session)
        client.app.state.store.set_execution_approval(session.split("/")[-1], True)
        async def fail_cleanup():
            raise RuntimeError("upstream details")
        hubs[0].close = fail_cleanup
        response = client.post("/api/workspace/open", json={"path": str(destination)})
        assert response.status_code == 200
        data = response.json()
        assert data["workspace"] == str(destination) and "JupyterLab" in data["hub_cleanup_warning"]
        assert "upstream details" not in response.text
        assert client.get("/api/config").json()["workspace"] == data["workspace"]
        tree = client.get("/api/workspace/tree").json()
        assert tree["root"] == data["workspace"] and tree["files"] == ["current.txt"]
        assert client.get(session + "/hub").json()["target"] == "jupyterhub"
        assert not client.get(session + "/hub").json()["connected"]
        assert not client.get(session).json()["allow_all_execution"]
    with client_for(settings.root) as reopened:
        assert reopened.get("/api/config").json()["workspace"] == str(destination)


def test_notebook_outputs_saved_to_matching_local_code_only(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    path = settings.workspace / "analysis.ipynb"
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": "Notes"},
        {"cell_type": "code", "metadata": {}, "source": ["x = 42\n", "print(x)"], "outputs": [], "execution_count": None}]}
    path.write_text(json.dumps(notebook), encoding="utf-8")
    args = {"path": path.name, "expected_sha256": digest(path.read_bytes()), "cell_index": 1}
    with client_for(settings.root, model_call("run_jupyter_notebook_cell", args, [])) as client:
        session = new(client)
        connect(client, session)
        assert decide(client, start(client, session), True)["status"] == "completed"
        changed = json.loads(path.read_bytes())
        assert changed["cells"][0] == notebook["cells"][0]
        assert changed["cells"][1]["outputs"][0]["text"] == "42\n"
        assert changed["cells"][1]["execution_count"] == 3
        assert hubs[0].calls == ["x = 42\nprint(x)"]

        args["expected_sha256"] = digest(path.read_bytes())
        hubs[0].after_execute = lambda: path.write_text('{"new":true}', encoding="utf-8")
        report = decide(client, start(client, session), True)
        assert report["status"] == "failed" and report["error"]["code"] == "hub_output_not_saved"
        assert path.read_bytes() == b'{"new":true}'
        assert len(hubs[0].calls) == 2


def test_unknown_execution_invalidates_only_own_kernel_and_stops_run(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    observed = []
    def lose_channel():
        raise RuntimeError("synthetic private error")
    with client_for(settings.root, model_call("run_jupyter_cell", {"code": "counter += 1"}, observed)) as client:
        first, second = new(client), new(client)
        connect(client, first)
        connect(client, second)
        hubs[0].after_execute = lose_channel
        report = decide(client, start(client, first), True, allow_all=True)
        assert report["error"]["code"] == "hub_execution_unknown"
        assert len(observed) == 1 and hubs[0].calls == ["counter += 1"]
        assert "synthetic private" not in str(report)
        assert hubs[0].closed and not hubs[1].closed
        assert not client.get(first).json()["allow_all_execution"]
        assert not client.get(first + "/hub").json()["connected"]
        assert client.get(second + "/hub").json()["connected"]


def test_script_child_uses_kernel_python_reports_exit_and_requires_matching_source(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    path = settings.workspace / "analysis.py"
    path.write_text("import sys, __main__\nprint(sys.argv, __name__, __main__.__file__)\n"
                    "print('notice', file=sys.stderr)\nsys.exit(7)\n", encoding="utf-8")
    args = {"path": path.name, "expected_sha256": digest(path.read_bytes()), "args": ["one two"]}
    requests = []
    async def run_in_kernel(self, code, timeout, max_output):
        requests.append(code)
        output = io.StringIO()
        with redirect_stdout(output):
            exec(code, {})
        return {"ok": True, "stdout": output.getvalue()}
    monkeypatch.setattr(FakeHub, "execute", run_in_kernel)
    with client_for(settings.root, model_call("run_jupyter_script", args, [])) as client:
        session = new(client)
        connect(client, session)
        report = decide(client, start(client, session), True)
        result = next(e["data"]["result"] for e in report["events"] if e["type"] == "tool_result")
        assert not result["ok"] and result["exit_code"] == 7
        assert "['analysis.py', 'one two'] __main__" in result["stdout"] and "analysis.py" in result["stdout"]
        assert result["stderr"].strip() == "notice"
        path.write_text("print('external edit')", encoding="utf-8")
        report = decide(client, start(client, session), True)
        assert any(e["data"].get("error", {}).get("code") == "file_conflict" for e in report["events"])
        assert len(requests) == 1


def test_cancelling_during_execution_drops_only_the_owned_kernel(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    async def block(self, code, timeout, max_output):
        self.calls.append(code)
        await asyncio.Event().wait()
    monkeypatch.setattr(FakeHub, "execute", block)
    with client_for(settings.root, model_call("run_jupyter_cell", {"code": "process_data()"}, [])) as client:
        first, second = new(client), new(client)
        connect(client, first)
        connect(client, second)
        run = start(client, first)
        pending = wait_status(client, run, "awaiting_approval")
        approval_id = next(e["data"]["approval_id"] for e in pending["events"] if e["type"] == "approval_required")
        client.post(run + "/approval", json={"approval_id": approval_id, "approved": True, "allow_all_execution": True})
        for _ in range(100):
            if hubs[0].calls:
                break
            time.sleep(.01)
        assert hubs[0].calls == ["process_data()"]
        assert client.post(run + "/cancel", json={}).json()["status"] == "cancelled"
        assert not client.get(first + "/hub").json()["connected"] and hubs[0].closed
        assert not client.get(first).json()["allow_all_execution"]
        assert client.get(second + "/hub").json()["connected"] and not hubs[1].closed


def test_discovery_is_private_to_each_chat_and_full_access_does_not_cross_targets(settings, hubs):
    with client_for(settings.root) as client:
        session, other = new(client), new(client)
        client.post(session + "/approval-mode", json={"approval_mode": "trusted"})
        assert discover(client, session).status_code == 200
        assert client.post(other + "/hub/connect", json={"kernel": "python3", "folder": ""}).status_code == 400
        assert client.post(session + "/hub/connect", json={"kernel": "python3", "folder": ""}).status_code == 200
        assert client.get(session).json()["approval_mode"] == "execution"
        assert not hubs[0].closed
        assert discover(client, other).status_code == 200
        assert client.post(other + "/hub/disconnect", json={}).status_code == 200
        assert hubs[1].closed and not hubs[0].closed


def test_remote_output_cannot_echo_login_secrets_to_model_or_history(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    async def echo(self, code, timeout, max_output):
        return {"ok": True, "stdout": "sample-private-password\n", "outputs": [
            {"output_type": "stream", "name": "stdout", "text": "sample-private-password\n"}]}
    monkeypatch.setattr(FakeHub, "execute", echo)
    observed = []
    with client_for(settings.root, model_call("run_jupyter_cell", {"code": "process()"}, observed)) as client:
        session = new(client)
        connect(client, session)
        report = decide(client, start(client, session), True)
        assert "sample-private-password" not in str(report) + str(observed) + client.get(session).text
        assert "[REDACTED]" in observed[-1][1][-1]["content"]


def test_unparseable_script_reply_requires_reconnection_and_review(settings, monkeypatch, hubs):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    source = settings.workspace / "analysis.py"
    source.write_text("print(42)", encoding="utf-8")
    args = {"path": source.name, "expected_sha256": digest(source.read_bytes())}
    with client_for(settings.root, model_call("run_jupyter_script", args, [])) as client:
        session = new(client)
        connect(client, session)
        report = decide(client, start(client, session), True, allow_all=True)
        assert report["error"]["code"] == "hub_execution_unknown"
        assert hubs[0].closed and not client.get(session + "/hub").json()["connected"]
        assert not client.get(session).json()["allow_all_execution"]
