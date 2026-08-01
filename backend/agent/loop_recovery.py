"""Recovery orchestration extracted from the main agent loop."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.error_withholding import RecoveryStrategy, is_media_size_error
from backend.agent.message import AgentEvent
from backend.agent.recovery_controller import (
    RecoveryController,
    RecoveryDependencies,
    RecoveryProfile,
)
from backend.agent.state import AgentState
from backend.feature_flags import feature_enabled
from backend.llm.base import ToolCallEvent, UsageInfo


logger = logging.getLogger(__name__)
RecoveryOperation = Callable[[AgentState, ContextBuilder], Awaitable[bool]]


async def emergency_compact(state: AgentState, context: ContextBuilder) -> bool:
    """Rewrite oversized history using the context builder's full compaction path."""
    try:
        summary = await context.full_compact(restore_state=state)
        if summary:
            logger.info("[ErrorWithholding] Emergency compaction succeeded")
            return True
    except Exception as exc:
        logger.warning("[ErrorWithholding] Compaction failed: %s", exc)
    return False


async def strip_historical_media(state: AgentState, context: ContextBuilder) -> bool:
    """Remove old image/PDF payloads after a provider media-size rejection."""
    strip = getattr(context, "strip_historical_media", None)
    if not callable(strip):
        return False
    try:
        stats = strip(keep_recent_user_turns=1) or {}
    except Exception as exc:
        logger.debug("strip_historical_media failed: %s", exc)
        return False
    stripped = int(stats.get("images", 0) or 0) + int(stats.get("documents", 0) or 0)
    if stripped <= 0:
        return False
    state.mark_transition(
        "media_size_strip",
        images=int(stats.get("images", 0) or 0),
        documents=int(stats.get("documents", 0) or 0),
        messages=int(stats.get("messages", 0) or 0),
    )
    return True


async def quarantine_external_web_results(state: AgentState, context: ContextBuilder) -> bool:
    """Isolate the latest external web batch after a content-filter rejection."""
    try:
        changed = context.quarantine_latest_external_web_results()
    except Exception as exc:
        logger.debug("quarantine_latest_external_web_results failed: %s", exc)
        return False
    if changed <= 0:
        return False
    state.mark_transition("content_filter_web_quarantine", results=changed)
    return True


async def try_error_withholding_recovery(
    *,
    error_controller: Any,
    classification: Any,
    error_content: str,
    state: AgentState,
    context: ContextBuilder,
    compact: RecoveryOperation,
    strip_media: RecoveryOperation,
    quarantine_web: RecoveryOperation = quarantine_external_web_results,
) -> bool:
    """Try bounded context recovery before exposing a provider error."""
    content_filter = str(classification.provider_error_type or "") == "content_filter"
    withhold_type = "content_filter" if content_filter else classification.error_type
    if not error_controller.is_withholdable(withhold_type, error_content):
        return False

    strategies: list[RecoveryStrategy] = []
    media_size = (
        str(classification.error_type or "") == "media_size"
        or is_media_size_error(error_content)
        or is_media_size_error(classification.error_type)
    )
    if content_filter:
        strategies.append(
            RecoveryStrategy(
                "quarantine_external_web_results",
                "Remove the latest rejected web batch and retry with a different source",
                lambda: quarantine_web(state, context),
            )
        )
    else:
        if not feature_enabled("reactive_compact", True):
            return False
        if state.reactive_compaction_attempted:
            return False
        state.reactive_compaction_attempted = True
    if media_size:
        strategies.append(
            RecoveryStrategy(
                "strip_historical_media",
                "Remove historical image/PDF attachments after media-size rejection",
                lambda: strip_media(state, context),
            )
        )
    if not content_filter:
        strategies.append(
            RecoveryStrategy(
                "emergency_compact",
                "Emergency compaction to reduce context size",
                lambda: compact(state, context),
            )
        )

    withheld = error_controller.withhold(
        error_content,
        "media_size" if media_size and not content_filter else withhold_type,
        strategies=strategies,
    )
    for strategy in withheld.recovery_strategies:
        try:
            # Strategies above are already bound to this state/context. Passing
            # them again raises TypeError, which used to be swallowed as a
            # failed recovery and made every withholding path silently fail.
            if await strategy.try_recover():
                error_controller.record_recovery(strategy.name, True)
                state.mark_transition(
                    f"recovered_{strategy.name}",
                    error_type="media_size" if media_size and not content_filter else withhold_type,
                )
                error_controller.clear()
                return True
        except Exception as exc:
            error_controller.record_recovery(strategy.name, False, str(exc))
    error_controller.clear()
    return False


async def degrade_and_finish(
    *,
    state: AgentState,
    context: ContextBuilder,
    usage: UsageInfo,
    stream_text: Any,
    full_text: str,
    pending_tool_calls: list[ToolCallEvent],
    profile: RecoveryProfile,
    dependencies: RecoveryDependencies,
) -> AsyncIterator[AgentEvent]:
    controller = RecoveryController(
        state=state,
        ctx=context,
        dependencies=dependencies,
    )
    async for event in controller.finish(
        usage=usage,
        stream_text=stream_text,
        full_text=full_text,
        pending_tool_calls=pending_tool_calls,
        profile=profile,
    ):
        yield event
