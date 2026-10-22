"""LLM settings and MCP configuration routes."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from backend.agent.instruction_discovery import INSTRUCTIONS_MAX_BYTES, clear_guideline_cache
from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.config import SETTINGS_FILE, SettingsError, load_config_layer_stack
from backend.hooks.runtime import run_config_change_hook
from backend.mcp.config_file import MCP_CONFIG_FILE
from backend.services.llm_provider_service import (
    check_llm_connection,
    refresh_llm_models,
)
from backend.services.feature_flag_settings_service import (
    FeatureFlagSettingsError,
    get_feature_flag_settings,
    update_feature_flag_settings,
)
from backend.services.llm_settings_service import (
    LLMSettingsServiceError,
    delete_provider_history,
    get_llm_settings,
    get_mcp_config,
    update_llm_settings,
    update_mcp_config,
)
from backend.services.bedrock_service import BedrockSetupRequest
from backend.application_network import ApplicationNetworkDenied

from . import _state
from .routes_health import get_mcp_status
from backend.services.llm_provider_helpers import (
    _fetch_anthropic_models,
    _check_openai_compatible_generation,
    _fetch_openai_compatible_models,
)
from .models import (
    FeatureFlagsUpdateRequest,
    LLMCheckResponse,
    LLMModelsRefreshResponse,
    LLMProviderHistoryDeleteRequest,
    LLMSettingsUpdateRequest,
    MCPConfigUpdateRequest,
    PersonalizationUpdateRequest,
)

router = APIRouter()
USER_INSTRUCTIONS_FILE = get_minicode_config_home_dir() / "INSTRUCTIONS.md"


@router.get("/api/llm/bedrock/discover")
def discover_bedrock_api(response: Response):
    from backend.llm.bedrock import discover_bedrock
    response.headers["Cache-Control"] = "no-store"
    return discover_bedrock()


@router.get("/api/llm/bedrock/gov-cloud-check")
def bedrock_gov_cloud_check_api(response: Response):
    from backend.services.bedrock_service import bedrock_gov_cloud_check
    response.headers["Cache-Control"] = "no-store"
    try:
        return bedrock_gov_cloud_check()
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/llm/bedrock/setup")
async def setup_bedrock_api(request: BedrockSetupRequest):
    from backend.services.bedrock_service import setup_bedrock
    from botocore.exceptions import BotoCoreError
    from backend.hooks.runtime import ConfigChangeHookBlocked
    try:
        payload, config = await setup_bedrock(request)
    except ApplicationNetworkDenied as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, SettingsError, BotoCoreError, ConfigChangeHookBlocked) as error:
        raise HTTPException(400, str(error)) from error
    if _state.bootstrap is not None:
        _state.bootstrap.config = config
    _state.invalidate_status_cache()
    return payload


class HookStateUpdate(BaseModel):
    key: str
    enabled: bool | None = None
    trusted_hash: str | None = None


class HookSettingsUpdate(BaseModel):
    workspace_root: str = ""
    updates: list[HookStateUpdate] = Field(default_factory=list)


@router.get("/api/settings/hooks")
def get_hook_settings_api(response: Response, workspace_root: str = "") -> dict[str, Any]:
    from backend.services.hook_settings_service import hook_settings
    response.headers["Cache-Control"] = "no-store"
    return hook_settings(workspace_root)


@router.get("/api/settings/open-target")
async def get_settings_open_target_api(
    response: Response,
    kind: Literal["config", "hook", "data"],
    workspace_root: str = "",
    hook_key: str | None = None,
    scope_id: str | None = None,
    session_id: str = "",
    conversation_id: str = "",
) -> dict[str, str]:
    """Resolve an application's registered settings action, never a caller path."""
    from pathlib import Path
    from backend.config import DATA_ROOT
    from backend.services.hook_settings_service import hook_settings

    response.headers["Cache-Control"] = "no-store"
    if kind == "config":
        if scope_id is None:
            target = SETTINGS_FILE
        else:
            from backend.services.agent_config_settings_service import agent_config_target
            try:
                target = await asyncio.to_thread(agent_config_target, workspace_root, scope_id)
            except ValueError as exc:
                raise HTTPException(404, detail=str(exc)) from exc
            if "workspace_root" in target and (session_id or conversation_id):
                from backend.api import _state
                if not session_id:
                    raise HTTPException(422, "An editor session owner is required.")
                session = _state.ws_manager.get_session(session_id)
                if session is None:
                    raise HTTPException(404, "Session is not connected.")
                if conversation_id:
                    if session.active_conversation_id != conversation_id:
                        raise HTTPException(409, "Workspace conversation owner is stale.")
                    conversation = await asyncio.to_thread(session.conversation_repo.get_conversation_summary, conversation_id)
                    if conversation is None or conversation.archived:
                        raise HTTPException(404, "The target conversation is not available.")
                    bound = conversation.execution_cwd or conversation.worktree_path or conversation.workspace_root
                    if not bound or bound.startswith("file:") or Path(bound).resolve() != Path(workspace_root).resolve():
                        raise HTTPException(409, "Workspace execution owner is stale.")
                session.editor_config_files[(str(Path(workspace_root).resolve()), target["path"])] = target["path"]
            return target
    elif kind == "data":
        target = DATA_ROOT
    else:
        hooks = await asyncio.to_thread(hook_settings, workspace_root)
        hook = next((entry for entry in hooks["hooks"] if entry["key"] == hook_key), None)
        if hook is None:
            raise HTTPException(404, detail="Hook is no longer registered; reload its configuration.")
        target = Path(hook["source_path"])
    return {"path": str(target.resolve())}


