from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.policies.web_search_guard import web_search_guard_reason
from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.review import generate_edit_diff_payload, generate_file_diff_payload
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

CHECKPOINT_WRITE_TOOL_NAMES = {"write_file", "edit_file"}
MUTATING_TOOL_NAMES = CHECKPOINT_WRITE_TOOL_NAMES | {
    "run_command", "git_commit", "save_memory", "remember_memory",
    "create_worktree", "remove_worktree",
}
SPECIAL_TOOL_NAMES = {"load_skill", "unload_skill", "list_skills", "ask_user"}

TOOL_TIMEOUTS: dict[str, float] = {
    "run_command": 120.0,
    "write_file": 30.0,
    "edit_file": 30.0,
    "read_file": 10.0,
    "list_files": 10.0,
    "grep_files": 15.0,
    "glob_files": 10.0,
    "fuzzy_search": 15.0,
    "web_search": 30.0,
    "mcp__websearch__search": 30.0,
    "web_fetch": 30.0,
    "mcp__websearch__fetch_page": 30.0,
}
DEFAULT_TOOL_TIMEOUT = 60.0


def describe_tool_call(tc: ToolCallEvent) -> str:
    args = tc.arguments or {}
    target = (
        str(args.get("file_path") or args.get("path") or args.get("target") or "").strip()
        or str(args.get("directory") or args.get("cwd") or "").strip()
        or str(args.get("pattern") or args.get("query") or "").strip()
        or str(args.get("command") or "").strip()
    )
    return f"{tc.name} {target}".strip()


def _short_text(value: str, max_len: int = 96) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _short_path(value: str, max_parts: int = 2) -> str:
    normalized = value.replace("\\", "/").strip()
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part]
    if len(parts) <= max_parts:
        return "/".join(parts) or normalized
    return f".../{'/'.join(parts[-max_parts:])}"


