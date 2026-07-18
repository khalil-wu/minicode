from __future__ import annotations

from typing import Any

from backend.agent.runtime import AgentRuntime, default_runtime
from backend.memory.manager import MEMORY_TYPES, MemoryFact, MemoryManager, MemoryType
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    TOOL_SIDE_EFFECT_WORKSPACE,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
    truncate_tool_result,
)
from backend.tools.contracts import ToolSpec
from backend.tools.subagent_runtime import runtime_from_context


class TeamMemorySyncTool(BaseTool):
    """Persist durable team findings into file-backed memory."""

    name = "team_memory_sync"
    description = (
        "Write explicit team findings or completed shared task outputs into file-backed memory. "
        "Use only for durable project/user/feedback/reference facts that should survive compaction; "
        "do not store facts that are directly derivable from current code."
    )
    permission = PermissionLevel.CONFIRM
    mutates_workspace = True
    side_effect_kind = TOOL_SIDE_EFFECT_WORKSPACE
    idempotent = False
    result_kind = "memory"
    activity_kind = "genericTool"
    display_label = "Team memory"
    max_result_chars = None

    _MAX_FACTS = 50
    _MAX_FACT_CHARS = 1200

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._memory_manager = memory_manager
        self._runtime = runtime

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="memory.team_sync",
            toolset="memory",
            exposure="deferred",
            required_args=(),
            arg_roles={
                "facts": "generated_content",
                "summary": "generated_content",
                "fact_type": "control",
                "task_ids": "control",
            },
            repair_policy={"facts": "needs_model_generation", "summary": "needs_model_generation"},
            empty_args_policy="block",
            blocked_guidance=(
                "Provide durable facts/summary to save, or set include_completed_task_outputs=true "
                "with filters for completed shared tasks."
            ),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "description": "Durable facts to append. Items may be strings or objects with type/text.",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": list(MEMORY_TYPES)},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["text"],
                                },
                            ]
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional single durable summary to append.",
                    },
                    "fact_type": {
                        "type": "string",
                        "enum": list(MEMORY_TYPES),
                        "description": "Default memory type for string facts and task outputs. Defaults to project.",
                    },
                    "include_completed_task_outputs": {
                        "type": "boolean",
                        "description": "Also save outputs from completed shared swarm tasks matching the filters.",
                    },
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional shared task ids to sync.",
                    },
                    "team_name": {
                        "type": "string",
                        "description": "Optional team filter for shared tasks.",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Optional assignee filter for shared tasks.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self._MAX_FACTS,
                        "description": "Maximum facts/task outputs to append. Defaults to 20.",
                    },
                },
            },
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        payload = args or {}
        fact_type = _memory_type(payload.get("fact_type"), default="project")
        if fact_type is None:
            return f"fact_type must be one of: {', '.join(MEMORY_TYPES)}"
        if "limit" in payload:
            try:
                limit = int(payload.get("limit"))
            except (TypeError, ValueError):
                return "limit must be an integer"
            if limit < 1 or limit > self._MAX_FACTS:
                return f"limit must be between 1 and {self._MAX_FACTS}"
        facts = payload.get("facts")
        if facts is not None and not isinstance(facts, list):
            return "facts must be an array"
        task_ids = payload.get("task_ids")
        if task_ids is not None and not isinstance(task_ids, list):
            return "task_ids must be an array"
        return ""

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        validation = self.validate_input(args)
        if validation:
            return self._error_result(validation)

        manager = self._get_memory_manager(context)
        if manager is None:
            return self._error_result("File memory is not available")

        default_type = _memory_type(args.get("fact_type"), default="project") or "project"
        limit = int(args.get("limit") or 20)
        facts: list[MemoryFact] = []

        summary = str(args.get("summary") or "").strip()
        if summary:
            facts.append(MemoryFact(default_type, _clip_fact(summary)))

        for raw_fact in args.get("facts") or []:
            fact = _coerce_tool_fact(raw_fact, default_type=default_type)
            if fact is not None:
                facts.append(fact)

        include_outputs = bool(args.get("include_completed_task_outputs"))
        if include_outputs or args.get("task_ids"):
            facts.extend(self._collect_task_output_facts(args, context, default_type=default_type, limit=limit))

        clean_facts = _dedupe_facts(facts)[:limit]
        if not clean_facts:
            return ToolResult(
                content="No durable memory facts matched. Nothing was written.",
                result_kind=self.result_kind,
                display_summary="No team memory synced",
            )

        wrote = manager.append_facts(clean_facts)
        if not wrote:
            return ToolResult(
                content="No new team memory facts were written; they may already exist or were filtered out.",
                result_kind=self.result_kind,
                display_summary="Team memory unchanged",
            )

        lines = [f"Synced {len(clean_facts)} fact(s) into file-backed memory:"]
        for fact in clean_facts:
            lines.append(f"- [{fact.type}] {fact.text}")
        return ToolResult(
            content=truncate_tool_result("\n".join(lines), 8000),
            result_kind=self.result_kind,
            display_summary=f"Synced {len(clean_facts)} memory fact(s)",
        )

    def _collect_task_output_facts(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None,
        *,
        default_type: MemoryType,
        limit: int,
    ) -> list[MemoryFact]:
        runtime = self._get_runtime(context)
        task_ids = [str(item).strip() for item in args.get("task_ids") or [] if str(item).strip()]
        tasks: list[Any] = []
        if task_ids:
            for task_id in task_ids:
                task = runtime.get_swarm_task(task_id)
                if task is not None:
                    tasks.append(task)
        else:
            tasks = runtime.list_swarm_tasks(
                assignee=str(args.get("assignee") or "").strip(),
                status="completed",
                team_name=str(args.get("team_name") or "").strip(),
                conversation_id=str(getattr(context, "conversation_id", "") or "").strip(),
                limit=limit,
            )

        output_facts: list[MemoryFact] = []
        for task in tasks:
            outputs = getattr(task, "outputs", []) or []
            for output in outputs:
                text = str(getattr(output, "content", "") or "").strip()
                if not text:
                    continue
                title = str(getattr(task, "title", "") or "").strip()
                task_id = str(getattr(task, "task_id", "") or "").strip()
                author = str(getattr(output, "author_id", "") or "").strip()
                prefix = f"Team task {task_id}"
                if title:
                    prefix += f" ({title})"
                if author:
                    prefix += f" by {author}"
                output_facts.append(MemoryFact(default_type, _clip_fact(f"{prefix}: {text}")))
                if len(output_facts) >= limit:
                    return output_facts
        return output_facts

    def _get_runtime(self, context: ToolExecutionContext | None) -> AgentRuntime:
        return self._runtime or runtime_from_context(context) or default_runtime()

    def _get_memory_manager(self, context: ToolExecutionContext | None) -> MemoryManager | None:
        if self._memory_manager is not None:
            return self._memory_manager
        metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
        candidate = metadata.get("memory_manager")
        if isinstance(candidate, MemoryManager):
            return candidate
        file_memory = metadata.get("file_memory")
        if file_memory is not None:
            return MemoryManager(file_memory)
        try:
            from backend.api import _state
        except Exception:
            return None
        bootstrap = getattr(_state, "bootstrap", None)
        file_memory = getattr(bootstrap, "file_memory", None) if bootstrap else None
        if file_memory is None:
            return None
        return MemoryManager(file_memory)


def _memory_type(raw: Any, *, default: MemoryType) -> MemoryType | None:
    value = str(raw or default).strip().lower()
    return value if value in MEMORY_TYPES else None  # type: ignore[return-value]


def _coerce_tool_fact(raw: Any, *, default_type: MemoryType) -> MemoryFact | None:
    if isinstance(raw, str):
        text = raw.strip()
        return MemoryFact(default_type, _clip_fact(text)) if text else None
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    memory_type = _memory_type(raw.get("type"), default=default_type) or default_type
    return MemoryFact(memory_type, _clip_fact(text))


def _clip_fact(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= TeamMemorySyncTool._MAX_FACT_CHARS:
        return clean
    return clean[: TeamMemorySyncTool._MAX_FACT_CHARS - 3].rstrip() + "..."


def _dedupe_facts(facts: list[MemoryFact]) -> list[MemoryFact]:
    seen: set[tuple[str, str]] = set()
    out: list[MemoryFact] = []
    for fact in facts:
        text = fact.text.strip()
        if not text:
            continue
        key = (fact.type, text.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(MemoryFact(fact.type, text))
    return out
