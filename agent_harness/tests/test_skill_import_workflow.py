import io
import json
import time
from zipfile import ZipFile

import pytest

from agent_harness.errors import HarnessError
from agent_harness.runtime.context import system_prompt

from agent_harness.tests.test_workbench import client_for


def archive():
    data = io.BytesIO()
    with ZipFile(data, "w") as bundle:
        bundle.writestr("example-main/skills/chart/SKILL.md", "# Chart\nDraw a chart.")
        bundle.writestr("example-main/skills/chart/assets/colors.bin", bytes(range(256)))
    return data.getvalue()


def test_preview_owner_config_and_protected_folder(settings):
    (settings.workspace / "sample.zip").write_bytes(archive())
    private_folder = settings.workspace / ".env-local" / "nested"
    private_folder.mkdir(parents=True)
    (private_folder / "SKILL.md").write_text("# Example")
    with client_for(settings.root) as client:
        imports = client.app.state.manager.imports
        ticket = imports.preview("first-chat", "workspace", "sample.zip")["ticket"]
        with pytest.raises(HarnessError, match="Preview the source again"):
            imports.apply("second-chat", ticket, ["*"])
        assert client.post("/api/skill-import/apply", json={"ticket": ticket, "selected": ["*"]}).status_code == 410
        assert client.put("/api/skill-source", json={"url": "https://example.invalid/skills.git"}).is_success
        with pytest.raises(HarnessError, match="Preview the source again"):
            imports.apply("first-chat", ticket, ["*"])
        assert not (settings.root / "skills/chart").exists()
        reply = client.post("/api/skill-import/preview", json={"source": "folder", "path": str(private_folder)})
        assert reply.status_code == 400 and "protected" in reply.text


def test_large_imported_catalog_stays_searchable_in_chat(settings):
    with client_for(settings.root) as client:
        registry = client.app.state.manager.imports.registry
        for i in range(200):
            key = f"example-{i:03}"
            registry.skills[key] = {"id": key, "name": key, "description": "Résumé " * 140}
        prompt = system_prompt(registry, "native")
        assert len(json.dumps(prompt)) < 12000 and "list_skills" in prompt
        assert registry.find_skills("example-199")["skills"][0]["id"] == "example-199"
        assert registry.find_skills("résumé")["next_offset"] == 10
        assert registry.find_skills("résumé", 190)["next_offset"] is None


def test_chat_pages_one_snapshot_and_installs_from_another_page(settings):
    with ZipFile(settings.workspace / "sample.zip", "w") as bundle:
        for i in range(30):
            bundle.writestr(f"set/example-{i:02}/SKILL.md", "# Example")
    with client_for(settings.root) as client:
        imports = client.app.state.manager.imports
        first = imports.chat_preview("chat", "workspace", "sample.zip")
        second = imports.chat_preview("chat", ticket=first["ticket"], offset=first["next_offset"])
        assert len(first["skills"]) == 25 and len(second["skills"]) == 5
        assert second["ticket"] == first["ticket"] and second["next_offset"] is None
        found = imports.chat_preview("chat", ticket=first["ticket"], query="example-29")
        assert found["matches"] == 1 and found["source_total"] == 30
        applied = imports.chat_apply("chat", found["ticket"], [found["skills"][0]["path"]])
        assert applied["results"][0]["status"] == "installed"
        assert (settings.root / "skills/example-29/SKILL.md").is_file()


def test_upload_install_and_conflict(settings):
    with client_for(settings.root) as client:
        def preview():
            reply = client.post("/api/skill-import/upload?name=example.zip", content=archive())
            assert reply.status_code == 200, reply.text
            return reply.json()
        def apply(data):
            return client.post("/api/skill-import/apply", json={"ticket": data["ticket"], "selected": ["*"]})
        data = preview()
        assert data["skills"] == [{"id": "chart", "path": "example-main/skills/chart"}]
        assert apply(data).json()["results"][0]["status"] == "installed"
        assert apply(data).status_code == 410
        assert apply(preview()).json()["results"][0]["status"] == "identical"
        target = settings.root / "skills/chart/assets/colors.bin"
        assert target.read_bytes() == bytes(range(256))
        target.write_bytes(b"my edits")
        assert apply(preview()).json()["results"][0]["status"] == "conflict"
        assert target.read_bytes() == b"my edits"
        assert "chart" in [s["id"] for s in client.get("/api/skills").json()["skills"]]


def test_chat_preview_approval_install(settings, monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_MODEL_PROVIDER", "fake")
    (settings.workspace / "sample.zip").write_bytes(archive())

    class Model:
        def __init__(self, config):
            self.step = 0

        async def complete(self, messages, schemas, emit, tools=True):
            self.step += 1
            if self.step == 1:
                name, args = "list_available_skills", {"source": "workspace", "path": "sample.zip"}
            elif self.step == 2:
                preview = json.loads(messages[-1]["content"])["result"]
                name, args = "install_skills", {"ticket": preview["ticket"], "selected": [preview["skills"][0]["path"]]}
            else:
                return {"role": "assistant", "content": "Done"}
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{self.step}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}

    with client_for(settings.root, Model) as client:
        session = client.post("/api/sessions", json={"approval_mode": "execution"}).json()["id"]
        run = "/api/runs/" + client.post(f"/api/sessions/{session}/runs", json={"prompt": "Install chart from sample.zip"}).json()["run_id"]
        def wait_for(*states):
            for _ in range(400):
                data = client.get(run).json()
                if data["status"] in states: return data
                time.sleep(.01)
            raise AssertionError(data["status"])
        report = wait_for("awaiting_approval", "failed", "completed")
        event = next(e["data"] for e in report["events"] if e["type"] == "approval_required")
        assert event["name"] == "install_skills"
        assert not (settings.root / "skills/chart").exists()
        assert client.post(run + "/approval", json={"approval_id": event["approval_id"], "approved": True}).is_success
        assert wait_for("completed", "failed")["status"] == "completed"
        assert (settings.root / "skills/chart/assets/colors.bin").read_bytes() == bytes(range(256))
