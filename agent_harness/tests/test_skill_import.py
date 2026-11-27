import os
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_harness import skill_import
from agent_harness.errors import HarnessError
from agent_harness.runtime.skills import SkillRegistry


def make_skill(folder, body=b"# Test skill\n"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_bytes(body)
    return folder


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            if isinstance(name, str) and "\\" in name:
                # PowerShell 5.1 uses raw backslashes; zipfile normally cleans them on Windows.
                entry = zipfile.ZipInfo(name)
                entry.filename = name
                if name.endswith("\\"):
                    entry.external_attr = (stat.S_IFDIR << 16) | 0x10
                name = entry
            archive.writestr(name, data)
    return path


def test_wrapped_zip_preserves_bytes_and_nested_skill_as_resource(tmp_path):
    source = make_zip(tmp_path / "download.zip", [
        ("repo-main/skills/pack/SKILL.md", b"# Pack\r\n"),
        ("repo-main/skills/pack/refs/nested/SKILL.md", b"# Example"),
        ("repo-main/skills/pack/assets/template.bin", b"\x00\xff\x01"),
        ("repo-main/skills/pack/scripts/demo.py", b"raise AssertionError('never run')"),
        ("repo-main/skills/pack/empty/", b""),
        ("repo-main/skills/other/SKILL.md", b"# Other"),
    ])
    registry = SkillRegistry(tmp_path / "library")
    choices = [{"id": "other", "path": "other"}, {"id": "pack", "path": "pack"}]
    assert skill_import.preview_skills(source, "repo-main/skills") == {"skills": choices}
    assert skill_import.install_skills(registry, source, ["*"], "repo-main/skills") == {
        "results": [{**entry, "status": "installed"} for entry in choices]}
    assert registry.get("pack")["name"] == "Pack"
    installed = registry.root / "pack"
    assert (installed / "SKILL.md").read_bytes() == b"# Pack\r\n"
    assert (installed / "assets/template.bin").read_bytes() == b"\x00\xff\x01"
    assert (installed / "refs/nested/SKILL.md").is_file()
    assert (installed / "empty").is_dir()
    assert not (registry.root / "repo-main").exists()


def test_single_skill_zip_uses_archive_stem(tmp_path):
    source = make_zip(tmp_path / "portable.zip", [("SKILL.md", "# Root")])
    assert skill_import.preview_skills(source) == {"skills": [{"id": "portable", "path": "."}]}


def test_windows_powershell_zip_preserves_binary_files_and_empty_directories(tmp_path):
    source = make_zip(tmp_path / "powershell.zip", [
        (r"wrap\example\SKILL.md", b"# Test"),
        (r"wrap\example\assets\sample.bin", bytes(range(256))),
        ("wrap\\example\\empty\\", b"")])
    choice = {"id": "example", "path": "wrap/example"}
    assert skill_import.preview_skills(source) == {"skills": [choice]}
    reg = SkillRegistry(tmp_path / "library")
    assert skill_import.install_skills(reg, source, ["wrap/example"]) == {
        "results": [{**choice, "status": "installed"}]}
    assert (reg.root / "example/assets/sample.bin").read_bytes() == bytes(range(256))
    assert (reg.root / "example/empty").is_dir()


def test_wrapper_private_files_are_never_opened_and_selected_private_resources_are_rejected(tmp_path, monkeypatch):
    source = make_zip(tmp_path / "repository.zip", [
        ("repo/.env.example", "private"), ("repo/.venv/lib/config", "private"),
        ("repo/skills/alpha/SKILL.md", "# Alpha"), ("repo/skills/incomplete/SKILL.md", "# Second"),
        ("repo/skills/incomplete/secret.key", "private")])
    original_open = zipfile.ZipFile.open

    def check_open(archive, entry, *args, **kwargs):
        assert all(name not in entry.filename for name in (".env", ".venv", ".key"))
        return original_open(archive, entry, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", check_open)
    reg = SkillRegistry(tmp_path / "library")
    assert [s["id"] for s in skill_import.preview_skills(source)["skills"]] == ["alpha", "incomplete"]
    with pytest.raises(HarnessError):
        skill_import.install_skills(reg, source, ["*"])
    assert not (reg.root / "alpha").exists()
    assert skill_import.install_skills(reg, source, ["repo/skills/alpha"])["results"][0]["status"] == "installed"


def test_local_edits_conflict_without_blocking_other_skills(tmp_path):
    src = tmp_path / "checkout"
    make_skill(src / "group/alpha")
    make_skill(src / ".git/private")
    make_skill(src / ".venv/vendor")
    reg = SkillRegistry(tmp_path / "library")
    first = skill_import.install_skills(reg, src, ["group/alpha"])
    assert first["results"][0]["status"] == "installed"
    assert skill_import.install_skills(reg, src, ["*"])["results"] == [
        {"id": "alpha", "path": "group/alpha", "status": "identical"}]
    (reg.root / "alpha/extra.txt").write_bytes(b"private edit")
    make_skill(src / "group/beta")
    assert skill_import.install_skills(reg, src, ["*"])["results"] == [
        {"id": "alpha", "path": "group/alpha", "status": "conflict"},
        {"id": "beta", "path": "group/beta", "status": "installed"}]
    assert (reg.root / "alpha/extra.txt").read_bytes() == b"private edit"
    (reg.root / "alpha/extra.txt").unlink()
    (reg.root / "alpha/SKILL.md").write_bytes(b"# My local version")
    assert skill_import.install_skills(reg, src, ["group/alpha"])["results"][0]["status"] == "conflict"
    assert (reg.root / "alpha/SKILL.md").read_bytes() == b"# My local version"


@pytest.mark.parametrize("name", ["../outside", "/outside", "C:/outside", r"pkg/..\outside",
                                  r"..\outside", r"C:\outside", r"\\host\share\outside",
                                  "pkg/aux.txt", "pkg/a:stream", "pkg/ending.", "pkg/.env",
                                  "pkg/key.PEM", "pkg/.GIT/config", "pkg/t/../private"])
def test_archive_rejects_unsafe_entries_without_publishing(tmp_path, name):
    src = make_zip(tmp_path / "bad.zip", [("pkg/SKILL.md", "# OK"), (name, "bad")])
    reg = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError):
        skill_import.install_skills(reg, src, ["*"])
    assert not (reg.root / "pkg").exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("entries", [
    [("pkg/asset", "file"), ("pkg/ASSET", "alias")],
    [("pkg/Assets/a", "one"), ("pkg/assets/b", "two")],
    [("pkg/asset", "file"), ("pkg/asset/data", "child")],
    [("pkg/asset", "one"), (r"pkg\asset", "alias")],
])
def test_archive_detects_case_aliases_and_file_directory_collisions(tmp_path, entries):
    src = make_zip(tmp_path / "bad.zip", [("pkg/SKILL.md", "# OK"), *entries])
    with pytest.raises(HarnessError):
        skill_import.preview_skills(src)


def test_archive_rejects_symlink_and_forged_windows_reparse_entry(tmp_path):
    entry = zipfile.ZipInfo("pkg/link")
    for attributes in ((stat.S_IFLNK | 0o777) << 16, stat.FILE_ATTRIBUTE_REPARSE_POINT):
        entry.external_attr = attributes
        src = make_zip(tmp_path / "bad.zip", [("pkg/SKILL.md", "# OK"), (entry, "../target")])
        with pytest.raises(HarnessError):
            skill_import.preview_skills(src)


@pytest.mark.parametrize("body", [b" ", b"\xff", b"\x00hello", b"---\nnot: [yaml\n---\n"])
def test_invalid_instruction_file_prevents_partial_bulk_install(tmp_path, body):
    src = tmp_path / "src"
    make_skill(src / "alpha")
    make_skill(src / "zulu", body)
    registry = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError):
        skill_import.install_skills(registry, src, ["*"])
    assert not (registry.root / "alpha").exists()


