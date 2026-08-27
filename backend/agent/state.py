"""
Agent 状态管理（DESIGN.md §7 Level 4 AgentState）。

AgentState 是 Agent Loop 的运行时状态载体，包含：
  - 任务进度
  - 工具调用历史
  - Artifact 引用
  - 活跃 Skill
  - 停滞检测数据
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal


_TRANSITION_HISTORY_LIMIT = 40

ToolCallStatus = Literal[
    "success",
    "error",  # legacy state restored from older checkpoints
    "failed",
    "blocked",
    "partial",
    "timeout",
    "cancelled",
]


@dataclass
class ToolCallRecord:
    """单次工具调用记录。"""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str | None = None
    artifact_id: str | None = None
    source_url: str | None = None
    extraction_status: str | None = None
    content_preview: str | None = None
    evidence_type: str | None = None
    provider: str | None = None
    provider_error_type: str | None = None
    error_kind: str | None = None
    user_summary: str | None = None
    developer_detail: str | None = None
    recoverable: bool = True
    projection: str | None = None
    model_observation: str | None = None
    turn_id: str | None = None  # Correlates tool calls with conversation turns
    iteration_id: str = ""  # Agent loop iteration that produced this call
    status: ToolCallStatus = "success"
    request_digest: str = ""
    cleanup_receipt: dict[str, Any] = field(default_factory=dict)


# Terminal-reason vocabulary for run termination.
TerminalReason = Literal[
    "completed",
    "interrupted",
    "budget_exceeded",
    "max_iterations",
    "max_tool_calls",
    "max_turn_seconds",
    "max_turn_tokens",
    "max_turn_cost_usd",
    "max_output",
    "context_window",
    "partial_stream_error",
    "stream_error",
    "partial_timeout",
    "timeout",
    "partial_api_error",
    "api_error",
    "api",
    "auth",
    "blocked",
    "provider_capability",
    "provider_protocol",
    "provider_continuation",
    "incomplete_tool_stream",
    "tool_error",
    "empty_reply",
    "missing_final_answer",
    "refusal",
    "max_retries",
    "invalid_model_action",
    "runtime_error",
    "billing",
    "model",
    "unknown",
]

TerminalStatus = Literal["completed", "partial", "cancelled", "failed"]


@dataclass
class AgentState:
    """
    Agent 运行时状态。

    生命周期：一次用户消息处理过程。
    """

    user_message: str
    max_iterations: int = 0  # 展示用；0 = 不限制（终止判定在 TurnBudgetController）

    # ── 执行状态 ──
    iterations: int = 0
    # Provider requests spent on recovery rather than on new work: stop-hook
    # feedback and steering. They
    # are already bounded by ``total_retries``
    # (agent.turn_error_budget), so charging them to the work budget as well
    # would end a turn early precisely when it is repairing itself.
    # ``iterations`` stays the raw provider-request counter: event ids
    # (``iter:N``), checkpoints and resume all key off it.
    recovery_iterations: int = 0
    reply: str = ""
    stopped_reason: TerminalReason | None = None
    # A semantic reason such as ``max_iterations`` does not by itself say
    # whether useful work was retained. Keep the externally visible outcome
    # explicit so every lifecycle consumer projects the same terminal state.
    terminal_status: TerminalStatus | None = None

    # ── 工具记录 ──
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    # ── 上下文增强（Phase 2 新增）──
    active_skills: list[str] = field(default_factory=list)
    retrieved_chunks: list[str] = field(default_factory=list)
    task_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    workspace_context: Any = None  # WorkspaceContext 实例（项目上下文）
    attachments: list[dict[str, Any]] = field(default_factory=list)
    evidence_records: list[Any] = field(default_factory=list)
    prompt_context: dict[str, Any] = field(default_factory=dict)

    # Tool-call sequence is retained for checkpoint compatibility and mutation
    # provenance. It does not infer whether repeated work is useful.
    _tool_sequence: int = 0
    _last_mutation_index: int = 0
    # Shared counter for every loop-owned recovery that starts another provider
    # attempt. Provider-internal transport retries retain their own bounded policy.
    total_retries: int = 0
    # Zero explicitly disables the shared recovery fuse.
    max_total_retries: int = 0
    # Recovery-ladder flags (declared instead of dynamic setattr so the ladder
    # is greppable and resettable in one place).
    reactive_compaction_attempted: bool = False
    max_output_recovery_count: int = 0
    max_output_partial_text: str = ""
    max_output_no_progress_count: int = 0
    max_output_last_partial_text: str = ""
    provider_continuation_recovery_count: int = 0
    stop_hook_feedback_count: int = 0
    # Consecutive auto-compaction failures. Once this reaches
    # MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES the loop stops attempting doomed
    # compactions instead of hammering the API every turn. Reset on success.
    consecutive_autocompact_failures: int = 0
    # Whether the approaching-compaction notice was already sent, so the
    # boundary announces once per band instead of on every turn.
    budget_warning_emitted: bool = False
    # Last loop transition reason, for observability/debugging. Not load-bearing.
    transition: str = ""
    transition_details: dict[str, Any] = field(default_factory=dict)
    transition_history: list[dict[str, Any]] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)
    # Deferred tools selected through tool_search become ordinary direct tools
    # on the following model iteration. Tool selection is kept in state so the
    # model should call the selected tool itself, not a MiniCode-only
    # tool_describe -> tool_call wrapper protocol.
    loaded_deferred_tools: set[str] = field(default_factory=set)
    tool_runtime_guidance: str = ""
    # UI projection bookkeeping belongs to the turn state schema as well;
    # declaring it prevents an eventual slots migration from breaking tools.
    ui_tool_started_at: dict[str, float] = field(default_factory=dict)
    # The rewind manager for write tools. ``snapshot_before_write`` refuses
    # every write tool when this is None, so the turn bootstrap defaults it.
    checkpoint_manager: Any | None = None

    def clear_transition(self) -> None:
        self.transition = ""
        self.transition_details.clear()
        self.transition_history.clear()

    def mark_transition(self, reason: str, **details: Any) -> None:
        """Record why the loop will continue or retry."""
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            return
        clean_details = {
            str(key): self._transition_value(value)
            for key, value in details.items()
            if value is not None and value != ""
        }
        self.transition = clean_reason
        self.transition_details = clean_details
        record: dict[str, Any] = {
            "reason": clean_reason,
            "iteration": self.iterations,
        }
        if clean_details:
            record["details"] = clean_details
        self.transition_history.append(record)
        if len(self.transition_history) > _TRANSITION_HISTORY_LIMIT:
            del self.transition_history[:-_TRANSITION_HISTORY_LIMIT]

    def transition_payload(self, *, default_reason: str = "") -> dict[str, Any]:
        reason = self.transition or str(default_reason or "").strip()
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        if self.transition_details:
            payload["details"] = dict(self.transition_details)
        if self.transition_history:
            payload["history_length"] = len(self.transition_history)
        return payload

    @classmethod
    def _transition_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= 240 else f"{value[:237]}..."
        if isinstance(value, dict):
            return {
                str(key): cls._transition_value(inner)
                for key, inner in list(value.items())[:20]
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._transition_value(item) for item in list(value)[:20]]
        return str(value)[:240]

    def disable_tools(self, names: set[str]) -> None:
        self.disabled_tools.update(names)

    @property
    def work_iterations(self) -> int:
        """Admitted model iterations, excluding inner transport retries.

        ``iterations`` advances once at admission. Provider retries already
        have a separate retry budget, so subtracting them raised this hard cap.
        """
        return max(0, self.iterations)

    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        output: str,
        artifact_id: str | None = None,
        is_error: bool = False,
        status: ToolCallStatus | None = None,
        mutates: bool = False,
        source_url: str | None = None,
        extraction_status: str | None = None,
        content_preview: str | None = None,
        evidence_type: str | None = None,
        provider: str | None = None,
        provider_error_type: str | None = None,
        error_kind: str | None = None,
        user_summary: str | None = None,
        developer_detail: str | None = None,
        recoverable: bool = True,
        projection: str | None = None,
        model_observation: str | None = None,
        turn_id: str | None = None,
        iteration_id: str = "",
        request_digest: str = "",
        cleanup_receipt: dict[str, Any] | None = None,
    ) -> None:
        """记录一次工具调用。"""
        resolved_status: ToolCallStatus
        if status is not None:
            resolved_status = status
        else:
            resolved_status = "error" if is_error else "success"

        self.tool_calls.append(
            ToolCallRecord(
                tool_name=name,
                tool_input=deepcopy(args),
                tool_output=output,
                artifact_id=artifact_id,
                source_url=source_url,
                extraction_status=extraction_status,
                content_preview=content_preview,
                evidence_type=evidence_type,
                provider=provider,
                provider_error_type=provider_error_type,
                error_kind=error_kind,
                user_summary=user_summary,
                developer_detail=developer_detail,
                recoverable=recoverable,
                projection=projection,
                model_observation=model_observation,
                turn_id=turn_id,
                iteration_id=iteration_id,
                status=resolved_status,
                request_digest=str(request_digest or "").strip(),
                cleanup_receipt=deepcopy(cleanup_receipt or {}),
            )
        )
        if artifact_id:
            self.artifact_refs.append(artifact_id)

        # Preserve the checkpoint-compatible sequence for mutation provenance.
        self._tool_sequence += 1

        if mutates and resolved_status == "success":
            self._last_mutation_index = self._tool_sequence

    def rebuild_tool_call_accounting(self) -> None:
        """Restore checkpoint-compatible tool sequence accounting."""
        self._tool_sequence = len(self.tool_calls)
