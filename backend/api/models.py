"""Pydantic request/response models shared across route modules."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Chat request payload."""
    message: str = Field(min_length=1)
    # Caller-owned iteration bound. Omitting the field inherits the configured
    # server value; zero explicitly means no host iteration ceiling.
    max_iterations: int | None = Field(default=None, ge=0)


class ToolCallRecord(BaseModel):
    """Tool call record payload."""
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str | None = None
    artifact_id: str | None = None
    status: str = "success"
    error_kind: str | None = None
    user_summary: str | None = None
    developer_detail: str | None = None
    recoverable: bool = True
    projection: str | None = None
    model_observation: str | None = None


class ChatResponse(BaseModel):
    """Chat response payload."""
    reply: str
    stopped_reason: str
    status: Literal["completed", "partial", "cancelled", "failed"] = "completed"
    errors: list[str] = Field(default_factory=list)
    iterations: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class UploadResponse(BaseModel):
    """Uploaded document metadata for the active session."""

    file_name: str
    doc_id: str
    artifact_id: str
    attachment: dict[str, Any]


class OpenAISettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int = 0
    wire_api: str = "responses"
    responses_stateful_continuation: bool = False
    prompt_cache_retention: str = ""
    reasoning_effort_levels: list[str] = Field(default_factory=list)


class AnthropicSettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    max_tokens: int = 8000
    thinking_budget: int = 0


class CustomSettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int = 0
    thinking_budget: int = 0
    wire_api: str = "chat"
    responses_stateful_continuation: bool = False
    prompt_cache_retention: str = ""
    reasoning_effort_levels: list[str] = Field(default_factory=list)


class LLMSettingsUpdateRequest(BaseModel):
    provider: str = "custom"
    openai: OpenAISettingsPayload = Field(default_factory=OpenAISettingsPayload)
    anthropic: AnthropicSettingsPayload = Field(default_factory=AnthropicSettingsPayload)
    custom: CustomSettingsPayload = Field(default_factory=CustomSettingsPayload)
    confirm_sensitive_change: bool = False


class LLMProviderHistoryDeleteRequest(BaseModel):
    provider: str = "custom"
    provider_id: str = ""
    base_url: str = ""
    model: str = ""
    wire_api: str = ""
    clear_api_key: bool = True
    confirm_sensitive_change: bool = False


class MCPConfigUpdateRequest(BaseModel):
    content: str = Field(min_length=1)
    reload: bool = True
    confirm_sensitive_change: bool = False


class FeatureFlagsUpdateRequest(BaseModel):
    flags: dict[str, bool | None] = Field(default_factory=dict)


class SkillInstallRequest(BaseModel):
    skill_name: str = Field(min_length=1)


class PluginStateUpdateRequest(BaseModel):
    enabled: bool


class PluginImportRequest(BaseModel):
    source_path: str = Field(min_length=1)
    overwrite: bool = False


class PluginValidateRequest(BaseModel):
    source_path: str = Field(min_length=1)


class PluginPackageRequest(BaseModel):
    source_path: str = Field(min_length=1)
    output_dir: str | None = None


class LLMModelsRefreshResponse(BaseModel):
    provider: str
    provider_id: str
    models: list[str] = Field(default_factory=list)
    selected_model: str = ""
    source: str = "preset"
    source_message: str = ""
    generated_at: float = Field(default_factory=time.time)


class LLMCheckResponse(BaseModel):
    ok: bool
    provider: str
    provider_id: str
    base_url: str = ""
    model: str = ""
    wire_api: str = ""
    has_api_key: bool = False
    status_code: int | None = None
    message: str = ""
    hint: str = ""
    models: list[str] = Field(default_factory=list)
    generated_at: float = Field(default_factory=time.time)
