"""Projects API."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.project_service import (
    ProjectRecord,
    ProjectServiceError,
    activate_project as activate_project_record,
    create_project as create_project_record,
    delete_project as delete_project_record,
    get_project as get_project_record,
    list_projects,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectInfo(BaseModel):
    id: str
    name: str
    path: str
    type: str = "unknown"
    last_opened: str | None = None


class ProjectCreateRequest(BaseModel):
    name: str
    path: str
    type: str = "unknown"


_projects: list[ProjectRecord] = []


def _to_project_info(record: ProjectRecord) -> ProjectInfo:
    return ProjectInfo(**record.to_dict())


@router.get("")
async def get_projects() -> list[ProjectInfo]:
    return [_to_project_info(project) for project in list_projects(_projects)]


@router.post("")
async def create_project(request: ProjectCreateRequest) -> ProjectInfo:
    return _to_project_info(
        create_project_record(_projects, name=request.name, path=request.path, type=request.type)
    )


@router.get("/{project_id}")
async def get_project(project_id: str) -> ProjectInfo:
    try:
        return _to_project_info(get_project_record(_projects, project_id))
    except ProjectServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/activate")
async def activate_project(project_id: str) -> dict:
    try:
        return activate_project_record(_projects, project_id)
    except ProjectServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict:
    global _projects
    _projects, result = delete_project_record(_projects, project_id)
    return result
