from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.config import (
    SettingsError,
    delete_llm_provider_history,
    get_llm_settings_payload,
    load_config,
    save_llm_settings,
)
from backend.mcp.config_file import read_mcp_config, write_mcp_config

ConfigChangeHook = Callable[..., Awaitable[None]]


class LLMSettingsServiceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SettingsMutationResult:
    payload: dict[str, Any]
    config: Any | None = None


def get_llm_settings() -> dict[str, Any]:
    return get_llm_settings_payload()


async def update_llm_settings(
    request: Any,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> SettingsMutationResult:
    if not request.confirm_sensitive_change:
        raise LLMSettingsServiceError(
            "LLM settings changes require explicit confirmation.",
            status_code=409,
        )
    saved = save_llm_settings(request.model_dump(exclude_unset=True))
    config = load_config()
    await config_change_hook(source="llm", file_path=str(settings_file))
    return SettingsMutationResult(saved, config=config)


async def delete_provider_history(
    request: Any,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> SettingsMutationResult:
    if not request.confirm_sensitive_change:
        raise LLMSettingsServiceError(
            "Deleting a saved provider configuration requires explicit confirmation.",
            status_code=409,
        )
    try:
        saved = delete_llm_provider_history(request.model_dump(exclude_unset=True))
    except SettingsError as exc:
        raise LLMSettingsServiceError(str(exc), status_code=404) from exc
    config = load_config()
    await config_change_hook(source="llm", file_path=str(settings_file))
    return SettingsMutationResult(saved, config=config)


def get_mcp_config(config_file: Path) -> dict[str, Any]:
    try:
        return read_mcp_config(config_file)
    except ValueError as exc:
        raise LLMSettingsServiceError(str(exc), status_code=400) from exc
    except OSError as exc:
        raise LLMSettingsServiceError(f"Failed to read MCP config: {exc}", status_code=500) from exc


async def update_mcp_config(
    request: Any,
    *,
    config_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    if not request.confirm_sensitive_change:
        raise LLMSettingsServiceError(
            "MCP config changes require explicit confirmation.",
            status_code=409,
        )
    try:
        result = write_mcp_config(request.content, config_file)
    except ValueError as exc:
        raise LLMSettingsServiceError(str(exc), status_code=400) from exc
    except OSError as exc:
        raise LLMSettingsServiceError(f"Failed to save MCP config: {exc}", status_code=500) from exc
    await config_change_hook(source="mcp", file_path=str(config_file))
    return result
