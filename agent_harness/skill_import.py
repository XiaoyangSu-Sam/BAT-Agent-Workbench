"""Discover and install portable Skill folders without executing their contents."""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
import threading
import zipfile
import zlib

import yaml

from .errors import HarnessError
from .runtime.paths import sensitive, text_file


MAX_ENTRIES = 10_000
MAX_BYTES = 64 * 1024 * 1024
_PUBLISH_LOCK = threading.Lock()
_LFS_POINTER = re.compile(
    rb"version (?:https://(?:git-lfs|hawser)\.github\.com/spec/v1|http://git-media\.io/v/2)\r?\n"
    rb"(?:ext-[0-9]-[A-Za-z0-9_-]+ [^\r\n]+\r?\n)*oid sha256:[0-9a-f]{64}\r?\nsize [0-9]+(?:\r?\n)?"
)


def _invalid(message):
    return HarnessError("invalid_skill_import", message, "tool")


def validate_relative_path(value: str, *, allow_protected=False) -> None:
    """Accept only portable, unprotected, slash-delimited relative names."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _invalid("Invalid source path.")
    parts = value.split("/")
    if len(parts) > 64:
        raise _invalid("Skill source paths may be at most 64 levels deep.")
    for part in parts:
        if (not part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                or (not allow_protected and sensitive(part.lower())) or len(part.encode("utf-16-le")) > 510
                or any(ord(c) < 32 or c in '<>:"\\|?*' for c in part)):
            raise _invalid("The source contains an unsafe or protected path.")


class _Budget:
    def __init__(self):
        self.entries = 0
        self.size = 0

    def entry(self):
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise _invalid("The Skill source contains too many entries.")

    def copy(self, stream, target):
        with target.open("xb") as output:
            while chunk := stream.read(128 * 1024):
                self.size += len(chunk)
                if self.size > MAX_BYTES:
                    raise _invalid("The Skill source exceeds the size limit.")
                output.write(chunk)


def _kind(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise _invalid("Links and junctions are not supported in Skill sources.")
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        return "file"
    raise _invalid("Skill sources must contain ordinary files and directories.")


@contextmanager
def _open_regular(path):
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(before, opened):
            raise _invalid("The source changed or contains a linked file.")
        yield stream


def _check_ancestors(path):
    for parent in reversed(path.parents):
        try:
            if _kind(parent) != "dir":
                raise _invalid("Expected an ordinary parent directory.")
        except FileNotFoundError:
            break


def _extract(archive, root):
    # Track implicit directories too, so extraction behaves identically on Windows and POSIX.
    nodes = {}
    explicit = set()
    budget = _Budget()
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise _invalid("The ZIP contains too many entries.")
    blocked_roots = set()
    public_members = []
    total_bytes = 0
    for item in members:
        # Windows PowerShell 5.1 Compress-Archive stores backslash separators.
        portable_name = item.orig_filename.replace("\\", "/")
        directory = portable_name.endswith("/")
        name = portable_name.removesuffix("/")
        validate_relative_path(name, allow_protected=True)
        mode = stat.S_IFMT(item.external_attr >> 16)
        if (item.flag_bits & 1 or item.external_attr & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                or (directory and item.file_size)):
            raise _invalid("The ZIP contains an encrypted, linked, or unsupported entry.")
        if name in explicit:
            raise _invalid("The ZIP contains duplicate entries.")
        explicit.add(name)
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            node = (prefix, index < len(parts) or directory)
            key = prefix.casefold()
            if key not in nodes:
                budget.entry()
                nodes[key] = node
            elif nodes[key] != node:
                raise _invalid("The ZIP contains colliding file or directory names.")
        if sensitive(name.lower()):
            blocked_roots.update((root / name).parents)
        else:
            public_members.append((item, name, directory))
            total_bytes += item.file_size
            if total_bytes > MAX_BYTES:
                raise _invalid("The ZIP exceeds the Skill source size limit.")
    for item, name, directory in public_members:
        target = root / name
        if directory:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as stream:
                budget.copy(stream, target)
    return blocked_roots


@contextmanager
def _source(source, subdir):
    try:
        path = Path(source).absolute()
        if ".." in path.parts or sensitive(path.name.lower()):
            raise _invalid("Invalid Skill source.")
        _check_ancestors(path)
        with ExitStack() as stack:
            blocked_roots = set()
            if _kind(path) == "dir":
                base, name = path, path.name
            else:
                if path.suffix.lower() != ".zip":
                    raise _invalid("Choose a Skill folder or a ZIP file.")
                if path.stat().st_size > MAX_BYTES + 16 * 1024 * 1024:
                    raise _invalid("The ZIP exceeds the Skill source size limit.")
                base = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="bat-skill-source-")))
                with _open_regular(path) as data, zipfile.ZipFile(data) as archive:
                    blocked_roots = _extract(archive, base)
                name = path.stem
            if subdir not in ("", "."):
                validate_relative_path(subdir)
                for part in subdir.split("/"):
                    base /= part
                    if _kind(base) != "dir":
                        raise _invalid("The selected Skill subdirectory is not a folder.")
                name = base.name
            yield base, name, blocked_roots
    except (OSError, TypeError, ValueError, RecursionError, RuntimeError, EOFError,
            zipfile.BadZipFile, zlib.error) as exc:
        raise _invalid("The Skill source could not be read or installed.") from exc


def _children(path, budget):
    children = []
    with os.scandir(path) as entries:
        for entry in entries:
            budget.entry()
            children.append(Path(entry.path))
    return sorted(children, key=lambda child: (child.name.casefold(), child.name))


def _check_manifest(manifest):
    if _kind(manifest) != "file":
        raise _invalid("SKILL.md must be a regular file.")
    _, body = text_file(manifest)
    if not body.strip():
        raise _invalid("Skill instructions cannot be empty.")
    if body.startswith("---"):
        parts = body.split("---", 2)
        try:
            metadata = yaml.safe_load(parts[1]) if len(parts) == 3 else None
        except (yaml.YAMLError, ValueError, RecursionError) as exc:
            raise _invalid("Invalid SKILL.md frontmatter.") from exc
        if not isinstance(metadata, dict) or any(
            key in metadata and not isinstance(metadata[key], str) for key in ("name", "description")
        ):
            raise _invalid("SKILL.md requires a metadata mapping with text names and descriptions.")


def _discover(base, root_name):
    skills = []
    budget = _Budget()

    def visit(folder):
        children = _children(folder, budget)
        relative = folder.relative_to(base).as_posix()
        if any(child.name == "SKILL.md" for child in children):
            name = root_name if relative == "." else folder.name
            validate_relative_path(name)
            skills.append({"id": name, "path": relative})
            return
        for child in children:
            if sensitive(child.name.lower()):
                continue
            validate_relative_path(child.relative_to(base).as_posix())
            if _kind(child) == "dir":
                visit(child)

    visit(base)
    return skills


def preview_skills(source: Path, subdir=""):
    with _source(source, subdir) as (base, name, _):
        return {"skills": _discover(base, name)}


def _selected(skills, selected):
    if not isinstance(selected, list) or not selected or not all(isinstance(p, str) for p in selected):
        raise _invalid("Select a Skill path or explicitly choose all Skills.")
    if selected != ["*"]:
        paths = set(selected)
        skills = [skill for skill in skills if skill["path"] in paths]
        if len(skills) != len(selected):
            raise _invalid("Select exact, unique paths from the Skill preview.")
    if not skills:
        raise _invalid("The selected source contains no Skills.")
    if len({skill["id"].casefold() for skill in skills}) != len(skills):
        raise _invalid("Several selected Skills have the same folder name. Select one exact path per name.")
    return skills


def _stage(source, target, budget, relative=Path(".")):
    target.mkdir()
    names = set()
    for child in _children(source, budget):
        name = child.name
        path = relative / name
        validate_relative_path(path.as_posix())
        if name.casefold() in names:
            raise _invalid("The selected Skill contains colliding file or directory names.")
        names.add(name.casefold())
        if _kind(child) == "dir":
            _stage(child, target / name, budget, path)
        else:
            size = child.stat().st_size
            if size + budget.size > MAX_BYTES:
                raise _invalid("The Skill source exceeds the size limit.")
            with _open_regular(child) as stream:
                if size < 1024:
                    if _LFS_POINTER.fullmatch(stream.read(1024)):
                        raise _invalid(f"{path.as_posix()} is a Git LFS pointer. Use a folder or ZIP containing the downloaded asset.")
                    stream.seek(0)
                budget.copy(stream, target / name)


def _identical(staged, local):
    try:
        if _kind(local) != "dir":
            return False
        expected = {item.name for item in staged.iterdir()}
        for item in local.iterdir():
            if item.name not in expected:
                return False
            expected.remove(item.name)
            other = staged / item.name
            if other.is_dir():
                if not _identical(other, item):
                    return False
            elif _kind(item) != "file" or other.stat().st_size != item.stat().st_size:
                return False
            else:
                with other.open("rb") as a, _open_regular(item) as b:
                    while chunk := a.read(128 * 1024):
                        if b.read(len(chunk)) != chunk:
                            return False
                    if b.read(1):
                        return False
        return not expected
    except (OSError, HarnessError):
        # Unreadable, linked or changing local content belongs to the existing owner.
        return False


def _publish(staged, destination):
    collisions = [p for p in destination.parent.iterdir() if p.name.casefold() == destination.name.casefold()]
    if not collisions:
        try:
            staged.rename(destination)
            return "installed"
        except OSError:
            if not os.path.lexists(destination):
                raise
            collisions = [destination]
    if len(collisions) == 1 and collisions[0].name == destination.name and _identical(staged, collisions[0]):
        return "identical"
    return "conflict"


def install_skills(registry, source: Path, selected: list[str], subdir=""):
    with _source(source, subdir) as (base, name, blocked_roots):
        choices = _selected(_discover(base, name), selected)
        for skill in choices:
            folder = base / skill["path"]
            if folder in blocked_roots:
                raise _invalid("A selected Skill contains a protected resource.")
            if registry.root.is_relative_to(folder):
                raise _invalid("The destination library cannot be inside a selected Skill.")
        with _PUBLISH_LOCK:
            _check_ancestors(registry.root)
            registry.root.mkdir(parents=True, exist_ok=True)
            if _kind(registry.root) != "dir":
                raise _invalid("The Skill library must be a directory.")
            existing = {item.name.casefold() for item in registry.root.iterdir()}
            new_ids = {skill["id"].casefold() for skill in choices} - existing
            if new_ids:
                registry.refresh()
                if len(registry.skills) + len(new_ids) > registry.max_skills:
                    raise _invalid(f"The Skill catalog supports up to {registry.max_skills} entries.")
            budget = _Budget()
            # Keep staging off the visible catalog and on the destination filesystem.
            scratch_dir = registry.root if os.path.ismount(registry.root) else registry.root.parent
            with tempfile.TemporaryDirectory(dir=scratch_dir, prefix=".bat-import-") as scratch:
                stage = Path(scratch)
                for skill in choices:
                    target = stage / skill["id"]
                    try:
                        _stage(base / skill["path"], target, budget)
                        _check_manifest(target / "SKILL.md")
                    except HarnessError as exc:
                        raise _invalid(f"Could not import Skill '{skill['path']}'. {exc.message}") from exc
                result = [{**skill, "status": _publish(stage / skill["id"], registry.root / skill["id"])}
                          for skill in choices]
            registry.refresh()
    return {"results": result}