@router.put("/api/settings/hooks")
async def update_hook_settings_api(request: HookSettingsUpdate) -> dict[str, Any]:
    from backend.hooks.runtime import ConfigChangeHookBlocked
    from backend.services.hook_settings_service import update_hook_settings
    try:
        return await update_hook_settings([update.model_dump(exclude_none=True) for update in request.updates], request.workspace_root)
    except (ValueError, ConfigChangeHookBlocked) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class AgentConfigUpdate(BaseModel):
    workspace_root: str = ""
    scope_id: str = "user"
    expected_version: str | None = None
    approval_policy: Literal["untrusted", "on-request", "never"] | None = None
    sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    web_search: Literal["disabled", "cached", "indexed", "live"] | None = None
    network_access: bool | None = None
    model_verbosity: Literal["low", "medium", "high"] | None = None
    model_reasoning_summary: Literal["auto", "concise", "detailed", "none"] | None = None


@router.get("/api/settings/agent-config")
def get_agent_config_settings_api(response: Response, workspace_root: str = "") -> dict[str, Any]:
    from backend.services.agent_config_settings_service import agent_config_settings
    response.headers["Cache-Control"] = "no-store"
    try:
        return agent_config_settings(workspace_root)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, detail=str(exc)) from exc


@router.put("/api/settings/agent-config")
async def update_agent_config_settings_api(request: AgentConfigUpdate) -> dict[str, Any]:
    from backend.config_requirements import RequirementViolation
    from backend.hooks.runtime import ConfigChangeHookBlocked
    from backend.services.agent_config_settings_service import update_agent_config_settings
    try:
        return await update_agent_config_settings(request.model_dump(exclude_unset=True, exclude={"workspace_root", "scope_id", "expected_version"}), request.workspace_root,
                                                   scope_id=request.scope_id, expected_version=request.expected_version)
    except (ValueError, RequirementViolation, ConfigChangeHookBlocked) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/settings/config-layers")
def get_config_layers_api(response: Response) -> dict[str, Any]:
    """Expose provenance and managed constraints without returning secret values."""
    response.headers["Cache-Control"] = "no-store"
    return load_config_layer_stack().to_payload()


# ── LLM settings ──


@router.get("/api/llm/settings")
def get_llm_settings_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return get_llm_settings()


