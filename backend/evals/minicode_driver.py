"""Non-interactive driver for the repository-task evaluator.

This intentionally goes through ``QueryEngine.submit`` (the same lifecycle used
by the websocket session) rather than implementing a second miniature ReAct
loop.  It is useful for reproducing real repository tasks from CI or a shell.
Credentials are read only from the process environment.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import os
import sys
import time
from pathlib import Path

from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.agent.loop import AgentLoopSessionContext
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.checker import PermissionChecker
from backend.services.tool_registry_factory import build_tool_registry


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
    agent_runtime = metadata.get("agent_runtime")
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


async def _run(prompt: str) -> int:
    workspace = Path(_required("MINICODE_EVAL_WORKSPACE")).resolve()
    base_url = os.environ.get("MINICODE_EVAL_BASE_URL", "https://api.openai.com/v1").strip()
    model = _required("MINICODE_EVAL_MODEL")
    wire_api = os.environ.get("MINICODE_EVAL_WIRE_API", "chat").strip() or "chat"
    settings = LLMSettings(
        api_key=_required("MINICODE_EVAL_API_KEY"),
        base_url=base_url,
        model=model,
        wire_api=wire_api,
        max_tokens=_optional_int_env("MINICODE_EVAL_MAX_TOKENS") or 0,
        reasoning_effort=os.environ.get("MINICODE_EVAL_REASONING_EFFORT", "").strip(),
        # The repository seed identifies an independent evaluation run. It is
        # not automatically forwarded as a provider sampling parameter because
        # competing agents do not expose equivalent semantics and many
        # OpenAI-compatible gateways only partially implement it.
        seed=_optional_int_env("MINICODE_EVAL_PROVIDER_SEED"),
    )
    llm = OpenAIAdapter(settings=settings)
    artifacts = ArtifactStore()
    registry = build_tool_registry(artifacts, llm_provider=lambda: llm)
    checker = PermissionChecker(PermissionSettings(), workspace_root=workspace)
    permission = checker.build_context(mode="bypass", workspace_scope="worktree", source="repository_eval")
    state = AgentState(user_message=prompt, max_iterations=int(os.environ.get("MINICODE_EVAL_MAX_ITERATIONS", "0")))
    runtime = AgentLoopSessionContext(
        permission_context=permission,
        workspace_root=workspace,
        session_id=f"eval-{os.environ.get('MINICODE_EVAL_TASK_ID', 'task')}",
        task_id=os.environ.get("MINICODE_EVAL_TASK_ID", ""),
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
    tool_call_counts: Counter[str] = Counter()
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
    async for event in QueryEngine().submit(submission):
        event_counts[event.type] += 1
        event_data = event.data if isinstance(event.data, dict) else {}
        if event.type == "tool_call":
            tool_call_counts[str(event_data.get("name") or "unknown")] += 1
        elif event.type == "tool_result":
            tool_result_statuses[str(event_data.get("status") or "unknown")] += 1
            error_kind = str(event_data.get("error_kind") or "").strip()
            if error_kind:
                tool_error_kinds[error_kind] += 1
            tool_name = str(event_data.get("name") or event_data.get("tool_name") or "").lower()
            if "search" in tool_name and error_kind in {"no_match", "invalid_arguments", "not_found"}:
                invalid_search_count += 1
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
        elif event.type == "stream_event":
            provider_usage = _usage_from_provider_stream_event(event_data)
            for key, value in provider_usage.items():
                usage_totals[key] += value
        raw_iteration = str(event_data.get("iteration_id") or "")
        if raw_iteration.startswith("iter:"):
            try:
                max_iteration = max(max_iteration, int(raw_iteration.split(":", 1)[1]))
            except ValueError:
                pass
        metrics = event_data.get("loop_metrics")
        if isinstance(metrics, dict):
            last_loop_metrics = dict(metrics)
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
    runtime_metrics = _runtime_subagent_metrics(runtime)
    if runtime_metrics is not None:
        subagents_started = set(runtime_metrics["started"])
        subagents_completed = set(runtime_metrics["completed"])
        subagent_statuses = Counter(runtime_metrics["statuses"])
        peak_parallel_subagents = int(runtime_metrics["peak_parallel"])
    print(
        json.dumps(
            {
                "type": "eval.driver.summary",
                "data": {
                    "agent": "minicode",
                    "model": model,
                    "terminal_status": terminal,
                    "terminal_reason": terminal_reason,
                    "iterations": max_iteration,
                    "event_counts": dict(sorted(event_counts.items())),
                    "tool_calls": dict(sorted(tool_call_counts.items())),
                    "tool_result_statuses": dict(sorted(tool_result_statuses.items())),
                    "tool_error_kinds": dict(sorted(tool_error_kinds.items())),
                    "tool_call_count": sum(tool_call_counts.values()),
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
    return 0 if terminal == "completed" and subagent_contract_satisfied else 1


def main() -> int:
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
