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

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal


_TRANSITION_HISTORY_LIMIT = 40


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
    projection: str | None = None
    turn_id: str | None = None  # Codex-style: correlates tool calls with conversation turns
    iteration_id: str = ""  # Agent loop iteration that produced this call
    status: Literal["success", "error", "failed", "blocked", "partial"] = "success"


# Terminal-reason vocabulary for run termination.
TerminalReason = Literal[
    "completed",
    "interrupted",
    "budget_exceeded",
    "max_iterations",
    "max_tool_calls",
    "stagnation",
    "partial_stream_error",
    "recovered_stream_error",
    "stream_error",
    "partial_timeout",
    "recovered_timeout",
    "timeout",
    "partial_api_error",
    "recovered_api_error",
    "api_error",
    "api",
    "auth",
    "blocked",
    "provider_capability",
    "incomplete_tool_stream",
    "tool_error",
    "empty_reply",
    "max_retries",
    "invalid_model_action",
    "billing",
    "model",
    "unknown",
]


@dataclass
class AgentState:
    """
    Agent 运行时状态。

    生命周期：一次用户消息处理过程。
    """

    user_message: str
    max_iterations: int = 60

    # ── 执行状态 ──
    iterations: int = 0
    reply: str = ""
    stopped_reason: TerminalReason | None = None

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

    # ── 停滞检测数据 ──
    _tool_call_hashes: dict[str, int] = field(default_factory=dict)
    _tool_call_labels: dict[str, str] = field(default_factory=dict)
    _tool_call_last_index: dict[str, int] = field(default_factory=dict)
    _last_tool_call_hash: str | None = None
    _consecutive_tool_call_count: int = 0
    _tool_sequence: int = 0
    _last_mutation_index: int = 0
    blocked_repeat_calls: int = 0
    blocked_category_counts: dict[str, int] = field(default_factory=dict)
    _last_blocked_category: str | None = None
    heal_attempts: int = 0
    max_heal_attempts: int = 2
    # Shared retry counter for the heal / future-action / coordinator paths
    # (incremented at the _looks_like_future_action_only_answer, heal/confidence
    # gate, and coordinator_finalization_feedback branches). Stream, max-output
    # and empty-reply recovery each have their OWN bounded ladder instead
    # (stream_retry.stream_max_attempts, _MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    # empty_reply_retries), so a single turn can perform more recovery steps
    # than max_total_retries. Consolidating them here is a future option; today
    # this counter gates only the three paths above.
    total_retries: int = 0
    max_total_retries: int = 5
    # Action-level verification (verify-after-edit) tracking.
    verify_attempts: int = 0
    max_verify_attempts: int = 2
    last_verified_mutation_index: int = 0
    # Recovery-ladder flags (declared instead of dynamic setattr so the ladder
    # is greppable and resettable in one place).
    empty_reply_retries: int = 0
    stop_hook_feedback_used: bool = False
    web_grounding_retry_used: bool = False
    consecutive_compaction_failures: int = 0  # circuit breaker for ineffective compactions
    reactive_compaction_attempted: bool = False
    max_output_recovery_count: int = 0
    max_output_recovered_text: str = ""
    # Last loop transition reason, for observability/debugging. Not load-bearing.
    transition: str = ""
    transition_details: dict[str, Any] = field(default_factory=dict)
    transition_history: list[dict[str, Any]] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)
    tool_runtime_guidance: str = ""
    loop_guidance: list[str] = field(default_factory=list)

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

    def disable_tools(self, names: set[str], guidance: str = "") -> None:
        self.disabled_tools.update(names)
        if guidance:
            self.add_loop_guidance(guidance)

    @property
    def has_unverified_mutations(self) -> bool:
        """True when successful workspace mutations happened after the last verify run."""
        return self._last_mutation_index > self.last_verified_mutation_index

    def mark_mutations_verified(self) -> None:
        self.last_verified_mutation_index = self._last_mutation_index

    def add_loop_guidance(self, guidance: str) -> None:
        text = guidance.strip()
        if text and text not in self.loop_guidance:
            self.loop_guidance.append(text)

    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        output: str,
        artifact_id: str | None = None,
        is_error: bool = False,
        status: Literal["success", "error", "failed", "blocked", "partial"] | None = None,
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
        projection: str | None = None,
        turn_id: str | None = None,
        iteration_id: str = "",
    ) -> None:
        """记录一次工具调用。"""
        resolved_status: Literal["success", "error", "failed", "blocked", "partial"]
        if status is not None:
            resolved_status = status
        else:
            resolved_status = "error" if is_error else "success"

        self.tool_calls.append(
            ToolCallRecord(
                tool_name=name,
                tool_input=args,
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
                projection=projection,
                turn_id=turn_id,
                iteration_id=iteration_id,
                status=resolved_status,
            )
        )
        if artifact_id:
            self.artifact_refs.append(artifact_id)

        # 停滞检测：记录 hash (use normalized args for semantic dedup)
        call_hash = self._hash_call(name, self._normalize_args(name, args))
        self._tool_sequence += 1
        self._tool_call_hashes[call_hash] = (
            self._tool_call_hashes.get(call_hash, 0) + 1
        )
        self._tool_call_labels.setdefault(call_hash, self._label_call(name, args))
        self._tool_call_last_index[call_hash] = self._tool_sequence

        if self._last_tool_call_hash == call_hash:
            self._consecutive_tool_call_count += 1
        else:
            self._last_tool_call_hash = call_hash
            self._consecutive_tool_call_count = 1

        if mutates and resolved_status == "success":
            self._last_mutation_index = self._tool_sequence

        nonfatal_projection = projection in {"silent", "status", "warning"}
        nonfatal_issue = error_kind in {
            "missing_generated_content",
            "routing_error",
            "stale_evidence",
            "repeat_guard",
            "tool_disabled",
        }
        if resolved_status == "blocked" and not (nonfatal_projection or nonfatal_issue):
            self.blocked_repeat_calls += 1
            category = self._blocked_category(output)
            if category:
                if self._last_blocked_category == category:
                    self.blocked_category_counts[category] = self.blocked_category_counts.get(category, 0) + 1
                else:
                    self._last_blocked_category = category
                    self.blocked_category_counts[category] = 1
        else:
            self.blocked_repeat_calls = 0
            self._last_blocked_category = None

    def repeat_count(self, name: str, args: dict[str, Any]) -> int:
        """Return how many times the exact tool call has already been recorded."""
        return self._tool_call_hashes.get(self.call_signature(name, args), 0)

    def call_signature(self, name: str, args: dict[str, Any]) -> str:
        """Stable signature for one tool name + argument set."""
        return self._hash_call(name, self._normalize_args(name, args))

    @staticmethod
    def _normalize_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Normalize tool arguments for semantic deduplication.

        Resolves path aliases and strips irrelevant differences so that
        read_file(file_path='./src/foo.py') and read_file(path='src/foo.py')
        produce the same signature.
        """
        normalized = dict(args)

        path_keys = ("file_path", "path", "target", "filename", "directory")
        for key in path_keys:
            value = normalized.get(key)
            if isinstance(value, str) and value:
                clean = value.replace("\\", "/").strip().lower()
                if clean.startswith("./"):
                    clean = clean[2:]
                normalized[key] = clean

        if name in ("read_file", "write_file", "edit_file", "list_files"):
            path_value = ""
            for key in path_keys:
                if normalized.get(key):
                    path_value = str(normalized[key])
                    break
            if path_value:
                for key in path_keys:
                    normalized.pop(key, None)
                normalized["_normalized_path"] = path_value

        normalized.pop("expected_hash", None)

        return normalized

    def find_last_tool_call(
        self,
        name: str,
        args: dict[str, Any],
    ) -> ToolCallRecord | None:
        """Find the latest record for the exact same tool name and arguments."""
        call_hash = self.call_signature(name, args)
        for record in reversed(self.tool_calls):
            if self.call_signature(record.tool_name, record.tool_input) == call_hash:
                return record
        return None

    def repeated_call_guard_reason(
        self,
        name: str,
        args: dict[str, Any],
        *,
        limit: int = 3,
    ) -> str:
        """
        Return a PreToolUse-style block reason for repeated failed calls.

        Successful repeated tool use is part of a normal ReAct loop. This guard
        only stops the model from replaying the same blocked/error action with
        no new observation or workspace-changing action in between.
        """
        last = self.find_last_tool_call(name, args)
        if last is None or last.status == "success":
            return ""
        # Web tools are handled by the unified guardrail controller for exact
        # repeats/no-progress. Avoid an extra state-level block here so the
        # model can change query/source instead of being trapped in retry loops.
        if name in {"web_search", "search_web"}:
            return ""

        call_hash = self.call_signature(name, args)
        count = self._tool_call_hashes.get(call_hash, 0)
        if count <= 0:
            return ""

        last_call_index = self._tool_call_last_index.get(call_hash, 0)
        if self._last_mutation_index > last_call_index:
            return ""

        consecutive = (
            self._consecutive_tool_call_count
            if self._last_tool_call_hash == call_hash
            else 0
        )
        repeat_threshold = max(limit - 1, 1)
        if consecutive < 1 and count < repeat_threshold:
            return ""

        label = self._tool_call_labels.get(call_hash, self._label_call(name, args))
        return (
            f"Skipped repeated failed tool call before execution: {label}. "
            "The previous result is already in the conversation above. "
            "Use that observation, change the arguments, choose a different tool, "
            "or answer from the information already gathered."
        )

    def rebuild_stagnation_accounting(self) -> None:
        """Rebuild stagnation/repeat bookkeeping from ``self.tool_calls``.

        Checkpoint resume restores ``tool_calls`` and the persisted
        ``_last_mutation_index`` / ``last_verified_mutation_index`` but not the
        derived hash/sequence maps, which would otherwise stay empty and disable
        the repeat-call guard and stagnation detection for the rest of the run.
        Only the derived maps and ``_tool_sequence`` are recomputed; the mutation
        indices are preserved as restored from the checkpoint.
        """
        self._tool_sequence = 0
        self._tool_call_hashes = {}
        self._tool_call_labels = {}
        self._tool_call_last_index = {}
        self._last_tool_call_hash = None
        self._consecutive_tool_call_count = 0
        for record in self.tool_calls:
            name = record.tool_name
            args = record.tool_input or {}
            call_hash = self._hash_call(name, self._normalize_args(name, args))
            self._tool_sequence += 1
            self._tool_call_hashes[call_hash] = self._tool_call_hashes.get(call_hash, 0) + 1
            self._tool_call_labels.setdefault(call_hash, self._label_call(name, args))
            self._tool_call_last_index[call_hash] = self._tool_sequence
            if self._last_tool_call_hash == call_hash:
                self._consecutive_tool_call_count += 1
            else:
                self._last_tool_call_hash = call_hash
                self._consecutive_tool_call_count = 1

    def is_stagnant(self, limit: int = 3) -> bool:
        """
        检测是否停滞（DESIGN.md §7 终止条件）。

        同一工具+同一参数调用 ≥ limit 次判定停滞。
        """
        return (
            any(count >= limit for count in self._tool_call_hashes.values())
            or any(count >= limit for count in self.blocked_category_counts.values())
        )

    def get_stagnation_detail(self, limit: int = 3) -> str:
        """获取停滞的详细信息。"""
        for category, count in self.blocked_category_counts.items():
            if count >= limit:
                if category == "shell_file_write":
                    return f"run_command shell file writes were blocked {count} times; use write_file or edit_file instead."
                return f"Blocked tool category {category} repeated {count} times."
        for call_hash, count in self._tool_call_hashes.items():
            if count >= limit:
                label = self._tool_call_labels.get(call_hash, "相同工具调用")
                return f"{label} 已重复 {count} 次"
        return ""

    @staticmethod
    def _blocked_category(output: str) -> str:
        text = str(output or "")
        if (
            "Blocked run_command because it appears to create or edit files through the shell" in text
            or "Use write_file for complete file writes or edit_file for targeted changes" in text
        ):
            return "shell_file_write"
        return ""

    @staticmethod
    def _hash_call(name: str, args: dict[str, Any]) -> str:
        """生成工具调用的 hash（用于去重检测）。"""
        raw = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _label_call(name: str, args: dict[str, Any]) -> str:
        """Human-readable label for repeated-call diagnostics."""
        try:
            raw_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_args = str(args)
        if len(raw_args) > 160:
            raw_args = f"{raw_args[:157]}..."
        return f"{name}({raw_args})"
