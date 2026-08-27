"""Cancellation-aware preflight and hook boundaries for an agent turn."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, TypeVar

from backend.async_cleanup import cancel_and_drain
from backend.hooks.manager import HookEvent, get_hook_manager

logger = logging.getLogger(__name__)


class PhaseDeadlineExceeded(asyncio.TimeoutError):
    """The active turn phase exhausted its absolute deadline."""


PreflightResult = TypeVar("PreflightResult")


@dataclass(frozen=True, slots=True)
class TurnPreflightResult:
    user_message: str
    deadline_reached: bool
    blocked: bool
    block_message: str
    session_hook_result: Any | None
    prompt_hook_result: Any | None
    # SessionStart hooks may provide a synthetic first user message and paths
    # that should be watched for FileChanged.  Keep both values on the
    # preflight boundary instead of silently dropping them.
    initial_user_message: str = ""
    watch_paths: tuple[str, ...] = ()


async def prepare_turn_input(
    user_message: str,
    *,
    state: Any,
    turn_kernel: Any,
    session_id: str,
    deadline: float | None,
    cancel_event: asyncio.Event | None,
    resume_from_checkpoint: bool = False,
) -> TurnPreflightResult:
    """Run hooks and schedule the one canonical turn input."""
    deadline_reached = False
    blocked = False
    block_message = ""
    session_hook_result = None
    prompt_hook_result = None
    initial_user_message = ""
    watch_paths: tuple[str, ...] = ()

    hook_manager = get_hook_manager()
    try:
        if (
            not deadline_reached
            and not resume_from_checkpoint
            and hook_manager
            and session_id
            and hook_manager_has_hooks(hook_manager, HookEvent.SESSION_START)
        ):
            session_hook_result = await await_preflight(
                hook_manager.run_session_start_once(session_id),
                deadline=deadline,
                cancel_event=cancel_event,
            )
            initial_user_message = str(
                getattr(session_hook_result, "initial_user_message", "") or ""
            ).strip()
            raw_watch_paths = getattr(session_hook_result, "watch_paths", ())
            if isinstance(raw_watch_paths, (list, tuple, set)):
                watch_paths = tuple(
                    dict.fromkeys(
                        str(path).strip()
                        for path in raw_watch_paths
                        if str(path or "").strip()
                    )
                )
            if initial_user_message:
                # Headless callers may provide an initial message separately
                # from the user turn. Consume it only when no user turn exists
                # and retain the hook value for embedding callers.
                if not str(user_message or "").strip():
                    user_message = initial_user_message
                    state.user_message = user_message
                elif isinstance(getattr(state, "prompt_context", None), dict):
                    state.prompt_context["hook_initial_user_message"] = initial_user_message
            if watch_paths and isinstance(getattr(state, "prompt_context", None), dict):
                state.prompt_context["hook_watch_paths"] = list(watch_paths)
        if (
            not deadline_reached
            and hook_manager
            and hook_manager_has_hooks(hook_manager, HookEvent.USER_PROMPT_SUBMIT)
        ):
            prompt_hook_result = await await_preflight(
                hook_manager.run_user_prompt_submit(user_message),
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if prompt_hook_result.blocked:
                blocked = True
                block_message = (
                    prompt_hook_result.message
                    or prompt_hook_result.feedback
                    or "User prompt blocked by hook"
                )
            elif prompt_hook_result.has_updated_input:
                user_message = prompt_hook_result.updated_input
                state.user_message = user_message
    except PhaseDeadlineExceeded:
        deadline_reached = True
        logger.warning("Turn deadline reached while running prompt hooks")

    if not blocked and not resume_from_checkpoint:
        turn_kernel.schedule_user_input(user_message)
    return TurnPreflightResult(
        user_message=user_message,
        deadline_reached=deadline_reached,
        blocked=blocked,
        block_message=block_message,
        session_hook_result=session_hook_result,
        prompt_hook_result=prompt_hook_result,
        initial_user_message=initial_user_message,
        watch_paths=watch_paths,
    )


async def await_preflight(
    operation: Awaitable[PreflightResult],
    *,
    deadline: float | None,
    cancel_event: asyncio.Event | None,
) -> PreflightResult:
    """Wait for preflight work under the turn's cancellation/deadline fence."""
    task = asyncio.ensure_future(operation)
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    waiters = {task, *([cancel_task] if cancel_task is not None else [])}
    timeout = max(0.0, deadline - time.monotonic()) if deadline is not None else None
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task is not None and cancel_task in done:
            await cancel_and_drain([task], timeout=2.5, label="agent preflight operation")
            raise asyncio.CancelledError
        if task in done:
            return task.result()
        await cancel_and_drain([task], timeout=2.5, label="agent preflight operation")
        raise PhaseDeadlineExceeded
    except asyncio.CancelledError:
        await cancel_and_drain([task], timeout=2.5, label="agent preflight operation")
        raise
    finally:
        if cancel_task is not None:
            await cancel_and_drain(
                [cancel_task], timeout=0.1, label="agent preflight cancellation waiter"
            )


def hook_manager_has_hooks(hook_manager: Any, event: HookEvent) -> bool:
    has_hooks = getattr(hook_manager, "has_hooks", None)
    if not callable(has_hooks):
        return False
    try:
        return bool(has_hooks(event))
    except Exception as exc:
        logger.debug("hook has_hooks(%s) failed: %s", event, exc)
        return False


async def run_stop_failure_hook(
    error: str,
    *,
    error_details: str = "",
    last_assistant_message: str = "",
) -> None:
    hook_manager = get_hook_manager()
    if not hook_manager or not hook_manager_has_hooks(hook_manager, HookEvent.STOP_FAILURE):
        return
    try:
        await hook_manager.run_stop_failure(
            error,
            error_details=error_details,
            last_assistant_message=last_assistant_message,
        )
    except Exception as exc:
        logger.warning("stop_failure hook failed: %s", exc)
