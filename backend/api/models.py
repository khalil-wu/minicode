"""Pydantic request/response models shared across route modules."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Chat request payload."""
    message: str = Field(min_length=1)
    max_iterations: int = Field(default=10, ge=1, le=50)


class ToolCallRecord(BaseModel):
    """Tool call record payload."""
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str | None = None
    artifact_id: str | None = None
    status: str = "success"


class ChatResponse(BaseModel):
    """Chat response payload."""
    reply: str
    stopped_reason: str
    iterations: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class UploadResponse(BaseModel):
    """Uploaded document metadata for the active session."""

    file_name: str
    doc_id: str
    artifact_id: str
    indexed_chunks: int
    attachment: dict[str, Any]


class OpenAISettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    wire_api: str = "chat"


class AnthropicSettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    max_tokens: int = 8192
    thinking_budget: int = 0


class CustomSettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    available_models: list[str] = Field(default_factory=list)
    reasoning_effort: str = "high"
    max_tokens: int = 8192
    thinking_budget: int = 0
    wire_api: str = "chat"


class LLMSettingsUpdateRequest(BaseModel):
    provider: str = "openai"
    openai: OpenAISettingsPayload = Field(default_factory=OpenAISettingsPayload)
    anthropic: AnthropicSettingsPayload = Field(default_factory=AnthropicSettingsPayload)
    custom: CustomSettingsPayload = Field(default_factory=CustomSettingsPayload)
    confirm_sensitive_change: bool = False


class MCPConfigUpdateRequest(BaseModel):
    content: str = Field(min_length=1)
    reload: bool = True
    confirm_sensitive_change: bool = False


class SkillInstallRequest(BaseModel):
    skill_name: str = Field(min_length=1)


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
