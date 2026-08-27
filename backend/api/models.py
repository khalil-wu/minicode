"""Pydantic request/response models shared across route modules."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Chat request payload."""
    message: str = Field(min_length=1)
    # Durable owner used by the REST admission fence. An empty value keeps
    # simple anonymous calls independent from conversation-scoped turns.
    conversation_id: str = Field(default="", min_length=0)
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

    conversation_id: str
    file_name: str
    doc_id: str
    artifact_id: str
    attachment: dict[str, Any]


class OpenAISettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    auth_header: bool = False
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    model_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_labels: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int = 0
    wire_api: str = "responses"
    proxy_mode: Literal["inherit", "direct"] = "inherit"
    prompt_cache_retention: str = ""
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    image_mode: Literal["disabled", "inherit", "custom"] = "inherit"
    image_api_key: str = ""
    image_base_url: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"
    image_quality: str = ""


class AnthropicSettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    auth_header: bool = False
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    model_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_labels: dict[str, str] = Field(default_factory=dict)
    max_tokens: int = 8000
    thinking_budget: int = 0
    proxy_mode: Literal["inherit", "direct"] = "inherit"
    image_mode: Literal["disabled", "inherit", "custom"] = "inherit"
    image_api_key: str = ""
    image_base_url: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"
    image_quality: str = ""


class CustomSettingsPayload(BaseModel):
    display_name: str = ""
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    auth_header: bool = False
    base_url: str = ""
    model: str = ""
    small_fast_model: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_source: str = ""
    model_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_labels: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str = ""
    responses_reasoning_summary: str = "off"
    max_tokens: int = 0
    thinking_budget: int = 0
    wire_api: str = "chat"
    proxy_mode: Literal["inherit", "direct"] = "inherit"
    prompt_cache_retention: str = ""
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    image_mode: Literal["disabled", "inherit", "custom"] = "inherit"
    image_api_key: str = ""
    image_base_url: str = ""
    image_model: str = ""
    image_size: str = "1024x1024"
    image_quality: str = ""


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


class PersonalizationUpdateRequest(BaseModel):
    instructions: str = ""


class SkillInstallRequest(BaseModel):
    skill_name: str = Field(min_length=1)


class SkillImportRequest(BaseModel):
    source_path: str = Field(min_length=1)


class PluginStateUpdateRequest(BaseModel):
    enabled: bool


class PluginImportRequest(BaseModel):
    source_path: str = Field(min_length=1)
    overwrite: bool = False
    marketplace: str = Field(default="local", min_length=1)


class PluginValidateRequest(BaseModel):
    source_path: str = Field(min_length=1)


class PluginPackageRequest(BaseModel):
    source_path: str = Field(min_length=1)
    output_dir: str | None = None


class PluginMarketplaceRequest(BaseModel):
    name: str = Field(min_length=1)
    source: dict[str, Any]


class PluginMarketplaceRefreshRequest(BaseModel):
    name: str = Field(min_length=1)


class PluginInstallRequest(BaseModel):
    source_path: str = ""
    overwrite: bool = False
    marketplace: str = Field(default="local", min_length=1)
    plugin_name: str | None = Field(default=None, min_length=1)
    refresh_marketplace: bool = True


class LLMModelsRefreshResponse(BaseModel):
    provider: str
    provider_id: str
    models: list[str] = Field(default_factory=list)
    selected_model: str = ""
    proxy_mode: Literal["inherit", "direct"] = "inherit"
    source: str = "preset"
    source_message: str = ""
    status_code: int | None = None
    failure_kind: str = ""
    retryable: bool = False
    message: str = ""
    hint: str = ""
    model_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    configured_reasoning_effort: str = ""
    effective_reasoning_effort: str = ""
    reasoning_effort_supported: bool = False
    context_window: int = 0
    context_window_source: str = ""
    context_window_verified: bool = False
    max_context_window: int = 0
    max_context_window_source: str = ""
    max_context_window_verified: bool = False
    max_output_tokens: int = 0
    max_output_tokens_source: str = ""
    max_output_tokens_verified: bool = False
    default_reasoning_effort: str = ""
    default_reasoning_summary: str = ""
    generated_at: float = Field(default_factory=time.time)


class LLMCheckResponse(BaseModel):
    ok: bool
    provider: str
    provider_id: str
    base_url: str = ""
    model: str = ""
    wire_api: str = ""
    proxy_mode: Literal["inherit", "direct"] = "inherit"
    generation_kind: Literal["text", "image"] = "text"
    has_api_key: bool = False
    status_code: int | None = None
    model_discovery_ok: bool | None = None
    generation_ok: bool | None = None
    failure_kind: str = ""
    retryable: bool = False
    message: str = ""
    hint: str = ""
    image_generation_ok: bool | None = None
    image_status_code: int | None = None
    image_failure_kind: str = ""
    image_retryable: bool = False
    image_message: str = ""
    image_hint: str = ""
    image_model: str = ""
    models: list[str] = Field(default_factory=list)
    generated_at: float = Field(default_factory=time.time)