@router.put("/api/llm/settings")
async def update_llm_settings_api(request: LLMSettingsUpdateRequest) -> dict[str, Any]:
    try:
        result = await update_llm_settings(
            request,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except LLMSettingsServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        _state.bootstrap.config = result.config
    return result.payload


@router.delete("/api/llm/provider-history")
async def delete_llm_provider_history_api(request: LLMProviderHistoryDeleteRequest) -> dict[str, Any]:
    try:
        result = await delete_provider_history(
            request,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except LLMSettingsServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if _state.bootstrap is not None:
        _state.bootstrap.config = result.config
    return result.payload


# ── Feature flags ──


@router.get("/api/settings/feature-flags")
def get_feature_flags_settings_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return get_feature_flag_settings()


@router.put("/api/settings/feature-flags")
async def update_feature_flags_settings_api(request: FeatureFlagsUpdateRequest, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await update_feature_flag_settings(
            request.flags,
            settings_file=SETTINGS_FILE,
            config_change_hook=run_config_change_hook,
        )
    except FeatureFlagSettingsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    config = result.pop("_config", None)
    if config is not None and _state.bootstrap is not None:
        _state.bootstrap.config = config
    _state.invalidate_status_cache()
    return result


# ── Personalization ──


def _personalization_payload() -> dict[str, Any]:
    with file_mutation_locks([USER_INSTRUCTIONS_FILE]):
        try:
            instructions = USER_INSTRUCTIONS_FILE.read_text(encoding="utf-8") if USER_INSTRUCTIONS_FILE.is_file() else ""
        except (OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"无法读取自定义指令：{exc}") from exc
    return {
        "instructions": instructions,
        "path": str(USER_INSTRUCTIONS_FILE),
        "exists": USER_INSTRUCTIONS_FILE.is_file(),
        "max_bytes": INSTRUCTIONS_MAX_BYTES,
    }


@router.get("/api/settings/personalization")
def get_personalization_settings_api(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return _personalization_payload()


@router.put("/api/settings/personalization")
def update_personalization_settings_api(
    request: PersonalizationUpdateRequest,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    instructions = request.instructions.replace("\r\n", "\n")
    if len(instructions.encode("utf-8")) > INSTRUCTIONS_MAX_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"自定义指令不能超过 {INSTRUCTIONS_MAX_BYTES // 1024} KiB",
        )
    try:
        with file_mutation_locks([USER_INSTRUCTIONS_FILE]):
            if instructions.strip():
                atomic_write_text(USER_INSTRUCTIONS_FILE, instructions)
            else:
                USER_INSTRUCTIONS_FILE.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法保存自定义指令：{exc}") from exc
    clear_guideline_cache()
    return _personalization_payload()


# ── LLM model refresh / check ──


@router.post("/api/llm/models/refresh", response_model=LLMModelsRefreshResponse)
async def refresh_llm_models_api(request: LLMSettingsUpdateRequest) -> LLMModelsRefreshResponse:
    try:
        result = await refresh_llm_models(
            request,
            fetch_anthropic_models=_fetch_anthropic_models,
            fetch_openai_models=_fetch_openai_compatible_models,
        )
    except (SettingsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LLMModelsRefreshResponse(**result)


@router.post("/api/llm/check", response_model=LLMCheckResponse)
async def check_llm_connection_api(request: LLMSettingsUpdateRequest) -> LLMCheckResponse:
    try:
        result = await check_llm_connection(
            request,
            fetch_anthropic_models=_fetch_anthropic_models,
            fetch_openai_models=_fetch_openai_compatible_models,
            check_openai_generation=_check_openai_compatible_generation,
        )
    except (SettingsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LLMCheckResponse(**result)


# ── MCP configuration ──


@router.get("/api/mcp/config")
async def get_mcp_config_api(response: Response) -> dict[str, Any]:
    """Read the local .mcp.json file for the Settings center."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_mcp_config(MCP_CONFIG_FILE)
    except LLMSettingsServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put("/api/mcp/config")
async def update_mcp_config_api(request: MCPConfigUpdateRequest, response: Response) -> dict[str, Any]:
    """Validate, backup, save, and optionally reload the local .mcp.json file."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await update_mcp_config(
            request,
            config_file=MCP_CONFIG_FILE,
            config_change_hook=run_config_change_hook,
        )
    except LLMSettingsServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    _state.invalidate_status_cache()
    if request.reload and _state.bootstrap is not None and _state.bootstrap.mcp_manager is not None:
        reload_managers = getattr(_state.bootstrap, "reload_mcp_managers", None)
        if callable(reload_managers):
            await reload_managers()
        else:
            await _state.bootstrap.mcp_manager.reload_config()

    return {
        **result,
        "mcp": get_mcp_status(),
    }
