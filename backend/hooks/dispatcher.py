from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from backend.hooks.runners import HookExecutionError

from backend.hooks.policy import event_policy


_EXACT_MATCHER_RE = re.compile(r"^[A-Za-z0-9_|]+$")

def _is_exact_matcher(raw: str) -> bool:
    """Return whether ``raw`` uses MiniCode's exact-name matcher grammar.

    ASCII letters, digits, underscore and pipe form exact-match syntax. A
    hyphen, dot, colon, parentheses, ``+`` or
    any other regex metacharacter switches the matcher to regular-expression
    mode.  This distinction prevents a matcher such as ``Bash`` from
    accidentally matching ``BashOutput``.
    """

    return bool(_EXACT_MATCHER_RE.fullmatch(raw))


def compile_hook_matcher(pattern: str | None) -> re.Pattern[str]:
    """Compile a MiniCode hook matcher.

    Empty and ``*`` are match-all patterns.  Exact names and pipe-separated
    names are compiled as anchored alternatives.  All other patterns are
    treated as regular expressions and validated eagerly so discovery can
    reject malformed entries before a turn starts.
    """

    raw = str(pattern or "").strip()
    if not raw or raw == "*":
        return re.compile(r"^.*$", re.DOTALL)
    if _is_exact_matcher(raw):
        alternatives = [
            re.escape(candidate.strip())
            for candidate in raw.split("|")
        ]
        return re.compile(r"^(?:" + "|".join(alternatives) + r")$")
    return re.compile(raw)


def matcher_matches(
    pattern: str | None,
    target: str,
    *,
    tool_match: bool = False,
) -> bool:
    """Evaluate one matcher against a query.

    ``tool_match`` remains an explicit call-site marker for diagnostics, but
    does not rewrite names. Invalid regular expressions fail closed.
    """

    raw = str(pattern or "").strip()
    query = str(target or "")
    if not raw or raw == "*":
        return True

    if _is_exact_matcher(raw):
        for candidate in raw.split("|"):
            if candidate.strip() == query:
                return True
        return False

    try:
        regex = re.compile(raw)
    except re.error:
        return False
    return regex.search(query) is not None


@dataclass(frozen=True)
class HookExecution:
    entry: Any
    stdout: str
    stderr: str
    exit_code: int
    configured_order: int
    completion_order: int
    duration_ms: int
    execution_failed: bool = False


def select_handlers(
    entries: Iterable[Any],
    *,
    event: Any,
    match_target: str,
    condition_matches: Callable[[Any], bool],
) -> list[Any]:
    policy = event_policy(event)
    selected: list[Any] = []
    tool_match = str(getattr(event, "value", event) or "") in {
        "pre_tool_use",
        "post_tool_use",
        "post_tool_use_failure",
        "permission_request",
        "permission_denied",
    }
    # A missing event query means "no filter": the current
    # event's handlers are selected even when a configured matcher exists.
    # This matters for session_start and other events whose optional field
    # may legitimately be empty.  Do not collapse this into matcher_matches(),
    # because a direct matcher call with an empty target should still report
    # whether the pattern matches that concrete string.
    should_filter = policy.matcher_applies and bool(str(match_target or ""))
    for entry in entries:
        if should_filter and not matcher_matches(
            entry.raw_matcher
            if hasattr(entry, "raw_matcher")
            else entry.matcher.pattern,
            match_target,
            tool_match=tool_match,
        ):
            continue
        if not condition_matches(entry):
            continue
        selected.append(entry)
    return selected


async def execute_handlers(
    entries: Iterable[Any],
    execute: Callable[[Any], Awaitable[tuple[str, str, int]]],
) -> list[HookExecution]:
    """Run one matched batch concurrently, then restore config order."""

    async def run_one(
        configured_order: int,
        entry: Any,
    ) -> tuple[int, Any, str, str, int, int, bool]:
        started = time.monotonic()
        execution_failed = False
        try:
            stdout, stderr, exit_code = await execute(entry)
        except asyncio.CancelledError:
            raise
        except HookExecutionError as exc:
            stdout, stderr, exit_code = "", f"Hook execution failed: {exc}", 1
            execution_failed = True
        except Exception as exc:
            stdout, stderr, exit_code = "", f"Hook execution failed: {exc}", 1
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        return configured_order, entry, stdout, stderr, exit_code, duration_ms, execution_failed

    tasks = [
        asyncio.create_task(run_one(configured_order, entry))
        for configured_order, entry in enumerate(entries)
    ]
    if not tasks:
        return []
    completed: list[HookExecution] = []
    completion_order = 0
    try:
        for future in asyncio.as_completed(tasks):
            (
                configured_order,
                entry,
                stdout,
                stderr,
                exit_code,
                duration_ms,
                execution_failed,
            ) = await future
            completed.append(
                HookExecution(
                    entry=entry,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    configured_order=configured_order,
                    completion_order=completion_order,
                    duration_ms=duration_ms,
                    execution_failed=execution_failed,
                )
            )
            completion_order += 1
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    completed.sort(key=lambda item: item.configured_order)
    return completed
