"""LLM settings and MCP configuration routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response

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
            config_change_hook=run_config_change_hook,
        )
    except (SettingsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config = result.pop("_config", None)
    if config is not None:
        if _state.bootstrap is not None:
            _state.bootstrap.config = config
        _state.invalidate_status_cache()
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
