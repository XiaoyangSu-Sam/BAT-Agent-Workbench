"""Read portable Skills from the one Git source configured on this machine."""
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from .errors import HarnessError
from .runtime.paths import sensitive
from .skill_import import MAX_BYTES, MAX_ENTRIES, validate_relative_path


def _invalid_url():
    return HarnessError("invalid_skill_repository",
                        "Enter an HTTPS or SSH Git clone address without embedded credentials.", "configuration")


def _repository_name(url):
    if (not isinstance(url, str) or len(url) > 4096 or
            any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in url)):
        raise _invalid_url()
    if "://" in url:
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in ("https", "ssh") or not parsed.hostname or parsed.port == 0 or
                    not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*|[0-9a-fA-F:]*:[0-9a-fA-F:]+", parsed.hostname) or
                    parsed.password is not None or
                    (parsed.username is not None and
                     (parsed.scheme != "ssh" or not re.fullmatch(r"[\w.][\w.-]*", parsed.username)))):
                raise ValueError()
        except ValueError:
            raise _invalid_url() from None
        path = unquote(parsed.path)
    else:
        scp = re.fullmatch(r"(?:[\w.][\w.-]*@)?([\w][\w.-]*|\[[0-9A-Fa-f:]+\]):([^:]+)", url)
        if not scp or (len(scp[1]) == 1 and "@" not in url) or scp[2].startswith("-"):
            raise _invalid_url()
        path = scp[2]
    if not path or "\\" in path or any(ord(char) < 32 for char in path):
        raise _invalid_url()
    name = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    try:
        validate_relative_path(name)
    except (HarnessError, UnicodeError):
        raise _invalid_url() from None
    return name


def _validated(url, subdir):
    if url == "":
        return {"url": "", "subdir": ""}
    _repository_name(url)
    if not isinstance(subdir, str):
        raise HarnessError("invalid_skill_subdir", "Use a relative repository folder.", "configuration")
    if subdir == ".":
        subdir = ""
    if subdir:
        try:
            validate_relative_path(subdir)
        except (HarnessError, UnicodeError):
            raise HarnessError("invalid_skill_subdir", "Use a relative, unprotected repository folder.",
                               "configuration") from None
    return {"url": url, "subdir": subdir}


def _git(directory, *args, data=b"", output=subprocess.DEVNULL):
    # Keep the user's credential helper and SSH agent; never use a checkout or its config.
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("GIT_TRACE") or name == "GIT_CURL_VERBOSE":
            del env[name]
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(name, None)
    env.update(GIT_CEILING_DIRECTORIES=str(directory.parent), GIT_ALLOW_PROTOCOL="https:ssh",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_NO_REPLACE_OBJECTS="1")
    subprocess.run(["git", "-c", f"core.hooksPath={directory / 'no-hooks'}", *args],
                   cwd=directory, env=env, input=data, stdout=output, stderr=subprocess.DEVNULL,
                   timeout=120, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _snapshot_error():
    return HarnessError("skill_repository_unavailable",
                        "Could not read the configured Git repository. Check Git access and the repository folder.",
                        "tool")


def _export(directory, url, subdir, archive_path):
    _git(directory, "clone", "--bare", "--no-local", "--depth=1", "--single-branch", "--no-tags",
         "--no-recurse-submodules", "--template=", "--", url, "objects.git")
    git_dir = "--git-dir=objects.git"
    tree = f"HEAD:{subdir}" if subdir else "HEAD"
    with tempfile.TemporaryFile(dir=directory) as listing:
        _git(directory, git_dir, "ls-tree", "-rlz", tree, output=listing)
        if listing.tell() > 16 * 1024 * 1024:
            raise HarnessError("skill_repository_too_large", "Repository file listing exceeds the import limit.", "tool")
        listing.seek(0)
        records = listing.read().rstrip(b"\0").split(b"\0")
    blobs = []
    total = 0
    for record in records:
        if not record:
            continue
        header, filename = record.split(b"\t", 1)
        mode, kind, oid, size_text = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise HarnessError("unsafe_skill_repository",
                               "The configured folder contains a symbolic link or submodule.", "tool")
        relative = filename.decode("utf-8")
        path = f"{subdir}/{relative}" if subdir else relative
        validate_relative_path(path, allow_protected=True)
        size = int(size_text)
        if sensitive(path.lower()):
            # The importer checks these names inside selected Skills and ignores wrapper siblings.
            oid, size = None, 0
        total += size
        if len(blobs) >= MAX_ENTRIES or total > MAX_BYTES:
            raise HarnessError("skill_repository_too_large", "Repository contents exceed the import limit.", "tool")
        blobs.append((oid, size, path))

    # Raw blobs retain binary bytes, line endings and files marked export-ignore.
    with tempfile.TemporaryFile(dir=directory) as content, ZipFile(archive_path, "x") as archive:
        _git(directory, git_dir, "cat-file", "--batch", data=b"".join(oid + b"\n" for oid, _, _ in blobs if oid),
             output=content)
        content.seek(0)
        for oid, size, path in blobs:
            if oid is None:
                archive.writestr(path, b"")
                continue
            if content.readline() != oid + b" blob " + str(size).encode("ascii") + b"\n":
                raise _snapshot_error()
            blob = content.read(size)
            if len(blob) != size or content.read(1) != b"\n":
                raise _snapshot_error()
            archive.writestr(path, blob)


class SkillRepository:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).resolve()
        self.file = self.state_dir / "skill-repository.json"

    def settings(self):
        try:
            config = json.loads(self.file.read_text(encoding="utf-8"))
            return _validated(config["url"], config["subdir"])
        except FileNotFoundError:
            return {"url": "", "subdir": ""}
        except (OSError, ValueError, TypeError, KeyError):
            raise HarnessError("invalid_skill_repository", "Could not read the saved Skill source.",
                               "configuration") from None

    def configure(self, url: str, subdir: str):
        config = _validated(url, subdir)
        temp = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_dir, delete=False) as out:
                temp = Path(out.name)
                json.dump(config, out)
            os.replace(temp, self.file)
        except OSError:
            raise HarnessError("invalid_skill_repository", "Could not save the Skill source.", "configuration") from None
        finally:
            if temp:
                temp.unlink(missing_ok=True)
        return config

    @contextmanager
    def snapshot(self):
        config = self.settings()
        if not config["url"]:
            raise HarnessError("skill_repository_not_configured", "Configure a Skill repository in the workbench.", "tool")
        with tempfile.TemporaryDirectory(prefix="skill-repo-", dir=self.state_dir) as temp:
            try:
                source = Path(temp) / (_repository_name(config["url"]) + ".zip")
                with tempfile.TemporaryDirectory(dir=temp) as work:
                    _export(Path(work), config["url"], config["subdir"], source)
            except (OSError, ValueError, subprocess.SubprocessError):
                raise _snapshot_error() from None
            yield source, config["subdir"]
