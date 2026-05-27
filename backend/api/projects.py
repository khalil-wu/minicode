"""
Projects API

项目管理相关的 API 端点
"""

from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


# 临时存储（实际应该使用数据库）
_projects: List[ProjectInfo] = []


@router.get("")
async def get_projects() -> List[ProjectInfo]:
    """获取所有项目列表"""
    return _projects


@router.post("")
async def create_project(request: ProjectCreateRequest) -> ProjectInfo:
    """创建新项目"""
    import uuid
    from datetime import datetime

    project = ProjectInfo(
        id=str(uuid.uuid4()),
        name=request.name,
        path=request.path,
        type=request.type,
        last_opened=datetime.now().isoformat()
    )
    _projects.append(project)
    return project


@router.get("/{project_id}")
async def get_project(project_id: str) -> ProjectInfo:
    """获取指定项目信息"""
    for project in _projects:
        if project.id == project_id:
            return project
    raise HTTPException(status_code=404, detail="Project not found")


@router.post("/{project_id}/activate")
async def activate_project(project_id: str) -> dict:
    """激活项目"""
    for project in _projects:
        if project.id == project_id:
            from datetime import datetime
            project.last_opened = datetime.now().isoformat()
            return {"status": "success", "project_id": project_id}
    raise HTTPException(status_code=404, detail="Project not found")


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict:
    """删除项目"""
    global _projects
    _projects = [p for p in _projects if p.id != project_id]
    return {"status": "success", "project_id": project_id}
