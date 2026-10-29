from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.provider_attempt import ProviderAttempt
from backend.secret_redaction import redact_secrets


def provider_stream_error_progress(
    message: str,
    *,
    attempt: ProviderAttempt,
    progress_id: str,
    error_message: str,
    detail: str = "",
    retry_attempt: int | None = None,
    max_retries: int | None = None,
    retry_after_ms: int | None = None,
    status_code: int | None = None,
    provider_error_code: str = "",
) -> AgentEvent:
    """A retry failure is public evidence, separate from request liveness."""
    return AgentEvent.progress(
        message,
        stage="status",
        status="info",
        id=f"{progress_id}:error:{attempt.span_id}:reconnecting",
        operation_id=progress_id,
        phase="recover",
        provider_state="reconnecting",
        error_message=error_message,
        detail=detail,
        retry_attempt=retry_attempt,
        max_retries=max_retries,
        retry_after_ms=retry_after_ms,
        status_code=status_code,
        provider_error_code=provider_error_code,
        visibility="timeline",
    )


def provider_failure_detail_progress(message: str, *, attempt: ProviderAttempt, progress_id: str) -> AgentEvent:
    """Retain the provider's real, redacted denial reason in process detail."""
    detail = redact_secrets(message)
    return AgentEvent.progress(
        "模型请求详情", stage="status", status="failed", phase="recover",
        id=f"{progress_id}:error:{attempt.span_id}:terminal", operation_id=progress_id,
        provider_state="failed", error_message=detail, detail=detail, visibility="timeline",
    )


def short_text(value: Any, limit: int = 72) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit - 1]}..."


def agent_progress(
    message: str,
    *,
    stage: str,
    status: str = "running",
    id: str,
    phase: str,
    label: str,
    summary: str | None = None,
    detail: str = "",
    visibility: str = "timeline",
    count: int | None = None,
    group_id: str = "",
    step_id: str = "",
    iteration_id: str = "",
    ephemeral: bool = False,
) -> AgentEvent:
    return AgentEvent.progress(
        message,
        stage=stage,
        status=status,
        id=id,
        phase=phase,
        label=label,
        summary=summary or message,
        detail=detail,
        visibility=visibility,
        count=count,
        group_id=group_id,
        step_id=step_id,
        iteration_id=iteration_id,
        ephemeral=ephemeral,
    )
