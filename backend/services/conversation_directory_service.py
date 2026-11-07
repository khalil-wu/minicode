"""The receiving session owns both the directory query and its workspace scope."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationDirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    limit: int | None = Field(None, ge=0)
    sort_key: Literal["created_at", "updated_at", "recency_at", "section_position"] | None = None
    sort_direction: Literal["asc", "desc"] | None = None
    model_providers: list[str] | None = None
    source_kinds: list[str] | None = None
    conversation_type: Literal["main", "side_chat"] | None = None
    excluded_thread_ids: list[str] | None = Field(None, max_length=100)
    originators: list[str] | None = None
    archived: bool | None = None
    section_id: str | None = None
    project_id: str | None = None
    cwd: str | list[str] | None = None
    use_state_db_only: bool = False
    search_term: str | None = None
    parent_thread_id: str | None = None
    ancestor_thread_id: str | None = None
    attached_to_thread_id: str | None = None
    sidebar_scope: Literal["recent", "project"] | None = None
    sidebar_project: str | None = None


def conversation_directory_page(session, request: ConversationDirectoryRequest):
    query = request.model_dump(exclude_unset=True)
    if request.sidebar_scope is not None:
        if request.sidebar_scope == "project" and not request.sidebar_project:
            raise ValueError("sidebar_project is required for a project page")
        session.conversation_repo.directory.ensure_current()
        page = session.conversation_repo.directory.sidebar_page(cursor=request.cursor, limit=request.limit or 25,
            project_key=request.sidebar_project if request.sidebar_scope == "project" else None)
        session._directory_known_ids.update(item["id"] for item in page["data"])
        return page
    if request.project_id is not None:
        from backend.workspace.recent_projects import RecentProjectStore

        if not any(project.path == request.project_id for project in RecentProjectStore().list()):
            raise ValueError(f"project not found: {request.project_id}")
    page = session.conversation_repo.directory.page(query)
    session._directory_known_ids.update(item["id"] for item in page["data"])
    return page
