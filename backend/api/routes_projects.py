"""Local named projects and their explicitly selected source folders."""
from __future__ import annotations
from pathlib import Path
from uuid import uuid4
from typing import Any, Literal
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator
from backend.async_cleanup import to_thread_cancel_safe
from backend.workspace.recent_projects import RecentProjectStore
from backend.workspace.trust import is_workspace_trusted
from backend.workspace.trust import trust_created_task_directory
from backend.workspace.delivery import run_delivery_command
from backend.atomic_io import canonical_file_path_key
from backend.services.thread_metadata_service import publish_project_metadata as _publish
from . import _state

router = APIRouter()


class IconProjectMarker(BaseModel):
    kind: Literal["icon"]
    icon: Literal["bar-chart", "book", "brain", "currency-dollar", "customize", "desk-globe", "dumbbell", "edit", "flask", "folder", "function", "globe", "graduation-cap", "health", "heart", "kettlebell", "logs", "lotus", "music", "palette", "paw", "plane", "plant", "popcorn", "scale", "stethoscope", "suitcase", "terminal", "wrench", "writing"]


class EmojiProjectMarker(BaseModel):
    kind: Literal["emoji"]
    emoji: str = Field(min_length=1)


class ProjectAppearance(BaseModel):
    color: Literal["black", "blue", "green", "orange", "pink", "purple", "red", "yellow"]
    marker: IconProjectMarker | EmojiProjectMarker = Field(discriminator="kind")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    source_folders: list[str] = Field(default_factory=list)
    appearance: ProjectAppearance | None = None


class ProjectUpdate(BaseModel):
    path: str
    name: str | None = Field(default=None, min_length=1)
    source_folders: list[str] | None = None
    pinned: bool | None = None
    appearance: ProjectAppearance | None = None

    @model_validator(mode="after")
    def nonnull_updates(self):
        for field in ("name", "source_folders", "pinned"):
            if field in self.model_fields_set and getattr(self, field) is None: raise ValueError(f"{field} cannot be null")
        return self


def _accepted_folders(folders: list[str]) -> list[str]:
    accepted = []
    for folder in folders:
        path = Path(folder).expanduser().resolve()
        if not path.is_dir(): raise HTTPException(409, f"Source folder does not exist: {path}")
        if not is_workspace_trusted(path): raise HTTPException(403, f"Source folder is not trusted: {path}")
        accepted.append(str(path))
    return accepted


async def _create_managed_workspace(store: RecentProjectStore) -> str:
    root = store._store_path.parent / "projects" / str(uuid4()) / "workspace"
    await to_thread_cancel_safe(lambda: root.mkdir(parents=True))
    await run_delivery_command(root, ["git", "init", "-b", "main"])
    await to_thread_cancel_safe(trust_created_task_directory, root)
    return str(root.resolve())


@router.get("/api/projects")
def list_projects(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"projects": [project.to_dict() for project in RecentProjectStore().list()]}


@router.post("/api/projects")
async def create_project(request: ProjectCreate) -> dict[str, Any]:
    name = request.name.strip()
    if not name: raise HTTPException(422, "Project name is required.")
    folders = _accepted_folders(request.source_folders)
    store = RecentProjectStore()
    managed_root = await _create_managed_workspace(store) if not folders else None
    try:
        project = await to_thread_cancel_safe(store.create, name, folders, request.appearance.model_dump(exclude_none=True) if request.appearance else None,
            managed_workspace_root=managed_root)
    except ValueError as error: raise HTTPException(409, str(error)) from error
    return {"project": project.to_dict(), **await _publish()}


@router.patch("/api/projects")
async def update_project(request: ProjectUpdate) -> dict[str, Any]:
    updates = request.model_dump(exclude_unset=True, exclude={"path"})
    store = RecentProjectStore()
    if "name" in updates:
        updates["name"] = updates["name"].strip()
        if not updates["name"]: raise HTTPException(422, "Project name is required.")
    if "source_folders" in updates: updates["source_folders"] = _accepted_folders(updates["source_folders"])
    if updates.get("source_folders") == []:
        project = next((project for project in store.list() if canonical_file_path_key(project.path) == canonical_file_path_key(request.path)), None)
        if project is None: raise HTTPException(404, "Project not found.")
        if project.managed_workspace_root is None: updates["managed_workspace_root"] = await _create_managed_workspace(store)
    try: project = await to_thread_cancel_safe(store.update, request.path, updates)
    except ValueError as error: raise HTTPException(404, str(error)) from error
    return {"project": project.to_dict(), **await _publish()}
