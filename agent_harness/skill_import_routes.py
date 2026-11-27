"""Preview a Skill source and import selected folders through the workbench or chat."""
import asyncio
import json
import secrets
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, Field

from .errors import HarnessError
from .runtime.paths import inside, sensitive
from .skill_import import install_skills, preview_skills
from .skill_repository import SkillRepository


MAX_UPLOAD = 64 * 1024 * 1024


def _chat_rows(rows):
    page = []
    size = 0
    for row in rows:
        size += len(json.dumps(row))
        if size > 12000 and page:
            break
        page.append(row)
    return page


@dataclass
class Preview:
    resources: ExitStack
    owner: str
    workspace: Path
    source: Path
    subdir: str
    skills: list[dict]
    expires: float
    workspace_source: bool


async def run_worker(fn, *args):
    # Keep the busy guard and source snapshot alive until the worker has finished.
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class SkillImports:
    def __init__(self, settings, registry):
        self.settings = settings
        self.registry = registry
        self.repository = SkillRepository(settings.state_dir)
        self.previews: dict[str, Preview] = {}
        self.ui_busy = False

    def close(self):
        while self.previews:
            self.previews.popitem()[1].resources.close()

    def _expire(self):
        for ticket, entry in list(self.previews.items()):
            if entry.expires <= time.monotonic() or entry.workspace != self.settings.workspace:
                self.previews.pop(ticket).resources.close()

    def configure(self, url, subdir):
        result = self.repository.configure(url, subdir)
        self.close()
        return result

    def preview(self, owner, source, path="", upload=None):
        self._expire()
        with ExitStack() as resources:
            subdir = ""
            if source == "repository":
                if path:
                    raise HarnessError("invalid_source", "The repository is selected in Skills settings.")
                selected, subdir = resources.enter_context(self.repository.snapshot())
            elif source == "upload":
                folder = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="bat-skills-")))
                selected = folder / path  # The upload route validates the filename.
                selected.write_bytes(upload)
            elif source == "workspace" and path and not Path(path).is_absolute() and not PureWindowsPath(path).drive:
                selected = inside(self.settings.workspace, path, True)
            elif source == "folder" and owner == "workbench" and path:
                selected = self.settings.workspace / Path(path)
                if sensitive(str(selected).lower()):
                    raise HarnessError("invalid_source", "Choose a source outside protected directories.")
                if not selected.is_dir():
                    raise HarnessError("invalid_source", "Select an existing Skill source folder.")
            else:
                raise HarnessError("invalid_source", "Choose a configured repository or a workspace-relative folder or ZIP.")
            result = preview_skills(selected, subdir)
            ticket = secrets.token_urlsafe(18)
            while len(self.previews) >= 8:
                self.previews.pop(next(iter(self.previews))).resources.close()
            self.previews[ticket] = Preview(resources.pop_all(), owner, self.settings.workspace, selected,
                                           subdir, result["skills"], time.monotonic() + 900, source == "workspace")
            return {"ticket": ticket, **result}

    def _entry(self, owner, ticket):
        self._expire()
        entry = self.previews.get(ticket)
        if entry is None or entry.owner != owner:
            raise HarnessError("skill_preview_expired", "Preview the source again to select Skills.")
        return entry

    def chat_preview(self, owner, source="repository", path="", ticket="", query="", offset=0):
        if not ticket:
            ticket = self.preview(owner, source, path)["ticket"]
        skills = self._entry(owner, ticket).skills
        term = query.casefold()
        matches = [row for row in skills if term in (row["id"] + " " + row["path"]).casefold()]
        page = _chat_rows(matches[offset:offset + 25])
        end = offset + len(page)
        return {"ticket": ticket, "skills": page, "matches": len(matches), "source_total": len(skills),
                "next_offset": end if end < len(matches) else None}

    def apply(self, owner, ticket, selected):
        entry = self._entry(owner, ticket)
        available = {row["path"] for row in entry.skills}
        paths = [row["path"] for row in entry.skills] if selected == ["*"] else selected
        if not paths or any(path not in available for path in paths):
            raise HarnessError("invalid_selection", "Choose one or more paths from this preview.")
        if entry.workspace_source:
            inside(entry.workspace, entry.source, True)
        result = install_skills(self.registry, entry.source, paths, entry.subdir)
        self.previews.pop(ticket).resources.close()
        return result

    def chat_apply(self, owner, ticket, selected):
        result = self.apply(owner, ticket, selected)
        rows = result["results"]
        if len(json.dumps(rows)) > 12000:
            # Retain destination ids and exact counts for bulk runs with long source paths.
            counts = {key: sum(r["status"] == key for r in rows) for key in ("installed", "identical", "conflict")}
            compact = [{"id": r["id"], "status": r["status"]} for r in sorted(rows, key=lambda r: r["status"] != "conflict")]
            page = _chat_rows(compact)
            return {"counts": counts, "results": page, "omitted": len(rows) - len(page)}
        return result


class SourceSettings(BaseModel):
    url: str = Field(max_length=2048)
    subdir: str = Field(default="", max_length=1024)


class SourcePreview(BaseModel):
    source: Literal["repository", "folder"]
    path: str = Field(default="", max_length=4096)


class SourceSelection(BaseModel):
    ticket: str = Field(min_length=1, max_length=128)
    selected: list[str] = Field(min_length=1, max_length=10000)


def register_skill_import_routes(app, manager):
    imports = manager.imports

    @contextmanager
    def operation():
        if manager.busy():
            raise HarnessError("workspace_busy", "Wait for the current operation to finish.")
        imports.ui_busy = True
        try:
            yield
        finally:
            imports.ui_busy = False

    @app.get("/api/skill-source")
    async def get_source():
        return imports.repository.settings()

    @app.put("/api/skill-source")
    async def set_source(value: SourceSettings):
        with operation():
            return await run_worker(imports.configure, value.url, value.subdir)

    @app.post("/api/skill-import/preview")
    async def preview(value: SourcePreview):
        with operation():
            return await run_worker(imports.preview, "workbench", value.source, value.path)

    @app.post("/api/skill-import/upload")
    async def upload(request: Request, name: str):
        filename = PureWindowsPath(name)
        if (filename.name != name or filename.suffix.lower() != ".zip" or len(name) > 180
                or filename.is_reserved() or any(ord(c) < 32 or c in '<>:"|?*' for c in name)):
            raise HarnessError("invalid_archive", "Choose a ZIP file with a plain filename.")
        with operation():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_UPLOAD:
                    raise HarnessError("archive_too_large", "Choose a ZIP file no larger than 64 MiB.")
                data.extend(chunk)
            return await run_worker(imports.preview, "workbench", "upload", name, data)

    @app.post("/api/skill-import/apply")
    async def apply(value: SourceSelection):
        with operation():
            return await run_worker(imports.apply, "workbench", value.ticket, value.selected)
