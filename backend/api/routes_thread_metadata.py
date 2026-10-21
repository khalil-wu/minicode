"""Local thread sections and durable resource-reference APIs."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from backend.async_cleanup import to_thread_cancel_safe
from backend.services.thread_metadata_service import publish_thread_metadata
from backend.workspace.recent_projects import RecentProjectStore
from . import _state

router = APIRouter(prefix="/api/thread-metadata")


def _session(session_id):
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "The original session is not connected")
    return session


def _repository(session_id):
    return _session(session_id).conversation_repo


@router.get("/{conversation_id}/voice")
def voice_target(conversation_id: str, response: Response, session_id: str = Query(..., min_length=1)):
    from backend.realtime.session import capture_config
    session = _session(session_id)
    if not session.is_connected:
        raise HTTPException(404, "The original session is not connected")
    conversation = session.conversation_repo.get_conversation_summary(conversation_id)
    if conversation is None or conversation.archived:
        raise HTTPException(404, "The destination voice task does not exist or is archived")
    response.headers["Cache-Control"] = "no-store"
    try:
        capability = capture_config(session, conversation_id, credentials=False).capabilities()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"conversationId": conversation_id, "realtime": capability}


async def _mutate(repository, background_tasks, operation):
    try:
        result = await to_thread_cancel_safe(operation)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    background_tasks.add_task(publish_thread_metadata, repository)
    return result


class SectionAppearance(BaseModel):
    icon: str | None = None
    color: str | None = None

    @field_validator("icon", "color")
    @classmethod
    def utf8_field_limit(cls, value):
        if value is not None and len(value.encode("utf-8")) > 64:
            raise ValueError("Section appearance fields must not exceed 64 bytes")
        return value


class SectionWrite(BaseModel):
    name: str = Field(min_length=1)
    appearance: SectionAppearance | None = None
    after_section_id: str | None = None


class SectionMove(BaseModel):
    kind: Literal["thread", "project"]
    id: str = Field(min_length=1)
    section_id: str | None
    before_item_key: str | None = None


class SectionOrder(BaseModel):
    moved_id: str
    before_id: str | None
    visible_ids: list[str]
    section_id: str | None = None


@router.get("/sections")
def sections(response: Response, session_id: str = Query(..., min_length=1)):
    response.headers["Cache-Control"] = "no-store"
    return {"sections": _repository(session_id).thread_sections.list()}


@router.post("/sections")
async def create_section(request: SectionWrite, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    section = await _mutate(repository, background_tasks, lambda: repository.thread_sections.create(request.name,
        appearance=request.appearance.model_dump() if request.appearance is not None else None, after_section_id=request.after_section_id))
    return {"section": section}


@router.patch("/sections/{section_id}")
async def update_section(section_id: str, request: SectionWrite, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    updates = {"appearance": request.appearance.model_dump() if request.appearance is not None else None} if "appearance" in request.model_fields_set else {}
    return {"section": await _mutate(repository, background_tasks, lambda: repository.thread_sections.update(section_id, request.name, **updates))}


@router.delete("/sections/{section_id}")
async def delete_section(section_id: str, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    await _mutate(repository, background_tasks, lambda: repository.thread_sections.delete(section_id))
    return {}


@router.post("/sections/move")
async def move_section_item(request: SectionMove, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    if request.kind == "thread":
        operation = lambda: repository.thread_sections.move_thread(request.id, request.section_id,
            before_item_key=request.before_item_key)
    else:
        if not any(project.path == request.id for project in RecentProjectStore().list()):
            raise HTTPException(404, "Project not found")
        operation = lambda: repository.thread_sections.move_project(request.id, request.section_id, before_item_key=request.before_item_key)
    await _mutate(repository, background_tasks, operation)
    return {}


@router.post("/sections/reorder")
async def reorder_sections(request: SectionOrder, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    operation = (lambda: repository.thread_sections.reorder(request.moved_id, request.before_id, request.visible_ids)) if request.section_id is None else (
        lambda: repository.thread_sections.reorder_items(request.section_id, request.moved_id, request.before_id, request.visible_ids))
    await _mutate(repository, background_tasks, operation)
    return {}


class ResourceWrite(BaseModel):
    attachment_type: str
    identity_key: str
    payload: Any


@router.get("/resources/owners")
def resource_owners(response: Response, attachment_type: str, identity_key: str, archived: bool | None = None,
    cursor: str | None = None, limit: int = Query(100, ge=1, le=100), session_id: str = Query(..., min_length=1)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return _repository(session_id).thread_resources.owners(attachment_type, identity_key, archived=archived, cursor=cursor, limit=limit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{conversation_id}/resources")
def list_resources(conversation_id: str, response: Response, cursor: str | None = None,
    limit: int = Query(100, ge=1, le=100), session_id: str = Query(..., min_length=1)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return _repository(session_id).thread_resources.list(conversation_id, cursor=cursor, limit=limit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{conversation_id}/resources/{resource_id}/pull-request")
async def linked_pull_request(conversation_id: str, resource_id: str, response: Response,
    session_id: str = Query(..., min_length=1)):
    from backend.services.thread_pull_request_service import linked_pull_request_status

    response.headers["Cache-Control"] = "no-store"
    return await linked_pull_request_status(_session(session_id), conversation_id, resource_id)


@router.post("/{conversation_id}/resources")
async def add_resource(conversation_id: str, request: ResourceWrite, background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    return await _mutate(repository, background_tasks, lambda: repository.thread_resources.add(conversation_id,
        request.attachment_type, request.identity_key, request.payload))


@router.delete("/{conversation_id}/resources")
async def remove_resource(conversation_id: str, attachment_type: str, identity_key: str,
    background_tasks: BackgroundTasks, session_id: str = Query(..., min_length=1)):
    repository = _repository(session_id)
    await _mutate(repository, background_tasks, lambda: repository.thread_resources.remove(conversation_id, attachment_type, identity_key))
    return {}
