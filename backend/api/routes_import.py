"""Local external-agent import and its persisted, opt-in sync consumer."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.async_cleanup import to_thread_cancel_safe
from backend.services.external_agent_import import ExternalAgentImportService
from . import _state


router = APIRouter()
logger = logging.getLogger(__name__)
_service: ExternalAgentImportService | None = None
_sync_task: asyncio.Task | None = None


def import_service() -> ExternalAgentImportService:
    global _service
    if _service is None:
        _service = ExternalAgentImportService()
    return _service


async def refresh_import_consumers(result: dict, *, publish: bool) -> list[str]:
    """Use the same live catalogs after manual import and background sync."""
    changed = {item["item_type"] for item in result["results"] if item["status"] == "success"}
    errors = []
    bootstrap = _state.bootstrap
    if bootstrap is not None:
        try:
            if "settings" in changed:
                await to_thread_cancel_safe(bootstrap.refresh_config)
            if changed & {"skills", "plugins"} and bootstrap.skill_manager is not None:
                await to_thread_cancel_safe(bootstrap.skill_manager.discover)
            if changed & {"mcp", "plugins"}:
                await bootstrap.reload_mcp_managers()
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(str(error))
    if publish:
        from backend.ws.handlers.misc import handle_commands_list, handle_skills_list
        from backend.ws.handlers.workspace import handle_workspace_recent
        expected_root = (import_service().state_dir / "conversations").resolve()
        for session in _state.ws_manager.iter_sessions():
            if not session.is_connected or session.conversation_repo.storage_root != expected_root:
                continue
            try:
                if "sessions" in changed:
                    await session.send_conversation_list()
                if changed & {"sessions", "projects"}:
                    await handle_workspace_recent(session, {"type": "workspace.recent"})
                if changed & {"skills", "plugins", "commands"}:
                    await handle_skills_list(session, {"type": "skills.list"})
                    await handle_commands_list(session, {"type": "commands.list"})
            except (OSError, ValueError, RuntimeError) as error:
                errors.append(str(error))
    _state.invalidate_status_cache()
    if errors:
        await import_service().record_refresh_errors(result["history"]["id"], errors)
    return errors


class ImportDetectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    workspace_roots: list[str] = Field(default_factory=list)
    provider_ids: list[Literal["claude", "cursor"]] = Field(default_factory=lambda: ["claude", "cursor"])
    include_home: bool = True
    max_session_age_days: int = Field(30, ge=1)
    max_sessions: int = Field(50, ge=1, le=1000)


class ImportSelectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    detection_id: str = Field(min_length=1)
    item_ids: list[str] = Field(min_length=1)
    workspace_root: str | None = None


class ImportSyncRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool
    item_types: list[str]


@router.get("/api/settings/import")
async def get_import_settings(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return await to_thread_cancel_safe(import_service().state)


@router.post("/api/settings/import/detect")
async def detect_external_agents(request: ImportDetectionRequest) -> dict:
    try:
        return await to_thread_cancel_safe(import_service().detect, **request.model_dump())
    except (OSError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/settings/import")
async def import_external_agents(request: ImportSelectionRequest) -> dict:
    try:
        result = await import_service().import_items(request.detection_id, request.item_ids, workspace_root=request.workspace_root)
    except (OSError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    result["setup_errors"] = await refresh_import_consumers(result, publish=False)
    return result


@router.put("/api/settings/import/sync")
async def update_import_sync(request: ImportSyncRequest) -> dict:
    try:
        return await import_service().set_sync(**request.model_dump())
    except (OSError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


async def _sync_loop():
    while True:
        await asyncio.sleep(30)
        try:
            results = await import_service().sync_connected()
            for result in results:
                await refresh_import_consumers(result, publish=True)
        except (OSError, ValueError) as error:
            # A background import failure stays inspectable in Import settings.
            service = import_service()
            await service.record_sync_error(str(error))
            logger.error("External-agent automatic import failed: %s", error)


async def start_import_sync() -> None:
    global _sync_task
    _sync_task = asyncio.create_task(_sync_loop(), name="external-agent-import-sync")


async def stop_import_sync() -> None:
    global _sync_task
    if _sync_task is None:
        return
    _sync_task.cancel()
    try:
        await _sync_task
    except asyncio.CancelledError:
        pass
    _sync_task = None
