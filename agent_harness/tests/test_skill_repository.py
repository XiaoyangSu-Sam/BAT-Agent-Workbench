"""Configured repository access, using only a disposable Git server."""
import shlex
import subprocess
from zipfile import ZipFile

import pytest

from agent_harness.errors import HarnessError
from agent_harness.skill_repository import SkillRepository


URL = "git@skills.example:catalog/field-kit.git"


@pytest.mark.parametrize("url", [
    "https://skills.example/team/field-kit.git",
    "ssh://git@skills.example:2222/team/field-kit.git",
    "ssh://git@[::1]:2222/team/field-kit.git",
    URL,
])
def test_settings_persist_and_can_be_cleared(tmp_path, url):
    repository = SkillRepository(tmp_path)
    assert repository.settings() == {"url": "", "subdir": ""}
    assert repository.configure(url, "skills/set") == {"url": url, "subdir": "skills/set"}
    assert SkillRepository(tmp_path).settings() == repository.settings()
    assert repository.configure("", "") == {"url": "", "subdir": ""}
    with pytest.raises(HarnessError):
        with repository.snapshot():
            pytest.fail("No source configured")


@pytest.mark.parametrize("url,subdir", [
    ("file:///tmp/field-kit", ""), ("ext::unused", ""), ("host::unused", ""),
    ("https://user:secret@skills.example/repo", ""), ("https://skills.example/repo?token=secret", ""),
    ("ssh://-option/kit", ""), ("-option:path", ""), ("C:/private/repo", ""),
    (URL, "../other"), (URL, "skills/.git"), (URL, "/absolute"), (URL, "a\\b"),
])
def test_invalid_changes_leave_saved_repository_untouched(tmp_path, url, subdir):
    repository = SkillRepository(tmp_path)
    saved = repository.configure(URL, "catalog")
    with pytest.raises(HarnessError) as error:
        repository.configure(url, subdir)
    assert "secret" not in error.value.message
    assert repository.settings() == saved


def git(directory, *args, data=None):
    return subprocess.run(["git", "-C", str(directory), *args], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


@pytest.fixture
def origin(tmp_path, monkeypatch):
    # The trusted SSH command connects upload-pack to a disposable local repository.
    source = tmp_path / "origin"
    source.mkdir()
    config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    ssh = tmp_path / "fixture-ssh"
    ssh.write_bytes(b'#!/bin/sh\nexec git upload-pack "$BAT_TEST_ORIGIN"\n')
    ssh.chmod(0o700)
    monkeypatch.setenv("GIT_SSH_COMMAND", "sh " + shlex.quote(ssh.as_posix()))
    monkeypatch.setenv("BAT_TEST_ORIGIN", str(source))
    git(source, "init", "-b", "library")
    git(source, "config", "user.name", "Example")
    git(source, "config", "user.email", "author@example.invalid")
    git(source, "config", "--global", "filter.check.clean", "cat")
    git(source, "config", "--global", "filter.check.smudge", "exit 17")
    git(source, "config", "--global", "filter.check.required", "true")
    tree = source / "catalog" / "safety-check"
    (tree / "templates").mkdir(parents=True)
    (source / "SKILL.md").write_bytes(b"# Field kit\n")
    (tree / "SKILL.md").write_bytes(b"# Safety check\n")
    (tree / "templates/tool.bin").write_bytes(b"\x00\r\n\xffraw")
    (source / ".gitattributes").write_bytes(b"*.bin -text filter=check export-ignore\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Add fixture")
    return source


def test_exports_committed_bytes_without_smudge_or_archive_filters(tmp_path, origin):
    from agent_harness.skill_import import install_skills, preview_skills
    from agent_harness.runtime.skills import SkillRegistry

    repository = SkillRepository(tmp_path / "state")
    assert repository.configure(URL, ".")["subdir"] == ""
    with repository.snapshot() as (source, subdir):
        assert preview_skills(source, subdir)["skills"] == [{"id": "field-kit", "path": "."}]
        assert list(source.parent.iterdir()) == [source]
    assert not source.exists()
    repository.configure(URL, "catalog")
    with repository.snapshot() as (source, subdir):
        assert preview_skills(source, subdir)["skills"] == [{"id": "safety-check", "path": "safety-check"}]
        registry = SkillRegistry(tmp_path / "installed")
        outcome = install_skills(registry, source, ["safety-check"], subdir)
        assert outcome["results"][0]["status"] == "installed"
        assert (registry.root / "safety-check/templates/tool.bin").read_bytes() == b"\x00\r\n\xffraw"
    assert not source.exists()


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_rejects_links_and_submodules_within_selected_tree(tmp_path, origin, mode):
    if mode == "120000":
        oid = git(origin, "hash-object", "-w", "--stdin", data=b"../../../elsewhere").decode().strip()
    else:
        oid = git(origin, "rev-parse", "HEAD").decode().strip()
    git(origin, "update-index", "--add", "--cacheinfo", f"{mode},{oid},foreign")
    git(origin, "commit", "-m", "Add unsupported entry")
    repository = SkillRepository(tmp_path / "state")
    repository.configure(URL, "")
    with pytest.raises(HarnessError):
        with repository.snapshot():
            pytest.fail("Unsupported Git object was accepted")
    repository.configure(URL, "catalog/safety-check")
    with repository.snapshot() as (source, subdir):
        from agent_harness.skill_import import preview_skills
        assert preview_skills(source, subdir)["skills"] == [{"id": "safety-check", "path": "."}]


def test_git_failure_is_generic_and_discards_temp_files(tmp_path, origin, monkeypatch):
    state = tmp_path / "state"
    repository = SkillRepository(state)
    repository.configure(URL, "")
    monkeypatch.setenv("BAT_TEST_ORIGIN", str(origin / "private-diagnostic"))
    before = set(state.iterdir())
    with pytest.raises(HarnessError) as error:
        with repository.snapshot():
            pytest.fail("Unexpectedly opened missing repository")
    assert "private-diagnostic" not in error.value.message
    assert "skills.example" not in error.value.message
    assert set(state.iterdir()) == before


def test_transport_rewrites_cannot_enable_local_file_access(tmp_path, origin):
    git(origin, "config", "--global", f"url.{origin.as_uri()}.insteadOf", URL)
    repository = SkillRepository(tmp_path / "state")
    repository.configure(URL, "")
    with pytest.raises(HarnessError):
        with repository.snapshot():
            pytest.fail("An unapproved transport was used")


def test_wrapper_private_files_are_omitted_but_skill_remains_installable(tmp_path, origin):
    from agent_harness.runtime.skills import SkillRegistry
    from agent_harness.skill_import import install_skills

    git(origin, "rm", "SKILL.md")
    (origin / ".env.example").write_bytes(b"private sibling")
    git(origin, "add", ".env.example")
    git(origin, "commit", "-m", "Keep catalog under wrapper")
    repository = SkillRepository(tmp_path / "state")
    repository.configure(URL, "")
    with repository.snapshot() as (source, subdir):
        with ZipFile(source) as snapshot:
            assert snapshot.read(".env.example") == b""
        reg = SkillRegistry(tmp_path / "installed")
        assert install_skills(reg, source, ["*"], subdir)["results"] == [
            {"id": "safety-check", "path": "catalog/safety-check", "status": "installed"}]