def test_bad_manifest_does_not_block_preview_or_single_valid_install(tmp_path):
    source = tmp_path / "checkout"
    make_skill(source / "group/bad", b"---\n- invalid metadata\n---\n")
    make_skill(source / "group/good")
    assert [s["path"] for s in skill_import.preview_skills(source)["skills"]] == ["group/bad", "group/good"]
    registry = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError, match="group/bad"):
        skill_import.install_skills(registry, source, ["*"])
    assert not (registry.root / "good").exists()
    assert skill_import.install_skills(registry, source, ["group/good"])["results"][0]["status"] == "installed"


@pytest.mark.parametrize("archive", [False, True])
def test_selected_lfs_pointer_requires_materialized_resource(tmp_path, archive):
    extension = b"ext-0-demo sha256:" + b"2" * 64 + b"\n" if archive else b""
    pointer = (b"version https://git-lfs.github.com/spec/v1\n" + extension
               + b"oid sha256:" + b"1" * 64 + b"\nsize 1234\n")
    source = tmp_path / "repo"
    alpha = make_skill(source / "alpha")
    # A document can legitimately explain the pointer format.
    (alpha / "example.md").write_bytes(b"# Example\n" + pointer)
    asset = make_skill(source / "bravo") / "asset.bin"
    asset.write_bytes(pointer)
    if archive:
        source = make_zip(tmp_path / "repo.zip", [
            (p.relative_to(source).as_posix(), p.read_bytes()) for p in source.rglob("*") if p.is_file()])
    registry = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError, match=r"Git LFS.*folder or ZIP"):
        skill_import.install_skills(registry, source, ["*"])
    assert not (registry.root / "alpha").exists()
    assert not (registry.root / "bravo").exists()
    assert skill_import.install_skills(registry, source, ["alpha"])["results"][0]["status"] == "installed"


