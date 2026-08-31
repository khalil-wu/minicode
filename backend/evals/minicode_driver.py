"""Non-interactive driver for the repository-task evaluator.

This intentionally goes through ``QueryEngine.submit`` (the same lifecycle used
by the websocket session) rather than implementing a second miniature ReAct
loop.  It is useful for reproducing real repository tasks from CI or a shell.
Credentials are read only from the process environment.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.agent.loop import AgentLoopSessionContext
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.runtime_env import ensure_utf8_console_logging
from backend.services.llm_adapter_factory import build_wire_adapter
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.base import PermissionLevel


_TRACE_STRING_LIMIT = 2_000
_HIGH_VOLUME_STREAM_EVENTS = frozenset(
    {
        "agent_message.delta",
        "text_delta",
        "thinking_chunk",
        "thinking_delta",
        "reasoning_chunk",
        "reasoning_delta",
        "stream.delta",
    }
)
_HIGH_VOLUME_PROVIDER_EVENTS = frozenset(
    {
        "text_delta",
        "thinking_delta",
        "reasoning_delta",
        "content_block_delta",
    }
)


def _compact_trace_value(value: object) -> object:
    if isinstance(value, str):
        if len(value) <= _TRACE_STRING_LIMIT:
            return value
        head_size = (_TRACE_STRING_LIMIT * 2) // 3
        tail_size = _TRACE_STRING_LIMIT - head_size
        omitted = len(value) - head_size - tail_size
        return f"{value[:head_size]}\n... [{omitted} chars omitted] ...\n{value[-tail_size:]}"
    if isinstance(value, list):
        items = value if len(value) <= 40 else [*value[:30], f"... {len(value) - 40} items omitted ...", *value[-10:]]
        return [_compact_trace_value(item) for item in items]
    if isinstance(value, dict):
        return {str(key): _compact_trace_value(item) for key, item in value.items()}
    return value


def _trace_event_data(event_type: str, data: dict[str, object]) -> dict[str, object]:
    compact_source = dict(data)
    # Tool outcomes duplicate the summary, status, artifact preview, and error
    # fields at the event root. Keeping the nested raw content made a single
    # long-running test crowd all earlier iterations out of evaluation reports.
    if event_type == "tool_result":
        compact_source.pop("outcome", None)
        compact_source.pop("developer_detail", None)
    compacted = _compact_trace_value(compact_source)
    return compacted if isinstance(compacted, dict) else {}


def _should_emit_trace_event(event_type: str, data: dict[str, object]) -> bool:
    if event_type in _HIGH_VOLUME_STREAM_EVENTS:
        return False
    if event_type == "stream_event" and str(data.get("event_type") or "") in _HIGH_VOLUME_PROVIDER_EVENTS:
        return False
    return True


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing {name}")
    return value


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


def _test_file_snapshot(workspace: Path) -> dict[str, str]:
    """Capture existing test files so an eval cannot weaken its oracle."""

    snapshot: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(workspace)
        if "tests" not in relative.parts and not path.name.startswith("test_"):
            continue
        snapshot[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _test_integrity_violations(
    before: dict[str, str],
    workspace: Path,
) -> dict[str, list[str]]:
    """Return modified/deleted pre-existing tests; newly added tests are valid."""

    after = _test_file_snapshot(workspace)
    modified = sorted(
        relative
        for relative, digest in before.items()
        if relative in after and after[relative] != digest
    )
    deleted = sorted(relative for relative in before if relative not in after)
    return {"modified": modified, "deleted": deleted}


def _repository_eval_permission(
    workspace: Path,
) -> tuple[PermissionChecker, PermissionContext]:
    """Build the autonomous permission boundary for an isolated eval checkout."""

    checker = PermissionChecker(
        PermissionSettings(require_diff_review=[]),
        workspace_root=workspace,
    )
    permission = checker.build_context(
        mode="auto",
        session_overrides={
            name: PermissionLevel.AUTO
            for name in ("run_command", "write_file", "edit_file", "apply_patch")
        },
        workspace_scope="worktree",
        source="repository_eval",
    )
    return checker, permission


async def _approve_isolated_eval_call(_tool_call_id: str) -> dict[str, str]:
    """Approve a call inside the evaluator-owned isolated checkout."""

    return {"action": "approve"}


def _usage_from_done_event(event_data: dict[str, object]) -> dict[str, int]:
    nested = event_data.get("usage")
    usage = nested if isinstance(nested, dict) else {}
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_output_tokens",
    )
    return {
        key: int(usage.get(key) or event_data.get(key) or 0)
        for key in keys
    }


def _usage_from_provider_stream_event(event_data: dict[str, object]) -> dict[str, int]:
    if str(event_data.get("event_type") or "") != "done":
        return {}
    raw = event_data.get("data")
    if not isinstance(raw, dict):
        return {}
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return {}
    completion_details = usage.get("completion_tokens_details")
    details = completion_details if isinstance(completion_details, dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cache_read_input_tokens": int(
            usage.get("cache_read_input_tokens")
            or usage.get("cached_input_tokens")
            or usage.get("cached_prompt_tokens")
            or 0
        ),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "reasoning_output_tokens": int(
            usage.get("reasoning_output_tokens")
            or details.get("reasoning_tokens")
            or 0
        ),
    }


def _observe_tool_call(
    tool_call_names: dict[str, str],
    event_data: dict[str, object],
) -> None:
    """Project one logical tool entity across pending/running updates."""
    tool_call_id = str(
        event_data.get("id")
        or event_data.get("tool_call_id")
        or event_data.get("step_id")
        or ""
    ).strip()
    if not tool_call_id:
        return
    tool_name = str(event_data.get("name") or "").strip()
    previous = tool_call_names.get(tool_call_id, "")
    if (
        not previous
        or previous == "unknown"
        or (
            previous == "tool_call"
            and tool_name
            and tool_name != "tool_call"
        )
    ):
        tool_call_names[tool_call_id] = tool_name or previous or "unknown"


def _event_loop_metrics(event_data: dict[str, object]) -> dict[str, object]:
    metrics = event_data.get("loop_metrics")
    if isinstance(metrics, dict):
        return dict(metrics)
    payload = event_data.get("payload")
    if isinstance(payload, dict):
        metrics = payload.get("loop_metrics")
        if isinstance(metrics, dict):
            return dict(metrics)
    return {}


def _authoritative_iteration_count(
    max_event_iteration: int,
    loop_metrics: dict[str, object],
) -> int:
    result = max(0, int(max_event_iteration or 0))
    for key in ("iteration", "provider_call_count", "iterations"):
        try:
            result = max(result, int(loop_metrics.get(key) or 0))
        except (TypeError, ValueError):
            pass
    return result


def _runtime_elapsed_ms(
    spans: list[dict[str, object]],
    event_names: set[str],
) -> int:
    return sum(
        max(0, int(span.get("duration_ms") or 0))
        for span in spans
        if str(span.get("event") or "") in event_names
    )


def _runtime_recovery_ids(spans: list[dict[str, object]]) -> set[str]:
    """Return distinct recovery attempts emitted through runtime spans."""

    recovery_ids: set[str] = set()
    for span in spans:
        phase = str(span.get("phase") or "").strip().lower()
        event = str(span.get("event") or "").strip()
        status = str(span.get("status") or "").strip().lower()
        if phase not in {"recover", "recovery"}:
            continue
        if status not in {"running", "started"} and not event.endswith(".started"):
            continue
        recovery_ids.add(
            str(span.get("span_id") or span.get("id") or event).strip()
        )
    return {item for item in recovery_ids if item}


def _eval_max_turn_seconds() -> float:
    """Keep the Agent deadline inside the evaluator process deadline."""

    explicit = os.environ.get("MINICODE_EVAL_MAX_TURN_SECONDS")
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except ValueError:
            return 0.0
    try:
        outer_timeout = float(os.environ.get("MINICODE_EVAL_AGENT_TIMEOUT_SECONDS", "0"))
    except ValueError:
        return 0.0
    if outer_timeout <= 0:
        return 0.0
    reserve = max(90.0, min(300.0, outer_timeout * 0.17))
    return max(30.0, outer_timeout - reserve)


def _peak_runtime_parallelism(records: list[dict[str, object]], *, now_ms: int | None = None) -> int:
    """Return peak overlap from authoritative persisted subagent intervals."""
    terminal_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    boundaries: list[tuple[int, int]] = []
    for record in records:
        try:
            started_at = int(record.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if started_at <= 0:
            continue
        try:
            completed_at = int(record.get("completed_at") or 0)
        except (TypeError, ValueError):
            completed_at = 0
        end_at = max(started_at + 1, completed_at or terminal_ms)
        boundaries.append((started_at, 1))
        boundaries.append((end_at, -1))

    active = 0
    peak = 0
    # An end at t is processed before a start at t: adjacent runs are not
    # concurrent merely because millisecond timestamps touch.
    for _timestamp, delta in sorted(boundaries, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _runtime_subagent_metrics(runtime: AgentLoopSessionContext) -> dict[str, object] | None:
    metadata = runtime.metadata if isinstance(runtime.metadata, dict) else {}
    run_context = runtime.run_context
    agent_runtime = run_context.agent_runtime if run_context is not None else None
    parent_run_id = str(metadata.get("run_id") or "").strip()
    if agent_runtime is None or not parent_run_id:
        return None
    list_runs = getattr(agent_runtime, "list_runs", None)
    if not callable(list_runs):
        return None
    try:
        snapshot = list_runs(include_subagents=True)
    except Exception:
        return None
    raw_records = snapshot.get("subagents", []) if isinstance(snapshot, dict) else []
    records = [
        dict(item)
        for item in raw_records
        if isinstance(item, dict)
        and str(item.get("parent_run_id") or "") == parent_run_id
    ]
    if not records:
        return None
    statuses = Counter(
        str(item.get("status") or "unknown").strip() or "unknown"
        for item in records
    )
    return {
        "started": {
            str(item.get("subagent_id") or "").strip()
            for item in records
            if str(item.get("subagent_id") or "").strip()
        },
        "completed": {
            str(item.get("subagent_id") or "").strip()
            for item in records
            if str(item.get("subagent_id") or "").strip()
            and str(item.get("status") or "") == "completed"
        },
        "statuses": statuses,
        "peak_parallel": _peak_runtime_parallelism(records),
    }


def _subagent_metrics_from_runtime_events(
    events: list[tuple[str, dict[str, object]]],
) -> dict[str, object] | None:
    """Measure subagents from the same durable events consumed by the UI.

    Completed blocking subagents may be removed from the live runtime registry
    before an evaluation turn ends. Their start/done events remain authoritative
    and preserve the ordering needed to calculate actual overlap.
    """

    started: set[str] = set()
    completed: set[str] = set()
    active: set[str] = set()
    statuses: Counter[str] = Counter()
    peak_parallel = 0
    for event_type, data in events:
        subagent_id = str(data.get("subagent_id") or "").strip()
        if not subagent_id:
            continue
        if event_type == "subagent.start":
            started.add(subagent_id)
            active.add(subagent_id)
            peak_parallel = max(peak_parallel, len(active))
        elif event_type == "subagent.done":
            status = str(data.get("status") or "unknown").strip() or "unknown"
            statuses[status] += 1
            if status == "completed":
                completed.add(subagent_id)
            active.discard(subagent_id)
    if not started and not completed:
        return None
    return {
        "started": started,
        "completed": completed,
        "statuses": statuses,
        "peak_parallel": peak_parallel,
    }


def _subagent_event_from_task_tool(
    event_type: str,
    data: dict[str, object],
    tool_call_names: dict[str, str],
) -> tuple[str, dict[str, object]] | None:
    """Project blocking ``task`` tools onto the public subagent lifecycle.

    Repository evaluations consume the parent QueryEngine stream directly.
    That stream always contains task tool boundaries even when the app-level
    subagent event sink is not attached. The tool call id is stable for the
    complete blocking child run and is sufficient for contract accounting.
    """

    call_id = str(data.get("id") or "").strip()
    if not call_id:
        return None
    if event_type == "tool_call":
        name = str(data.get("name") or "").strip().lower()
        status = str(data.get("status") or "").strip().lower()
        if name == "task" and status == "running":
            return "subagent.start", {"subagent_id": call_id}
        return None
    if event_type != "tool_result":
        return None
    name = str(data.get("name") or data.get("tool_name") or "").strip().lower()
    if not name:
        name = str(tool_call_names.get(call_id) or "").strip().lower()
    result_kind = str(data.get("result_kind") or "").strip().lower()
    if name != "task" and result_kind != "subagent":
        return None
    tool_status = str(data.get("status") or "unknown").strip().lower() or "unknown"
    status = "completed" if tool_status in {"success", "partial"} else tool_status
    return "subagent.done", {"subagent_id": call_id, "status": status}


async def _run(prompt: str) -> int:
    workspace = Path(_required("MINICODE_EVAL_WORKSPACE")).resolve()
    initial_test_snapshot = _test_file_snapshot(workspace)
    base_url = os.environ.get("MINICODE_EVAL_BASE_URL", "https://api.openai.com/v1").strip()
    model = _required("MINICODE_EVAL_MODEL")
    wire_api = os.environ.get("MINICODE_EVAL_WIRE_API", "chat").strip() or "chat"
    settings = LLMSettings(
        api_key=_required("MINICODE_EVAL_API_KEY"),
        provider="custom",
        base_url=base_url,
        model=model,
        small_fast_model=os.environ.get(
            "MINICODE_EVAL_SMALL_FAST_MODEL",
            "",
        ).strip(),
        wire_api=wire_api,
        max_tokens=_optional_int_env("MINICODE_EVAL_MAX_TOKENS") or 0,
        reasoning_effort=os.environ.get("MINICODE_EVAL_REASONING_EFFORT", "").strip(),
        responses_reasoning_summary=os.environ.get(
            "MINICODE_EVAL_RESPONSES_REASONING_SUMMARY",
            "off",
        ).strip(),
        # The repository seed identifies an independent evaluation run. It is
        # not automatically forwarded as a provider sampling parameter because
        # competing agents do not expose equivalent semantics and many
        # OpenAI-compatible gateways only partially implement it.
        seed=_optional_int_env("MINICODE_EVAL_PROVIDER_SEED"),
        auth_header=_env_bool("MINICODE_EVAL_AUTH_HEADER"),
    )
    llm = build_wire_adapter(
        settings,
        thinking_budget=_optional_int_env("MINICODE_EVAL_THINKING_BUDGET"),
        provider_id="custom",
    )
    artifacts = ArtifactStore()
    registry = build_tool_registry(
        artifacts,
        workspace_root=workspace,
        llm_provider=lambda: llm,
    )
    # Repository evaluation is an autonomous host boundary: there is no
    # interactive approval channel. Grant only the concrete workspace mutation
    # capabilities required by this isolated task; the normal permission
    # checker, path ownership, sandbox policy, and tool capability floors still
    # evaluate every call.
    checker, permission = _repository_eval_permission(workspace)

    state = AgentState(user_message=prompt, max_iterations=int(os.environ.get("MINICODE_EVAL_MAX_ITERATIONS", "0")))
    runtime_spans: list[dict[str, object]] = []
    runtime_subagent_events: list[tuple[str, dict[str, object]]] = []
    task_tool_subagent_events: list[tuple[str, dict[str, object]]] = []

    async def capture_runtime_event(event_type: str, data: dict[str, object]) -> None:
        if event_type in {"subagent.start", "subagent.done"}:
            runtime_subagent_events.append((event_type, dict(data)))
            return
        if event_type != "runtime.span":
            return
        payload = dict(data)
        runtime_spans.append(payload)
        print(
            json.dumps(
                {"type": event_type, "data": _compact_trace_value(payload)},
                ensure_ascii=False,
            ),
            flush=True,
        )

    runtime = AgentLoopSessionContext(
        permission_context=permission,
        workspace_root=workspace,
        session_id=f"eval-{os.environ.get('MINICODE_EVAL_TASK_ID', 'task')}",
        task_id=os.environ.get("MINICODE_EVAL_TASK_ID", ""),
        emit_event=capture_runtime_event,
        metadata={
            "eval": True,
            "workspace_root": str(workspace),
            # An evaluator has no interactive user to approve a model-authored
            # draft plan. Keep plan events visible, but do not turn the run
            # into a permanent read-only permission mode.
            "autonomous_execution": True,
        },
    )
    submission = QuerySubmission(
        user_message=prompt,
        session=AgentSession(
            llm=llm,
            tool_registry=registry,
            artifact_store=artifacts,
            permission_checker=checker,
            # The evaluator owns this isolated checkout and records approval
            # through the same canonical boundary used by the desktop channel.
            approval_handler=_approve_isolated_eval_call,
            agent_settings=AgentSettings(
                max_iterations=state.max_iterations,
                # Evaluation must not add a hidden loop fuse. Set
                # MINICODE_EVAL_MAX_TOOL_CALLS explicitly for a bounded run.
                max_tool_calls=int(os.environ.get("MINICODE_EVAL_MAX_TOOL_CALLS", "0")),
                max_turn_seconds=_eval_max_turn_seconds(),
                live_text_streaming=False,
            ),
            token_budget=TokenBudget(total=200_000),
        ),
        state=state,
        runtime=runtime,
    )
    terminal = ""
    terminal_reason = ""
    event_counts: Counter[str] = Counter()
    tool_call_names: dict[str, str] = {}
    tool_result_statuses: Counter[str] = Counter()
    tool_error_kinds: Counter[str] = Counter()
    subagents_started: set[str] = set()
    subagents_completed: set[str] = set()
    active_subagents: set[str] = set()
    peak_parallel_subagents = 0
    subagent_statuses: Counter[str] = Counter()
    max_iteration = 0
    last_loop_metrics: dict[str, object] = {}
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    recovery_events: set[str] = set()
    invalid_search_count = 0
    final_text_parts: list[str] = []
    thinking_text_parts: list[str] = []
    thinking_chars = 0
    async for event in QueryEngine().submit(submission):
        event_counts[event.type] += 1
        event_data = event.data if isinstance(event.data, dict) else {}
        if event.type == "tool_call":
            _observe_tool_call(tool_call_names, event_data)
            projected = _subagent_event_from_task_tool(
                event.type,
                event_data,
                tool_call_names,
            )
            if projected is not None:
                task_tool_subagent_events.append(projected)
        elif event.type == "tool_result":
            tool_result_statuses[str(event_data.get("status") or "unknown")] += 1
            error_kind = str(event_data.get("error_kind") or "").strip()
            if error_kind:
                tool_error_kinds[error_kind] += 1
            tool_name = str(event_data.get("name") or event_data.get("tool_name") or "").lower()
            if "search" in tool_name and error_kind in {"no_match", "invalid_arguments", "not_found"}:
                invalid_search_count += 1
            projected = _subagent_event_from_task_tool(
                event.type,
                event_data,
                tool_call_names,
            )
            if projected is not None:
                task_tool_subagent_events.append(projected)
        elif event.type == "subagent.start":
            subagent_id = str(event_data.get("subagent_id") or "").strip()
            if subagent_id:
                subagents_started.add(subagent_id)
                active_subagents.add(subagent_id)
                peak_parallel_subagents = max(
                    peak_parallel_subagents,
                    len(active_subagents),
                )
        elif event.type == "subagent.done":
            subagent_id = str(event_data.get("subagent_id") or "").strip()
            status = str(event_data.get("status") or "unknown").strip() or "unknown"
            subagent_statuses[status] += 1
            if subagent_id and status == "completed":
                subagents_completed.add(subagent_id)
            active_subagents.discard(subagent_id)
        elif event.type == "item.completed":
            message_item = event_data.get("item") if isinstance(event_data.get("item"), dict) else {}
            if message_item.get("type") == "agent_message":
                final_text_parts[:] = [str(message_item.get("text") or "")]
        elif event.type in {"thinking_delta", "thinking"}:
            chunk = str(event_data.get("content") or "")
            if chunk and thinking_chars < 120_000:
                remaining = 120_000 - thinking_chars
                kept = chunk[:remaining]
                thinking_text_parts.append(kept)
                thinking_chars += len(kept)
        elif event.type == "stream_event":
            provider_usage = _usage_from_provider_stream_event(event_data)
            for key, value in provider_usage.items():
                usage_totals[key] += value
        event_payload = (
            event_data.get("payload")
            if isinstance(event_data.get("payload"), dict)
            else {}
        )
        raw_iteration = str(
            event_data.get("iteration_id")
            or event_payload.get("iteration_id")
            or ""
        )
        if raw_iteration.startswith("iter:"):
            try:
                max_iteration = max(max_iteration, int(raw_iteration.split(":", 1)[1]))
            except ValueError:
                pass
        metrics = _event_loop_metrics(event_data)
        if metrics:
            last_loop_metrics = metrics
        if str(event_data.get("phase") or "").lower() in {"recover", "recovery"}:
            recovery_events.add(str(event_data.get("id") or event_data.get("event_id") or len(recovery_events)))

        # JSONL remains human-auditable, but excludes token deltas and duplicate
        # raw artifact bodies so a bounded report retains the complete run.
        if _should_emit_trace_event(event.type, event_data):
            print(
                json.dumps(
                    {"type": event.type, "data": _trace_event_data(event.type, event_data)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if event.type == "done":
            terminal = str(event_data.get("status") or "")
            terminal_reason = str(event_data.get("reason") or "")
            final_usage = _usage_from_done_event(event_data)
            for key, value in final_usage.items():
                if value > 0 or usage_totals[key] == 0:
                    usage_totals[key] = value
    runtime_metrics = _subagent_metrics_from_runtime_events(runtime_subagent_events)
    if runtime_metrics is None:
        runtime_metrics = _subagent_metrics_from_runtime_events(
            task_tool_subagent_events
        )
    live_runtime_metrics = _runtime_subagent_metrics(runtime)
    if runtime_metrics is None:
        runtime_metrics = live_runtime_metrics
    elif live_runtime_metrics is not None:
        runtime_metrics["started"] = set(runtime_metrics["started"]) | set(
            live_runtime_metrics["started"]
        )
        runtime_metrics["completed"] = set(runtime_metrics["completed"]) | set(
            live_runtime_metrics["completed"]
        )
        runtime_metrics["peak_parallel"] = max(
            int(runtime_metrics["peak_parallel"]),
            int(live_runtime_metrics["peak_parallel"]),
        )
        if not runtime_metrics["statuses"]:
            runtime_metrics["statuses"] = live_runtime_metrics["statuses"]
    if runtime_metrics is not None:
        subagents_started = set(runtime_metrics["started"])
        subagents_completed = set(runtime_metrics["completed"])
        subagent_statuses = Counter(runtime_metrics["statuses"])
        peak_parallel_subagents = int(runtime_metrics["peak_parallel"])
    recovery_events.update(_runtime_recovery_ids(runtime_spans))
    tool_call_counts = Counter(tool_call_names.values())
    authoritative_iterations = _authoritative_iteration_count(
        max_iteration,
        last_loop_metrics,
    )
    provider_terminal_events = {
        "provider.request.completed",
        "provider.request.failed",
        "provider.request.cancelled",
    }
    provider_elapsed_ms = _runtime_elapsed_ms(
        runtime_spans,
        provider_terminal_events,
    )
    tool_elapsed_ms = _runtime_elapsed_ms(
        runtime_spans,
        {"tool.completed"},
    )
    side_calls = [
        dict(item)
        for item in (runtime.metadata or {}).get("_side_calls", [])
        if isinstance(item, dict)
    ]
    side_call_usage = {
        key: sum(
            int((item.get("usage") or {}).get(key) or 0)
            for item in side_calls
            if isinstance(item.get("usage"), dict)
        )
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "reasoning_output_tokens",
        )
    }
    side_call_elapsed_ms = sum(
        max(0, int(item.get("elapsed_ms") or 0)) for item in side_calls
    )
    test_integrity = _test_integrity_violations(initial_test_snapshot, workspace)
    test_integrity_satisfied = not any(test_integrity.values())
    print(
        json.dumps(
            {
                "type": "eval.driver.summary",
                "data": {
                    "agent": "minicode",
                    "model": model,
                    "terminal_status": terminal,
                    "terminal_reason": terminal_reason,
                    "iterations": authoritative_iterations,
                    "event_counts": dict(sorted(event_counts.items())),
                    "tool_calls": dict(sorted(tool_call_counts.items())),
                    "tool_result_statuses": dict(sorted(tool_result_statuses.items())),
                    "tool_error_kinds": dict(sorted(tool_error_kinds.items())),
                    "tool_call_count": sum(tool_call_counts.values()),
                    "provider_elapsed_ms": provider_elapsed_ms,
                    "tool_elapsed_ms": tool_elapsed_ms,
                    "side_call_count": len(side_calls),
                    "side_call_elapsed_ms": side_call_elapsed_ms,
                    "side_call_usage": side_call_usage,
                    "side_calls": _compact_trace_value(side_calls),
                    "test_integrity": test_integrity,
                    "seed_semantics": (
                        "provider_sampling"
                        if settings.seed is not None
                        else "evaluation_run_identifier"
                    ),
                    "tool_failure_count": sum(
                        count
                        for status, count in tool_result_statuses.items()
                        if status not in {"success", "partial"}
                    ),
                    "invalid_search_count": invalid_search_count,
                    "recovery_count": len(recovery_events),
                    "usage": usage_totals,
                    "subagents_started": sorted(subagents_started),
                    "subagents_completed": sorted(subagents_completed),
                    "peak_parallel_subagents": peak_parallel_subagents,
                    "subagent_statuses": dict(sorted(subagent_statuses.items())),
                    "final_text": _compact_trace_value("".join(final_text_parts)),
                    "thinking_text": _compact_trace_value("".join(thinking_text_parts)),
                    "thinking_chars": thinking_chars,
                    "loop_metrics": _compact_trace_value(last_loop_metrics),
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    required_subagents = max(0, int(os.environ.get("MINICODE_EVAL_MIN_SUBAGENTS", "0")))
    required_parallel_subagents = max(
        0,
        int(os.environ.get("MINICODE_EVAL_MIN_PARALLEL_SUBAGENTS", "0")),
    )
    maximum_subagents = max(0, int(os.environ.get("MINICODE_EVAL_MAX_SUBAGENTS", "0")))
    subagent_contract_satisfied = (
        len(subagents_started) >= required_subagents
        and len(subagents_completed) >= required_subagents
        and peak_parallel_subagents >= required_parallel_subagents
        and (maximum_subagents == 0 or len(subagents_started) <= maximum_subagents)
    )
    if not subagent_contract_satisfied:
        print(
            json.dumps(
                {
                    "type": "eval.driver.contract_failed",
                    "data": {
                        "contract": "minimum_completed_subagents",
                        "required": required_subagents,
                        "started": len(subagents_started),
                        "completed": len(subagents_completed),
                        "required_parallel": required_parallel_subagents,
                        "peak_parallel": peak_parallel_subagents,
                        "maximum": maximum_subagents,
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    if not test_integrity_satisfied:
        print(
            json.dumps(
                {
                    "type": "eval.driver.contract_failed",
                    "data": {
                        "contract": "pre_existing_test_integrity",
                        **test_integrity,
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return (
        0
        if terminal == "completed"
        and subagent_contract_satisfied
        and test_integrity_satisfied
        else 1
    )


def main() -> int:
    # The trace is JSONL with ensure_ascii=False; on Windows the default console
    # codepage (cp936) would encode it as mojibake that no reader can parse.
    ensure_utf8_console_logging()
    parser = argparse.ArgumentParser(description="Run MiniCode in repository-evaluation mode.")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    if args.model.strip():
        # This mutation is child-process local; RepositoryTaskRunner still owns
        # the credential/config allowlist at the host boundary.
        os.environ["MINICODE_EVAL_MODEL"] = args.model.strip()
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("empty task prompt", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(prompt))
    except Exception as exc:
        print(json.dumps({"driver_error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
