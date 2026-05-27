"""
Agent Loop — Single-loop + Recovery-ladder architecture.

Inspired by Claude Code's agent harness pattern:
  1. Context Pipeline  (before the call)
  2. Streaming Execution (during the call)
  3. Recovery Paths    (after the call)
  4. Termination Conditions (when to stop)
  5. State Threading   (across iterations)

The model decides: tool_calls → execute → loop; no tool_calls → done.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.policies import (
    DefaultGroundedReplyPolicy,
    DefaultRealtimeSearchPolicy,
    DefaultReflectionPolicy,
    DefaultStreamRetryPolicy,
)
from backend.hooks.manager import HookEvent, HookManager, HookResult, get_hook_manager
from backend.agent.progress import (
    agent_progress as _agent_progress,
    short_text as _short,
)
from backend.agent.state import AgentState
from backend.agent.tool_execution import (
    execute_tool_batch as _execute_tool_batch,
    generate_diff as _generate_diff,
)
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)
from backend.llm.errors import classify_llm_error
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_POST_COMPACTION_HARD_LIMIT = 0.98
LLM_STREAM_TIMEOUT_SECONDS = 120.0

# ── Session Context ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class AgentLoopSessionContext:
    """Per-session runtime dependencies bag."""

    skill_manager: Any | None = None
    vector_memory: Any | None = None
    permission_context: PermissionContext | None = None
    workspace_root: Path | None = None
    session_id: str = ""
    task_id: str = ""
    task_manager: Any | None = None
    background_manager: Any | None = None
    stream_callback: Any | None = None
    emit_event: Any | None = None
    metadata: dict[str, Any] | None = None


# ── Recovery helpers ───────────────────────────────────────────────────────


def _format_llm_error(message: str, error_type: str) -> str:
    if error_type != "blocked":
        return message
    suffix = (
        " 当前网关拒绝了这次请求。请检查 Base URL 是否需要 /v1、API Format 是否匹配"
        "（OpenAI Chat / Responses / Anthropic Messages）、模型名是否被该网关允许，以及网关侧的风控/白名单。"
    )
    return message if suffix in message else f"{message}{suffix}"


def _timeout_tool_result_reply(state: AgentState) -> str:
    if not state.tool_calls:
        return ""
    # Last resort: show raw tool output snippet so user isn't left empty-handed
    successful = [
        tc for tc in state.tool_calls
        if getattr(tc, "status", "") == "success"
        and str(getattr(tc, "tool_output", "") or "").strip()
    ]
    if not successful:
        return ""
    last_output = str(successful[-1].tool_output).strip()
    if len(last_output) > 600:
        last_output = last_output[:600] + "..."
    return f"模型响应超时，以下是工具获取到的原始信息：\n\n{last_output}"


# ── Context pipeline ───────────────────────────────────────────────────────


async def _manage_context_budget(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
    tool_schemas: list[dict[str, Any]],
) -> AsyncIterator[AgentEvent]:
    """Pre-call context pipeline: check budget, compact if needed."""
    try:
        should_compact = ctx.needs_compaction(state, tool_schemas=tool_schemas)
    except TypeError:
        should_compact = ctx.needs_compaction()

    usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)

    if usage_pct > 0.75 and not should_compact:
        yield AgentEvent.budget_warning(
            bucket="total", percent=round(usage_pct, 3),
            will_compact=usage_pct > 0.85,
        )

    # Emergency compaction
    if usage_pct >= 0.95 and hasattr(ctx, "full_compact"):
        # Hook: pre_compact
        hook_mgr = get_hook_manager()
        if hook_mgr and hook_mgr.has_hooks(HookEvent.PRE_COMPACT):
            await hook_mgr.run_pre_compact()
        summary = ctx.full_compact()
        logger.info("Emergency compaction: %s", summary[:120] if summary else "(empty)")
        yield AgentEvent.context_compacted(summary=summary)
        usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
        if usage_pct >= _POST_COMPACTION_HARD_LIMIT:
            yield AgentEvent.error(
                message="Context still nearly full after emergency compaction. Use /clear or /compact.",
                recoverable=True, error_type="budget",
            )
            state.stopped_reason = "budget_exceeded"
            return

    # Normal compaction
    elif should_compact:
        # Hook: pre_compact
        hook_mgr = get_hook_manager()
        if hook_mgr and hook_mgr.has_hooks(HookEvent.PRE_COMPACT):
            await hook_mgr.run_pre_compact()
        summary = await ctx.compact()
        logger.info("Compaction done: %s", summary[:80] if summary else "(empty)")
        yield AgentEvent.context_compacted(summary=summary)
        usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
        if usage_pct >= _POST_COMPACTION_HARD_LIMIT:
            yield AgentEvent.error(
                message="Context stayed above safety limit after compaction.",
                recoverable=True, error_type="budget",
            )
            state.stopped_reason = "budget_exceeded"
            return


# ── Main loop ──────────────────────────────────────────────────────────────


async def run_agent_loop(
    user_message: str,
    llm: LLMAdapter,
    tool_registry: ToolRegistry,
    artifact_store: ArtifactStore,
    permission_checker: PermissionChecker,
    agent_settings: AgentSettings | None = None,
    token_budget: TokenBudget | None = None,
    context_builder: ContextBuilder | None = None,
    state: AgentState | None = None,
    approval_handler: Callable | None = None,
    skill_manager: Any | None = None,
    vector_memory: Any | None = None,
    permission_context: PermissionContext | None = None,
    session_id: str = "",
    task_id: str = "",
    task_manager: Any | None = None,
    background_manager: Any | None = None,
    stream_callback: Any | None = None,
    emit_event: Any | None = None,
    metadata: dict[str, Any] | None = None,
    session_context: AgentLoopSessionContext | None = None,
) -> AsyncIterator[AgentEvent]:
    """
    Agent Loop — single while-true with recovery ladder.

    The model decides: has tool_calls → execute → loop; no tool_calls → done.
    """
    # ── Phase 1: Setup ─────────────────────────────────────────────────────
    if session_context is not None:
        skill_manager = skill_manager or session_context.skill_manager
        vector_memory = vector_memory or session_context.vector_memory
        permission_context = permission_context or session_context.permission_context
        session_id = session_id or session_context.session_id
        task_id = task_id or session_context.task_id
        task_manager = task_manager or session_context.task_manager
        background_manager = background_manager or session_context.background_manager
        stream_callback = stream_callback or session_context.stream_callback
        emit_event = emit_event or session_context.emit_event
        metadata = metadata or session_context.metadata

    settings = agent_settings or AgentSettings()
    if (
        settings.stream_timeout_seconds == AgentSettings.stream_timeout_seconds
        and LLM_STREAM_TIMEOUT_SECONDS != AgentSettings.stream_timeout_seconds
    ):
        settings = AgentSettings(
            max_iterations=settings.max_iterations,
            compaction_threshold=settings.compaction_threshold,
            stagnation_limit=settings.stagnation_limit,
            history_keep_recent=settings.history_keep_recent,
            fallback_providers=settings.fallback_providers,
            reflection_pass=settings.reflection_pass,
            agent_mode=settings.agent_mode,
            stream_timeout_seconds=LLM_STREAM_TIMEOUT_SECONDS,
            stream_max_attempts=settings.stream_max_attempts,
            stream_retry_delay_seconds=settings.stream_retry_delay_seconds,
            stream_retryable_substrings=settings.stream_retryable_substrings,
            realtime_search_policy=settings.realtime_search_policy,
            grounded_reply_policy=settings.grounded_reply_policy,
            reflection_policy=settings.reflection_policy,
            stream_retry_policy=settings.stream_retry_policy,
        )
    budget = token_budget or TokenBudget()
    ctx = context_builder or ContextBuilder(
        token_budget=budget, agent_settings=settings, vector_memory=vector_memory,
    )
    state = state or AgentState(user_message=user_message, max_iterations=settings.max_iterations)

    # Policies
    realtime_search_policy = settings.realtime_search_policy or DefaultRealtimeSearchPolicy()
    grounded_reply_policy = settings.grounded_reply_policy or DefaultGroundedReplyPolicy()
    reflection_policy = settings.reflection_policy or DefaultReflectionPolicy(settings)
    stream_retry_policy = settings.stream_retry_policy or DefaultStreamRetryPolicy(settings)

    # Tool execution context
    workspace_root = session_context.workspace_root if session_context is not None else None
    if workspace_root is None and state.workspace_context and hasattr(state.workspace_context, 'root_path'):
        workspace_root = state.workspace_context.root_path

    tool_ctx = ToolExecutionContext(
        permission=permission_context or PermissionContext(),
        session_id=session_id, task_id=task_id,
        metadata=dict(metadata or {}), emit_event=emit_event,
        stream_callback=stream_callback, workspace_root=workspace_root,
        task_manager=task_manager, background_manager=background_manager,
        checkpoint_manager=getattr(state, "checkpoint_manager", None),
        conversation_id=getattr(state, "conversation_id", ""),
    )

    full_text = ""

    # ── Skills auto-detect ──
    if skill_manager:
        try:
            for skill_name in skill_manager.auto_detect(user_message):
                if skill_manager.activate(skill_name):
                    state.active_skills.append(skill_name)
                    yield AgentEvent(type="skill_activated",
                                     data={"skill_name": skill_name,
                                           "description": f"自动激活 Skill: {skill_name}"})
        except asyncio.CancelledError:
            state.stopped_reason = "interrupted"
            raise
        except Exception as exc:
            logger.debug("Skills auto-detect failed: %s", exc)

    # Record user message
    ctx.append_user(user_message)

    # ── Hook: user_prompt_submit ──
    hook_mgr = get_hook_manager()
    if hook_mgr and hook_mgr.has_hooks(HookEvent.USER_PROMPT_SUBMIT):
        prompt_hook = await hook_mgr.run_user_prompt_submit(user_message)
        if prompt_hook.has_feedback:
            ctx.append_user(prompt_hook.feedback)

    yield _agent_progress(
        "Reading request and workspace context",
        stage="planning",
        status="running",
        id="agent:orient",
        phase="orienting",
        label="Thinking",
        summary="Reading request and workspace context",
        visibility="debug",
        step_id="orient",
    )

    tool_schemas = tool_registry.get_schemas(
        budget=budget.tool_schemas,
        permission_checker=permission_checker,
        permission_context=tool_ctx.permission,
    )

    # ── Phase 2: Main loop (the kernel) ──────────────────────────────────────
    try:
        while True:
            # Breathing room between iterations (prevents API flooding)
            if state.iterations > 0:
                await asyncio.sleep(0.15)

            # ── Termination: max iterations ──
            if state.iterations >= settings.max_iterations:
                logger.warning("Max iterations reached: %d", settings.max_iterations)
                yield AgentEvent.error(
                    message=f"已达到最大迭代次数限制（{settings.max_iterations}次）。当前进度已保存。",
                    recoverable=True, error_type="budget",
                )
                state.stopped_reason = "max_iterations"
                break

            # ── Termination: stagnation hard stop ──
            if state.blocked_repeat_calls >= settings.stagnation_limit:
                detail = state.get_stagnation_detail()
                logger.warning("Stagnation hard stop: %s", detail)
                yield AgentEvent.error(
                    message=(
                        "已停止：模型连续尝试完全相同的工具调用，系统已拦截。"
                        "请换一种描述方式，或明确指定下一步操作。"
                    ),
                    recoverable=True, error_type="stagnant",
                )
                state.stopped_reason = "stagnation"
                break

            # ── Context pipeline: budget check & compaction ──
            async for ev in _manage_context_budget(ctx, state, budget, tool_schemas):
                yield ev
            if state.stopped_reason:
                break

            # ── Build messages & call LLM ──
            messages = await ctx.build(user_message=user_message, state=state)

            # Inject realtime search hint
            hint = realtime_search_policy.build_system_hint(user_message, tool_schemas, state)
            if hint:
                insert_pos = 1 if messages and messages[0].role == "system" else 0
                messages.insert(insert_pos, LLMMessage(role="system", content=hint))

            yield AgentEvent(type="budget_update",
                             data=ctx.get_budget_snapshot(state=state, tool_schemas=tool_schemas))

            state.iterations += 1
            yield _agent_progress(
                "Choosing the next step",
                stage="planning" if state.iterations == 1 else "status",
                status="running",
                id=f"agent:model:{state.iterations}",
                phase="model",
                label="Thinking",
                summary="Choosing the next step",
                visibility="debug",
                count=state.iterations,
                step_id=f"model:{state.iterations}",
            )

            # ── Stream LLM response (with retry ladder) ──
            full_text = ""
            streamed_text = ""
            pending_tool_calls: list[ToolCallEvent] = []
            usage = UsageInfo()
            stream_attempt = 0
            tool_signal_seen = False

            try:
                while True:
                    should_retry = False
                    stream_iter = llm.stream_chat(messages, tools=tool_schemas).__aiter__()
                    first_event = True
                    while True:
                        timeout = settings.stream_timeout_seconds if first_event else 60.0
                        try:
                            async with asyncio.timeout(timeout):
                                event = await stream_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        first_event = False
                        if event.type == StreamEventType.TEXT_CHUNK:
                            full_text += event.content
                            streamed_text += event.content
                        elif event.type == StreamEventType.THINKING_CHUNK:
                            yield AgentEvent.thinking_chunk(event.content)
                        elif event.type == StreamEventType.IMAGE_CHUNK:
                            yield AgentEvent.image_chunk(event.image_data, event.image_media_type)
                        elif event.type == StreamEventType.TOOL_CALL_START:
                            tool_signal_seen = True
                        elif event.type == StreamEventType.TOOL_CALL_DELTA:
                            tool_signal_seen = True
                        elif event.type == StreamEventType.TOOL_CALL:
                            tool_signal_seen = True
                            pending_tool_calls = event.tool_calls
                        elif event.type == StreamEventType.DONE:
                            usage = event.usage
                        elif event.type == StreamEventType.ERROR:
                            # Recovery ladder: retry transient errors
                            classification = classify_llm_error(event.content)
                            if not full_text and not pending_tool_calls and not classification.fatal:
                                decision = stream_retry_policy.decide_retry(event.content, stream_attempt)
                                if decision.should_retry:
                                    stream_attempt += 1
                                    logger.warning("Retrying stream (%d): %s", stream_attempt, event.content)
                                    yield _agent_progress(
                                        "Retrying model stream",
                                        stage="status",
                                        status="running",
                                        id=f"agent:recover:{state.iterations}:{stream_attempt}",
                                        phase="recover",
                                        label="Recovering",
                                        summary="Retrying model stream",
                                        detail=_short(event.content, 160),
                                        visibility="timeline",
                                        count=stream_attempt,
                                        step_id=f"recover:{state.iterations}",
                                    )
                                    await asyncio.sleep(decision.delay_seconds)
                                    should_retry = True
                                    break
                            # Fatal or non-retryable
                            yield AgentEvent.error(
                                message=_format_llm_error(event.content, classification.error_type),
                                recoverable=not classification.fatal,
                                error_type=classification.error_type,
                            )
                            state.stopped_reason = classification.error_type if classification.fatal else "api_error"
                            if full_text:
                                ctx.append_assistant(full_text)
                            return

                    if should_retry:
                        continue
                    break

            except asyncio.TimeoutError:
                logger.warning("LLM stream timeout: %ss", settings.stream_timeout_seconds)
                timeout_reply = _timeout_tool_result_reply(state)
                if timeout_reply:
                    yield _agent_progress(
                        "Model stream timed out; using tool results",
                        stage="status",
                        status="completed",
                        id=f"agent:timeout:{state.iterations}",
                        phase="recover",
                        label="Recovered",
                        summary="Used available tool results",
                        visibility="timeline",
                        step_id=f"recover:{state.iterations}",
                    )
                    yield AgentEvent.text_chunk(timeout_reply)
                    ctx.append_assistant(timeout_reply)
                    state.reply = timeout_reply
                    state.stopped_reason = "completed"
                    yield AgentEvent.done(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_creation_input_tokens=usage.cache_creation_input_tokens,
                        cache_read_input_tokens=usage.cache_read_input_tokens,
                    )
                    break
                yield _agent_progress(
                    "Model stream timed out",
                    stage="status",
                    status="failed",
                    id=f"agent:timeout:{state.iterations}",
                    phase="recover",
                    label="Stopped",
                    summary="Model stream timed out",
                    visibility="timeline",
                    step_id=f"recover:{state.iterations}",
                )
                yield AgentEvent.error(message="LLM response timed out, please retry.",
                                       recoverable=True, error_type="timeout")
                state.stopped_reason = "timeout"
                if full_text:
                    ctx.append_assistant(full_text)
                return

            except Exception as exc:
                logger.error("LLM call failed: %s", exc, exc_info=True)
                classification = classify_llm_error(exc)
                yield _agent_progress(
                    "Model request failed",
                    stage="status",
                    status="failed",
                    id=f"agent:error:{state.iterations}",
                    phase="recover",
                    label="Stopped",
                    summary="Model request failed",
                    detail=_short(exc, 160),
                    visibility="timeline",
                    step_id=f"recover:{state.iterations}",
                )
                yield AgentEvent.error(
                    message=_format_llm_error(f"LLM API 调用失败: {exc}", classification.error_type),
                    recoverable=not classification.fatal,
                    error_type=classification.error_type,
                )
                state.stopped_reason = classification.error_type if classification.fatal else "api_error"
                if full_text:
                    ctx.append_assistant(full_text)
                return

            # ── Decision point: tool_calls → execute → loop; no tool_calls → done ──

            if not pending_tool_calls:
                # ── Stop quality gate + hook ──
                hook_mgr = get_hook_manager()
                stop_feedback: str | None = None

                # 1. Run stop hook (exit code 2 → feedback injection)
                if hook_mgr and hook_mgr.has_hooks(HookEvent.STOP):
                    hook_result = await hook_mgr.run_stop(
                        user_message, full_text, tool_results=state.tool_calls
                    )
                    if hook_result.has_feedback:
                        stop_feedback = hook_result.feedback

                # 2. Stop quality gate (detect weak replies)
                if not stop_feedback:
                    stop_feedback = await grounded_reply_policy.maybe_produce_grounded_reply(
                        user_message, full_text, state, llm
                    )

                # 3. If feedback exists, inject as user message and retry
                if stop_feedback:
                    if full_text:
                        ctx.append_assistant(full_text)
                    ctx.append_user(stop_feedback)
                    full_text = ""
                    continue

                # 4. Reflection pass (optional polish, not answer generation)
                if streamed_text:
                    full_text = streamed_text

                if full_text:
                    yield AgentEvent.text_chunk(full_text)

                reflection = await reflection_policy.maybe_reflect(user_message, full_text, state, llm)
                if reflection and reflection.verdict == "revise" and reflection.addendum:
                    full_text += reflection.addendum
                    yield AgentEvent.text_chunk(reflection.addendum)

                if full_text:
                    ctx.append_assistant(full_text)
                    state.reply = full_text
                state.stopped_reason = "completed"
                yield AgentEvent.done(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                )
                break

            # ── Execute tool calls ──
            ctx.append_assistant_tool_calls(pending_tool_calls)

            async for ev in _execute_tool_batch(
                pending_tool_calls,
                ctx=ctx, state=state,
                tool_registry=tool_registry,
                permission_checker=permission_checker,
                approval_handler=approval_handler,
                skill_manager=skill_manager,
                permission_context=permission_context,
                tool_ctx=tool_ctx,
                stagnation_limit=settings.stagnation_limit,
            ):
                yield ev

    except asyncio.CancelledError:
        state.stopped_reason = "interrupted"
        if full_text:
            ctx.append_assistant(full_text)
        raise

    # ── Phase 3: Post-session memory ──────────────────────────────────────
    if ctx and ctx.history_length > 4:
        try:
            await asyncio.to_thread(_save_session_memory, state, vector_memory)
        except Exception as exc:
            logger.debug("Session memory save failed: %s", exc)


def _save_session_memory(state: AgentState, vector_memory: Any | None = None) -> None:
    """Save key session facts to long-term vector memory."""
    if vector_memory is None:
        return

    parts = []
    if state.user_message:
        parts.append(f"用户请求: {state.user_message[:200]}")
    for tc in state.tool_calls[:5]:
        name = tc.tool_name if hasattr(tc, "tool_name") else tc.get("name", "?")
        output = tc.tool_output if hasattr(tc, "tool_output") else tc.get("result", "")
        parts.append(f"工具 {name}: {(output or '')[:100]}")
    if state.stopped_reason:
        parts.append(f"终止原因: {state.stopped_reason}")
    if state.active_skills:
        parts.append(f"使用 Skills: {', '.join(state.active_skills)}")
    if not parts:
        return
    content = "\n".join(parts)
    tags = [f"session:{state.stopped_reason}"] + [f"skill:{s}" for s in state.active_skills]
    vector_memory.remember(content, tags=tags, importance=3)
    vector_memory.flush()