def test_conflicting_source_ids_require_exact_selection(tmp_path):
    src = tmp_path / "repo"
    make_skill(src / "left/shared")
    make_skill(src / "right/shared", b"# Right")
    registry = SkillRegistry(tmp_path / "library")
    for selected in ([], ["*"], ["shared"], ["left/shared", "right/shared"]):
        with pytest.raises(HarnessError):
            skill_import.install_skills(registry, src, selected)
    assert not (registry.root / "shared").exists()
    assert skill_import.install_skills(registry, src, ["right/shared"])["results"] == [
        {"id": "shared", "path": "right/shared", "status": "installed"}]
    assert (registry.root / "shared/SKILL.md").read_bytes() == b"# Right"


def test_protected_or_oversized_source_rolls_back_all_prepared_skills(tmp_path, monkeypatch):
    src = tmp_path / "repo"
    make_skill(src / "alpha")
    later = make_skill(src / "bravo")
    protected = later / ".env.local"
    protected.write_bytes(b"private")
    reg = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError):
        skill_import.install_skills(reg, src, ["*"])
    protected.unlink()
    (later / "large.bin").write_bytes(bytes(200))
    monkeypatch.setattr(skill_import, "MAX_BYTES", 128)
    with pytest.raises(HarnessError):
        skill_import.install_skills(reg, src, ["*"])
    assert not (reg.root / "alpha").exists()
    assert not (reg.root / "bravo").exists()


def test_linked_local_asset_and_linked_destination_are_preserved(tmp_path):
    src = make_skill(tmp_path / "alpha")
    external = tmp_path / "private.txt"
    external.write_text("private")
    try:
        os.link(external, src / "data.txt")
    except OSError:
        pytest.skip("Hard links are unavailable on this filesystem")
    reg = SkillRegistry(tmp_path / "library")
    with pytest.raises(HarnessError):
        skill_import.install_skills(reg, src, ["."])
    (src / "data.txt").unlink()
    existing = make_skill(reg.root / "alpha", b"# private")
    (existing / "SKILL.md").unlink()
    os.link(external, existing / "SKILL.md")
    assert skill_import.install_skills(reg, src, ["."])["results"][0]["status"] == "conflict"
    assert external.read_bytes() == b"private"


def test_simultaneous_imports_publish_one_complete_skill(tmp_path):
    src = make_skill(tmp_path / "example")
    registry = SkillRegistry(tmp_path / "library")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(skill_import.install_skills, registry, src, ["."]) for _ in range(4)]
    statuses = [task.result()["results"][0]["status"] for task in futures]
    assert sorted(statuses) == ["identical", "identical", "identical", "installed"]
    assert registry.get("example")["name"] == "Test skill"


def test_catalog_capacity_preserves_registered_skills_and_allows_retries(tmp_path, monkeypatch):
    source = make_skill(tmp_path / "source/zulu")
    registry = SkillRegistry(tmp_path / "library")
    monkeypatch.setattr(registry, "max_skills", 1, raising=False)
    skill_import.install_skills(registry, source, ["."])
    assert skill_import.install_skills(registry, source, ["."])["results"][0]["status"] == "identical"
    (registry.root / "zulu/edit.txt").write_text("private")
    assert skill_import.install_skills(registry, source, ["."])["results"][0]["status"] == "conflict"
    other = make_skill(tmp_path / "source/alpha")
    with pytest.raises(HarnessError):
        skill_import.install_skills(registry, other, ["."])
    assert not (registry.root / "alpha").exists()
    assert list(registry.skills) == ["zulu"]
