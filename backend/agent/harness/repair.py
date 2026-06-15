from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from backend.agent.state import AgentState
from backend.agent.harness.catalog import tool_spec_for
from backend.agent.harness.contracts import ToolSpec
from backend.llm.base import ToolCallEvent
from backend.tools.registry import ToolRegistry


class ResourceResolverProtocol(Protocol):
    def resolve(self, role: str) -> Any:
        """Resolve an argument role into a concrete tool argument value."""


def argument_has_value(args: dict[str, Any], field: str) -> bool:
    if field not in args:
        return False
    value = args.get(field)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def task_parallel_tasks_have_value(args: dict[str, Any] | None) -> bool:
    raw_tasks = (args or {}).get("parallel_tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) < 2:
        return False
    valid_count = 0
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        if argument_has_value(item, "description") and argument_has_value(item, "prompt"):
            valid_count += 1
    return valid_count >= 2




@dataclass(frozen=True)
class RepairResult:
    """Structured outcome for tool argument repair."""

    tool_call: ToolCallEvent
    status: str  # unchanged | repaired | needs_user_input | needs_model_generation | routing_correction | blocked
    user_message: str = ""
    model_observation: str = ""
    developer_detail: str = ""
    confidence: float = 0.0

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"

    @property
    def needs_user_input(self) -> bool:
        return self.status == "needs_user_input"

    @property
    def needs_model_generation(self) -> bool:
        return self.status == "needs_model_generation"

    @property
    def routing_correction(self) -> bool:
        return self.status == "routing_correction"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def resolved_value_is_safe(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _looks_like_workspace_path(value: Any) -> bool:
    """Heuristic: does the value look like a local workspace path rather than a URL or artifact ref."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.startswith(("http://", "https://", "artifact:", "mcp__")):
        return False
    return bool(text) and not text.startswith("<")


def _looks_like_url_or_artifact(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    return text.startswith(("http://", "https://", "artifact:", "art_"))


def _check_resource_routing(tc: ToolCallEvent, spec: ToolSpec) -> RepairResult | None:
    """Return routing_correction when a tool call targets a resource type the tool rejects."""
    if not spec.rejected_resource_types and not spec.accepted_resource_types:
        return None
    args = dict(tc.arguments or {})
    for arg_name, value in args.items():
        if not isinstance(value, str) or not value.strip():
            continue
        role = spec.role_for(arg_name)
        if "workspace" in role and "workspace_file" in spec.rejected_resource_types:
            return RepairResult(
                tool_call=tc,
                status="routing_correction",
                model_observation=(
                    f"Tool '{tc.name}' is for external documents/URLs, not workspace source files. "
                    f"Use read_file or grep_files for workspace files instead."
                ),
                developer_detail=f"routing: {tc.name} rejected resource type workspace_file for arg {arg_name}",
                confidence=1.0,
            )
        if role == "explicit_document_source" and _looks_like_workspace_path(value) and not _looks_like_url_or_artifact(value):
            return RepairResult(
                tool_call=tc,
                status="routing_correction",
                model_observation=(
                    f"Tool '{tc.name}' is for uploaded documents, artifact references, or URLs. "
                    f"The value '{value}' looks like a workspace file path. Use read_file or grep_files instead."
                ),
                developer_detail=f"routing: {tc.name} rejected workspace-like source for arg {arg_name}",
                confidence=1.0,
            )
        if role in {"latest_url", "search_query"} and "workspace_file" in spec.accepted_resource_types:
            continue
    return None


class ToolArgRepairEngine:
    """Central repair path for missing tool arguments."""

    def __init__(
        self,
        state: AgentState,
        tool_registry: ToolRegistry,
        resolver: ResourceResolverProtocol | None = None,
    ) -> None:
        self.state = state
        self.tool_registry = tool_registry
        self.resolver = resolver

    def repair_result(self, tc: ToolCallEvent) -> RepairResult:
        if self.tool_registry.get_tool(tc.name) is None:
            return RepairResult(tool_call=tc, status="unchanged", developer_detail="tool is not registered")
        if self.resolver is None:
            return RepairResult(tool_call=tc, status="unchanged", developer_detail="no resource resolver provided")
        spec = tool_spec_for(tc.name, self.tool_registry)

        routing = _check_resource_routing(tc, spec)
        if routing:
            return routing

        defaulted_args = self._apply_default_args(tc, spec.default_args or {})
        defaulted = defaulted_args is not tc.arguments
        if defaulted:
            tc = replace(tc, arguments=defaulted_args)
        if tc.name == "task" and task_parallel_tasks_have_value(tc.arguments):
            status = "repaired" if defaulted else "unchanged"
            return RepairResult(
                tool_call=tc,
                status=status,
                model_observation="Applied default argument(s)." if defaulted else "",
                developer_detail="task parallel_tasks satisfies required input",
                confidence=1.0,
            )
        if not spec.required_args:
            status = "repaired" if defaulted else "unchanged"
            return RepairResult(
                tool_call=tc,
                status=status,
                model_observation="Applied default argument(s)." if defaulted else "",
                developer_detail="tool has no required args",
                confidence=1.0,
            )
        args = dict(tc.arguments or {})
        repaired_fields: list[str] = []
        unresolved_fields: list[str] = []
        generation_fields: list[str] = []
        for arg in spec.required_args:
            if argument_has_value(args, arg):
                continue
            policy = spec.policy_for(arg)
            if policy == "needs_model_generation":
                generation_fields.append(arg)
                continue
            if policy == "routing_correction":
                return RepairResult(
                    tool_call=tc,
                    status="routing_correction",
                    model_observation=(
                        f"Tool '{tc.name}' requires an explicit external document source for '{arg}'. "
                        "Do not invent one. If the target is a workspace file, use read_file or grep_files; "
                        "if it is an uploaded document or URL, ask for or use that explicit source."
                    ),
                    developer_detail=f"routing correction required for missing {arg}",
                    confidence=1.0,
                )
            role = spec.role_for(arg)
            if not role:
                unresolved_fields.append(arg)
                continue
            value = self.resolver.resolve(role)
            if not resolved_value_is_safe(value):
                unresolved_fields.append(arg)
                continue
            args[arg] = value
            repaired_fields.append(arg)

        if generation_fields:
            names = ", ".join(generation_fields)
            repaired_call = replace(tc, arguments=args)
            missing_resources = f" Missing unresolved resource argument(s): {', '.join(unresolved_fields)}." if unresolved_fields else ""
            return RepairResult(
                tool_call=repaired_call,
                status="needs_model_generation",
                model_observation=(
                    f"Tool '{tc.name}' requires generated content for: {names}. "
                    f"Do not call {tc.name} with empty arguments. "
                    f"Generate the required content first, then call {tc.name} with all required fields."
                    f"{missing_resources}"
                ),
                developer_detail=f"generated args missing: {names}",
                confidence=1.0,
            )

        repaired_call = replace(tc, arguments=args) if repaired_fields else tc
        if repaired_fields:
            return RepairResult(
                tool_call=repaired_call,
                status="repaired",
                model_observation=f"Repaired missing argument(s): {', '.join(repaired_fields)}.",
                developer_detail=f"roles={spec.arg_roles or {}}",
                confidence=0.8,
            )
        if unresolved_fields and spec.empty_args_policy.endswith("ask"):
            return RepairResult(
                tool_call=tc,
                status="needs_user_input",
                user_message="I need one more detail before I can use this tool safely.",
                model_observation=f"Missing argument(s): {', '.join(unresolved_fields)}.",
                developer_detail=f"policy={spec.empty_args_policy}",
                confidence=0.0,
            )
        if unresolved_fields:
            return RepairResult(
                tool_call=tc,
                status="blocked",
                user_message="Tool call is missing required information.",
                model_observation=f"Missing argument(s): {', '.join(unresolved_fields)}.",
                developer_detail=f"policy={spec.empty_args_policy}",
                confidence=0.0,
            )
        return RepairResult(tool_call=tc, status="unchanged", confidence=1.0)

    def _apply_default_args(self, tc: ToolCallEvent, defaults: dict[str, Any]) -> dict[str, Any]:
        if not defaults:
            return tc.arguments
        args = dict(tc.arguments or {})
        changed = False
        for key, value in defaults.items():
            if argument_has_value(args, key):
                continue
            args[key] = value
            changed = True
        return args if changed else tc.arguments

    def repair(self, tc: ToolCallEvent) -> ToolCallEvent:
        return self.repair_result(tc).tool_call

    def missing_required_reason(self, tc: ToolCallEvent) -> str:
        if tc.name == "task" and task_parallel_tasks_have_value(tc.arguments):
            return ""
        spec = tool_spec_for(tc.name, self.tool_registry)
        missing = [
            arg
            for arg in spec.required_args
            if not argument_has_value(tc.arguments or {}, arg)
        ]
        if not missing:
            return ""
        generation = [a for a in missing if spec.policy_for(a) == "needs_model_generation"]
        if generation:
            names = ", ".join(generation)
            return (
                f"Tool '{tc.name}' needs generated content for: {names}. "
                f"Generate the required content first, then retry with all fields."
            )
        received = list((tc.arguments or {}).keys())
        guidance = spec.blocked_guidance or "Re-read the tool schema and retry with all required fields."
        return (
            f"Invalid tool call for '{tc.name}': missing required argument(s): {missing}. "
            f"Received keys: {received}. {guidance}"
        )
