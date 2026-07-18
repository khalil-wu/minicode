from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid


class ProjectServiceError(ValueError):
    pass


@dataclass
class ProjectRecord:
    id: str
    name: str
    path: str
    type: str = "unknown"
    last_opened: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "last_opened": self.last_opened,
        }


def list_projects(projects: list[ProjectRecord]) -> list[ProjectRecord]:
    return projects


def create_project(projects: list[ProjectRecord], *, name: str, path: str, type: str = "unknown") -> ProjectRecord:
    project = ProjectRecord(
        id=str(uuid.uuid4()),
        name=name,
        path=path,
        type=type,
        last_opened=datetime.now().isoformat(),
    )
    projects.append(project)
    return project


def get_project(projects: list[ProjectRecord], project_id: str) -> ProjectRecord:
    for project in projects:
        if project.id == project_id:
            return project
    raise ProjectServiceError("Project not found")


def activate_project(projects: list[ProjectRecord], project_id: str) -> dict[str, Any]:
    project = get_project(projects, project_id)
    project.last_opened = datetime.now().isoformat()
    return {"status": "success", "project_id": project_id}


def delete_project(projects: list[ProjectRecord], project_id: str) -> tuple[list[ProjectRecord], dict[str, Any]]:
    return [project for project in projects if project.id != project_id], {"status": "success", "project_id": project_id}