def _hostname(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return ""
    return parsed.netloc or parsed.path.split("/")[0]


def result_kind_for_tool(tool_name: str) -> str:
    name = tool_name.lower()
    if name.startswith("mcp__"):
        return "mcp"
    if name in {"web_fetch", "mcp__websearch__fetch_page"} or "fetch" in name:
        return "web"
    if name in {"web_search", "search_web", "mcp__websearch__search"}:
        return "search"
    if "command" in name or "terminal" in name or name in {"bash", "powershell"}:
        return "command"
    if name in {"write_file", "edit_file"} or any(part in name for part in ("write", "edit", "patch", "delete")):
        return "edit"
    if any(part in name for part in ("read", "file", "list", "grep", "glob")):
        return "file"
    return "generic"


def input_summary_for_tool(tool_name: str, args: dict[str, Any]) -> str:
    name = tool_name.lower()
    if name in {"web_search", "search_web", "mcp__websearch__search"}:
        return _short_text(str(args.get("query") or args.get("q") or ""))
    if name in {"web_fetch", "mcp__websearch__fetch_page"} or "fetch" in name:
        url = str(args.get("url") or "")
        return _hostname(url) or _short_text(url)
    if "command" in name or "terminal" in name or name in {"bash", "powershell"}:
        return _short_text(str(args.get("command") or args.get("cmd") or ""))
    path_value = str(args.get("file_path") or args.get("path") or args.get("target") or args.get("directory") or "")
    if path_value:
        return _short_path(path_value)
    query = str(args.get("query") or args.get("pattern") or "")
    if query:
        return _short_text(query)
    return ""


def display_hint_for_tool(tool_name: str) -> str:
    return {
        "web": "Fetching page",
        "search": "Searching",
        "command": "Running command",
        "file": "Reading workspace",
        "edit": "Editing workspace",
        "mcp": "Running MCP tool",
    }.get(result_kind_for_tool(tool_name), "Running tool")


def status_for_result(result: ToolResult, requested_status: str | None = None) -> str:
    if requested_status in {"success", "failed", "blocked"}:
        return requested_status
    if result.status in {"success", "failed", "blocked"}:
        return str(result.status)
    return "failed" if result.is_error else "success"


def display_summary_for_result(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    status: str,
    diff: dict[str, Any] | None = None,
) -> str:
    if result.display_summary:
        return result.display_summary
    kind = result.result_kind or result_kind_for_tool(tc.name)
    name = tc.name.lower()
    args = tc.arguments or {}
    target = input_summary_for_tool(tc.name, args)
    if kind == "search":
        return f"Searched web: {target}" if target else "Searched web"
    if kind == "web":
        url = str(args.get("url") or result.source_url or "")
        host = _hostname(url) or target
        verb = "Fetch failed" if status == "failed" else "Fetched page"
        return f"{verb}: {host}" if host else verb
    if kind == "command":
        prefix = "Command failed" if status == "failed" else "Ran command"
        return f"{prefix}: {target}" if target else prefix
    if kind == "edit":
        path = target or _short_path(str(args.get("file_path") or args.get("path") or ""))
        summary = f"Edited file: {path}" if path else "Edited file"
        if diff:
            plus = int(diff.get("plus") or diff.get("additions") or 0)
            minus = int(diff.get("minus") or diff.get("deletions") or 0)
            if plus or minus:
                summary = f"{summary} (+{plus} -{minus})"
        return summary
    if kind == "file":
        if name == "list_files":
            return f"Listed directory: {target}" if target else "Listed directory"
        if name == "read_file":
            return f"Read file: {target}" if target else "Read file"
        return f"Used workspace tool: {target}" if target else "Used workspace tool"
    if kind == "mcp":
        parts = tc.name.split("__")
        label = "/".join(parts[1:3]) if len(parts) >= 3 else tc.name
        return f"Ran MCP tool: {label}"
    if status == "blocked":
        return f"Blocked tool: {tc.name}"
    if status == "failed":
        return f"Tool failed: {tc.name}"
    return f"Ran tool: {tc.name}"


def _tool_start_times(state: AgentState) -> dict[str, float]:
    existing = getattr(state, "_ui_tool_started_at", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, float] = {}
    setattr(state, "_ui_tool_started_at", created)
    return created


async def snapshot_before_write(tc: ToolCallEvent, tool_ctx: ToolExecutionContext) -> None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return
    manager = getattr(tool_ctx, "checkpoint_manager", None)
    if manager is None:
        return
    try:
        record = await manager.snapshot(
            tool_name=tc.name,
            args=tc.arguments,
            workspace_root=tool_ctx.workspace_root,
            conversation_id=tool_ctx.conversation_id,
            session_id=tool_ctx.session_id,
            tool_call_id=tc.id,
        )
    except Exception as exc:
        logger.warning("checkpoint snapshot failed: %s", exc)
        return
    if record is None:
        return
    emit = getattr(tool_ctx, "emit_event", None)
    if emit:
        try:
            await emit("checkpoint.created", record.to_dict())
        except Exception:
            pass


async def run_tool(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if hook_mgr:
        try:
            pre = await hook_mgr.run_pre_tool(tc.name, tc.arguments)
            if pre.blocked:
                return ToolResult(content=f"Tool blocked by hook: {pre.message}", is_error=True)
        except Exception as exc:
            logger.warning("pre_tool hook failed: %s", exc)

    await snapshot_before_write(tc, tool_ctx)
    changed_file = changed_file_event_payload(tc, tool_ctx)

    tm = tool_ctx.task_manager
    if tm:
        try:
            managed = tm.create(
                kind="tool_run",
                awaitable=tool_registry.execute(tc.name, tc.arguments, context=tool_ctx),
            )
            result = await tm.wait(managed.id)
        except Exception as exc:
            result = ToolResult(content=f"Task execution failed: {exc}", is_error=True)
    else:
        result = await tool_registry.execute(tc.name, tc.arguments, context=tool_ctx)

    if hook_mgr and not result.is_error:
        try:
            await hook_mgr.run_post_tool(tc.name, tc.arguments, result.content or "")
        except Exception as exc:
            logger.warning("post_tool hook failed: %s", exc)

    if changed_file and not result.is_error:
        emit = getattr(tool_ctx, "emit_event", None)
        if emit:
            try:
                await emit("file.changed", changed_file)
            except Exception as exc:
                logger.debug("file change emit failed: %s", exc)

    return result


def changed_file_event_payload(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any] | None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return None
    raw_path = str(tc.arguments.get("file_path") or "").strip()
    if not raw_path:
        return None
    workspace_root = Path(tool_ctx.workspace_root).resolve() if tool_ctx.workspace_root else None
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else ((workspace_root / path).resolve() if workspace_root else path.resolve())
    existed_before = resolved.exists()
    event_type = "created" if tc.name == "write_file" and not existed_before else "modified"
    display_path = raw_path
    if workspace_root:
        try:
            display_path = resolved.relative_to(workspace_root).as_posix()
        except ValueError:
            display_path = resolved.as_posix()
    return {
        "path": display_path,
        "event": event_type,
    }


async def run_tool_with_timeout(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    timeout = TOOL_TIMEOUTS.get(tc.name, DEFAULT_TOOL_TIMEOUT)
    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            result = await run_tool(tc, tool_registry, tool_ctx)
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return ToolResult(
            content=f"Tool '{tc.name}' timed out after {timeout:.0f}s. "
            "Consider breaking the operation into smaller steps.",
            is_error=True,
            duration_ms=elapsed,
            status="timeout",
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.duration_ms is None:
        result = replace(result, duration_ms=elapsed)
    return result


def batch_tool_calls(
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
) -> list[tuple[bool, list[ToolCallEvent]]]:
    if not tool_calls:
        return []
    batches: list[tuple[bool, list[ToolCallEvent]]] = []
    for tc in tool_calls:
        tool = tool_registry.get_tool(tc.name)
        is_safe = tool.is_concurrency_safe(tc.arguments) if tool else False
        if is_safe and batches and batches[-1][0]:
            batches[-1][1].append(tc)
        else:
            batches.append((is_safe, [tc]))
    return batches


async def execute_tool_batch(
    tool_calls: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    stagnation_limit: int,
) -> AsyncIterator[AgentEvent]:
    auto_queue: list[ToolCallEvent] = []
    seen_in_step: set[str] = set()

    for tc in tool_calls:
        started_epoch = time.time()
        _tool_start_times(state)[tc.id] = started_epoch
        yield AgentEvent.tool_call(
            id=tc.id,
            name=tc.name,
            args=tc.arguments,
            started_at=int(started_epoch * 1000),
            display_hint=display_hint_for_tool(tc.name),
            input_summary=input_summary_for_tool(tc.name, tc.arguments),
        )

        sig = state.call_signature(tc.name, tc.arguments)
        if sig in seen_in_step:
            async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
                yield ev
            auto_queue = []
            reason = f"Skipped duplicate tool call in the same model step: {describe_tool_call(tc)}"
            yield store_result(tc, ToolResult(content=reason, is_error=True), ctx, state, status="blocked")
            continue
        seen_in_step.add(sig)

        repeat_reason = state.repeated_call_guard_reason(tc.name, tc.arguments, limit=stagnation_limit)
        if repeat_reason:
            async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
                yield ev
            auto_queue = []
            yield store_result(tc, ToolResult(content=repeat_reason, is_error=True), ctx, state, status="blocked")
            continue

        web_guard_reason = web_search_guard_reason(state, tc, queued_tool_calls=auto_queue)
        if web_guard_reason:
            async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
                yield ev
            auto_queue = []
            yield store_result(tc, ToolResult(content=web_guard_reason, is_error=True), ctx, state, status="blocked")
            continue

        perm = permission_checker.check(tc.name, tc.arguments, context=permission_context)
        denial = permission_checker.get_denial_reason(tc.name, tc.arguments, context=permission_context)

        if denial or perm == PermissionLevel.ALWAYS_DENY:
            async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
                yield ev
            auto_queue = []
            msg = denial or f"Tool '{tc.name}' is blocked by policy"
            yield store_result(tc, ToolResult(content=msg, is_error=True), ctx, state, status="blocked")
            continue

        if perm == PermissionLevel.AUTO and tc.name not in SPECIAL_TOOL_NAMES:
            auto_queue.append(tc)
            continue

        async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
            yield ev
        auto_queue = []

        async for ev in execute_serial(
            tc,
            perm=perm,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
        ):
            yield ev

    async for ev in flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx):
        yield ev


async def flush_queue(
    queue: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> AsyncIterator[AgentEvent]:
    if not queue:
        return

    for is_concurrent, batch in batch_tool_calls(queue, tool_registry):
        if is_concurrent and len(batch) > 1:
            results = await asyncio.gather(
                *(run_tool_with_timeout(tc, tool_registry, tool_ctx) for tc in batch),
                return_exceptions=True,
            )
            for tc, raw in zip(batch, results):
                result = raw if isinstance(raw, ToolResult) else ToolResult(
                    content=f"Execution failed: {raw}",
                    is_error=True,
                )
                yield store_result(tc, result, ctx, state)
        else:
            for tc in batch:
                diff = generate_diff(tc.name, tc.arguments)
                result = await run_tool_with_timeout(tc, tool_registry, tool_ctx)
                yield store_result(tc, result, ctx, state, diff=None if result.is_error else diff)


async def execute_serial(
    tc: ToolCallEvent,
    *,
    perm: PermissionLevel,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    approval_handler: Callable | None,
    skill_manager: Any | None,
) -> AsyncIterator[AgentEvent]:
    diff: dict[str, Any] | None = None

    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW):
        diff = generate_diff(tc.name, tc.arguments) if perm == PermissionLevel.DIFF_REVIEW else None
        yield AgentEvent.approval_request(
            tool_call_id=tc.id,
            tool_name=tc.name,
            args=tc.arguments,
            diff=diff,
        )
        if approval_handler:
            approval = await approval_handler(tc.id)
            if approval.get("action") == "reject":
                guidance = approval.get("guidance", "user rejected this action")
                result = ToolResult(content=f"Operation rejected: {guidance}", is_error=True)
                yield store_result(tc, result, ctx, state)
                return
            if approval.get("action") == "partial":
                decisions = approval.get("decisions", {})
                target = tc.arguments.get("file_path") or tc.arguments.get("path") or ""
                rejected = [p for p, d in decisions.items() if d == "rejected"]
                if target and any(target.endswith(rp) or rp.endswith(target) for rp in rejected):
                    result = ToolResult(content=f"Operation rejected for file: {target}", is_error=True)
                    yield store_result(tc, result, ctx, state)
                    return

    if tc.name == "ask_user" and approval_handler:
        question = tc.arguments.get("question", "")
        yield AgentEvent(type="ask_user", data={"tool_call_id": tc.id, "question": question})
        answer_data = await approval_handler(tc.id)
        answer = answer_data.get("answer", answer_data.get("guidance", ""))
        result = ToolResult(content=f"User answer: {answer}")
    elif tc.name == "load_skill" and skill_manager:
        name = tc.arguments.get("skill_name", "")
        result = (
            ToolResult(content=f"Skill '{name}' activated")
            if name and skill_manager.activate(name)
            else ToolResult(content=f"Skill '{name}' activation failed", is_error=True)
        )
    elif tc.name == "unload_skill" and skill_manager:
        name = tc.arguments.get("skill_name", "")
        result = (
            ToolResult(content=f"Skill '{name}' deactivated")
            if name and skill_manager.deactivate(name)
            else ToolResult(content=f"Skill '{name}' is not active", is_error=True)
        )
    elif tc.name == "list_skills" and skill_manager:
        skills = skill_manager.list_all()
        lines = ["Available Skills:"] + [
            f"  [{'active' if s.get('active') else 'inactive'}] {s['name']}: {s.get('description', '')}"
            for s in skills
        ]
        result = ToolResult(content="\n".join(lines))
    else:
        if diff is None:
            diff = generate_diff(tc.name, tc.arguments)
        result = await run_tool_with_timeout(tc, tool_registry, tool_ctx)

    yield store_result(tc, result, ctx, state, diff=None if result.is_error else diff)


def store_result(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
) -> AgentEvent:
    from backend.tools.base import truncate_tool_result

    final_status = status_for_result(result, status)
    result_kind = result.result_kind or result_kind_for_tool(tc.name)
    limitation = result.limitation or (
        "unsandboxed background command"
        if tc.name == "run_command" and bool(tc.arguments.get("run_in_background"))
        else ""
    )
    started_at = _tool_start_times(state).get(tc.id)
    duration_ms = result.duration_ms
    if duration_ms is None and isinstance(started_at, (int, float)):
        duration_ms = int(max(0.0, time.time() - started_at) * 1000)
    truncated = replace(result, content=truncate_tool_result(result.content))
    display_summary = display_summary_for_result(tc, truncated, status=final_status, diff=diff)
    truncated = replace(
        truncated,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation,
    )
    ctx.append_tool_result(tc.id, tc.name, truncated)
    state.record_tool_call(
        tc.name,
        tc.arguments,
        truncated.to_context_string(),
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        mutates=tc.name in MUTATING_TOOL_NAMES,
        status=final_status,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
    )
    return AgentEvent.tool_result(
        id=tc.id,
        summary=truncated.content,
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        diff=diff,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation or "",
    )


def generate_diff(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            inject_expected_hash(args, file_path)
            return generate_file_diff_payload(file_path, content)
    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            inject_expected_hash(args, file_path)
            return generate_edit_diff_payload(file_path, old_string, new_string)
    return None


def inject_expected_hash(args: dict[str, Any], file_path: str) -> None:
    if str(args.get("expected_hash") or "").strip():
        return
    path = Path(str(file_path))
    if not path.exists():
        args["expected_hash"] = ""
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    args["expected_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
