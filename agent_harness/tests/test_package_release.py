import hashlib
import json
import shutil
import subprocess
import zipfile

import pytest

from scripts import package_release as release


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name, contents in {
        "pyproject.toml": '[project]\nversion = "1.0"\n',
        "agent_harness/__main__.py": "",
        "scripts/check_environment.py": "",
        "AGENTS.md": "Public guide",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode())
    manifest = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}
    (root / release.MANIFEST).write_text(json.dumps(manifest))
    return root


def test_package_verifies_reviewed_files_and_ignores_local_additions(source):
    (source / "docs").mkdir()
    (source / "docs/draft.md").write_text("Private draft")
    (source / "AGENTS.md").write_text("Local instructions")
    with pytest.raises(ValueError, match="integrity"):
        release.package(source)
    (source / "AGENTS.md").write_bytes(b"Public guide")
    with zipfile.ZipFile(release.package(source)) as archive:
        assert "BAT-Agent/docs/draft.md" not in archive.namelist()
        assert archive.read("BAT-Agent/AGENTS.md") == b"Public guide"


@pytest.mark.skipif(not shutil.which("git"), reason="Maintainer workflow requires Git")
def test_refresh_uses_staged_bytes_and_only_named_additions(source):
    def git(*args):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)

    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Seed")
    (source / "docs").mkdir()
    (source / "docs/guide.md").write_bytes(b"Reviewed guide")
    (source / "docs/draft.md").write_bytes(b"Unlisted draft")
    (source / "config").mkdir()
    (source / "config/model.yaml").write_bytes(b"local")
    git("add", ".")
    (source / "docs/guide.md").write_bytes(b"Working draft")
    before = (source / release.MANIFEST).read_bytes()
    with pytest.raises(ValueError, match="Invalid release"):
        release.refresh_manifest(source, ["config/model.yaml"])
    assert (source / release.MANIFEST).read_bytes() == before
    manifest = release.refresh_manifest(source, ["docs/guide.md"])
    assert manifest["docs/guide.md"] == hashlib.sha256(b"Reviewed guide").hexdigest()
    assert "docs/draft.md" not in manifest
    assert "config/model.yaml" not in manifest
    with pytest.raises(ValueError, match="integrity"):
        release.package(source)
