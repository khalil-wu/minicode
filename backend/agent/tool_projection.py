from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult


@dataclass(frozen=True)
class ToolProjection:
    """UI-facing projection metadata for a tool call/result pair."""

    result_kind: str
    display_hint: str
    input_summary: str = ""
    activity_kind: str = ""
    display_scope: str = "activity"
    panel_hint: str = "inspector"
    requires_attention: bool = False


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


def _short_label(value: str, max_len: int = 72) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _hostname(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return ""
    return parsed.netloc or parsed.path.split("/")[0]


def _mcp_server_and_tool(name: str) -> tuple[str, str]:
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp":
        return parts[1], parts[2]
    return "", ""


def _with_target(label: str, target: str) -> str:
    return f"{label} {_short_label(target)}" if target else label


def _read_file_line_range_suffix(args: dict[str, Any]) -> str:
    """Build a line-range suffix like ' L100-L200' for read_file args."""
    start_raw = args.get("start_line") or args.get("startLine")
    end_raw = args.get("end_line") or args.get("endLine")
    start: int | None = None
    end: int | None = None
    try:
        start = int(start_raw) if start_raw is not None else None
    except (TypeError, ValueError):
        pass
    try:
        end = int(end_raw) if end_raw is not None else None
    except (TypeError, ValueError):
        pass
    if start and end:
        return f" L{start}-L{end}"
    if start:
        return f" L{start}+"
    if end:
        return f" L1-L{end}"
    return ""


def _parallel_task_descriptions(args: dict[str, Any]) -> list[str]:
    raw = args.get("parallel_tasks")
    if not isinstance(raw, list):
        return []
    descriptions: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("description") or item.get("objective") or item.get("prompt") or "").strip()
        if text:
            descriptions.append(_short_text(text, 72))
    return descriptions


def _workflow_step_count(args: dict[str, Any]) -> int:
    raw_steps = args.get("steps")
    return len(raw_steps) if isinstance(raw_steps, list) else 0


def user_facing_tool_name(tool_name: str, *, cjk: bool = False) -> str:
    name = str(tool_name or "").strip()
    normalized = name.lower()
    if normalized in {"web_search", "search_web"}:
        return "搜索" if cjk else "web search"
    if normalized == "web_fetch":
        return "网页打开" if cjk else "web fetch"
    if normalized == "read_file":
        return "读文件" if cjk else "read file"
    if normalized == "read_artifact":
        return "读产物" if cjk else "read artifact"
    if normalized == "write_file":
        return "写文件" if cjk else "write file"
    if normalized == "edit_file":
        return "编辑文件" if cjk else "edit file"
    if normalized == "run_command":
        return "运行命令" if cjk else "run command"
    if normalized == "reply":
        return "回复" if cjk else "reply"
    return name.replace("_", " ")


def sanitize_internal_tool_names_for_user_text(text: str, *, cjk: bool = False) -> str:
    cleaned = str(text or "")
    if not cleaned:
        return ""

    if cjk:
        cleaned = re.sub(
            r"这次\s*`?web_search`?\s*(?:已?被限制继续使用|已?被限制|受限|预算(?:已)?用尽)",
            "这次后续检索受限",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"`?web_search`?\s*(?:已?被限制继续使用|已?被限制|受限|预算(?:已)?用尽)",
            "后续检索受限",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"`?web_fetch`?\s*(?:抓取|打开|访问)?(?:失败|被拦截|受限)",
            "页面未能打开",
            cleaned,
            flags=re.IGNORECASE,
        )

    replacements = {
        "web_search": "搜索" if cjk else "web search",
        "search_web": "搜索" if cjk else "web search",
        "web_fetch": "网页打开" if cjk else "web fetch",
        "read_file": "读文件" if cjk else "read file",
        "read_artifact": "读产物" if cjk else "read artifact",
        "write_file": "写文件" if cjk else "write file",
        "edit_file": "编辑文件" if cjk else "edit file",
        "run_command": "运行命令" if cjk else "run command",
        "reply": "回复" if cjk else "reply",
    }
    for raw, label in replacements.items():
        cleaned = re.sub(rf"`{re.escape(raw)}`", label, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"\b{re.escape(raw)}\b", label, cleaned, flags=re.IGNORECASE)

    if cjk:
        cleaned = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", cleaned)
    return cleaned


def _is_web_search_tool_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in {"web_search", "search_web"}:
        return True
    if not lowered.startswith("mcp__"):
        return False
    return (
        ("websearch" in lowered or "web_search" in lowered or "__web__" in lowered)
        and lowered.endswith("__search")
    )


def _is_web_fetch_tool_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in {"web_fetch", "fetch_url", "fetch_web", "fetch_page"}:
        return True
    if not lowered.startswith("mcp__"):
        return False
    return (
        ("websearch" in lowered or "web_search" in lowered or "__web__" in lowered)
        and (lowered.endswith("__fetch_page") or lowered.endswith("__fetch") or lowered.endswith("__fetch_url"))
    )


class ProjectionRegistry:
    """Centralizes ordinary UI labels derived from tool metadata/results."""

    def __init__(self) -> None:
        self._tool_metadata: dict[str, dict[str, Any]] = {}

    def register_tool_metadata(self, tool_name: str, metadata: dict[str, Any] | None) -> None:
        """Register non-model-facing projection metadata from a BaseTool.

        Name inference below remains the compatibility fallback. This hook lets
        extracted per-tool modules gradually own their result category and UI
        labels without changing every execution call site at once.
        """
        name = str(tool_name or "").strip().lower()
        if not name:
            return
        clean = {
            str(key): value
            for key, value in (metadata or {}).items()
            if key in {"result_kind", "activity_kind", "display_scope", "panel_hint", "display_label"}
            and value
        }
        if clean:
            self._tool_metadata[name] = clean
        else:
            self._tool_metadata.pop(name, None)

    def _metadata_for_tool(self, tool_name: str) -> dict[str, Any]:
        return self._tool_metadata.get(str(tool_name or "").lower(), {})

    def result_kind_for_tool(self, tool_name: str) -> str:
        metadata_kind = self._metadata_for_tool(tool_name).get("result_kind")
        if metadata_kind:
            return str(metadata_kind)
        name = tool_name.lower()
        if name == "reply":
            return "reply"
        if name in {
            "task",
            "task_status",
            "task_stop",
            "workflow",
            "send_message",
            "message_list",
            "task_create",
            "task_list",
            "task_get",
            "task_update",
            "task_output",
            "team_create",
            "team_list",
            "team_delete",
        }:
            return "subagent"
        if name in {"load_skill", "unload_skill", "list_skills"}:
            return "skill"
        if name in {"todo_write", "todo_read"}:
            return "generic"
        if name.startswith("mcp__"):
            return "mcp"
        if _is_web_fetch_tool_name(name):
            return "web"
        if _is_web_search_tool_name(name):
            return "search"
        if name in {"web_fetch"} or "fetch" in name:
            return "web"
        if name in {"web_search", "search_web"}:
            return "search"
        if "command" in name or "terminal" in name or name in {"bash", "powershell"}:
            return "command"
        if name in {"write_file", "edit_file"} or any(part in name for part in ("write", "edit", "patch", "delete")):
            return "edit"
        if any(part in name for part in ("read", "file", "list", "grep", "glob")):
            return "file"
        return "generic"

    def activity_kind_for_tool(self, tool_name: str) -> str:
        metadata_kind = self._metadata_for_tool(tool_name).get("activity_kind")
        if metadata_kind:
            return str(metadata_kind)
        kind = self.result_kind_for_tool(tool_name)
        name = tool_name.lower()
        if name in {"ask_user", "reply"}:
            return ""
        if name in {"todo_write", "todo_read"}:
            return "genericTool"
        if kind in {"search", "web"}:
            return "webSearch"
        if kind == "command":
            return "commandExecution"
        if kind == "edit":
            return "fileChange"
        if kind == "mcp":
            return "mcpToolCall"
        if kind == "file":
            if name in {"read_file", "read_artifact"}:
                return "fileRead"
            return "workspaceSearch"
        return "genericTool"

    def input_summary_for_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        name = tool_name.lower()
        if name == "task":
            parallel = _parallel_task_descriptions(args)
            if parallel:
                first = parallel[0]
                suffix = f": {first}" if first else ""
                return _short_text(f"{len(parallel)} agents{suffix}")
            return _short_text(str(args.get("description") or args.get("objective") or args.get("prompt") or ""))
        if name == "workflow":
            workflow_name = str(args.get("name") or args.get("workflow_id") or "").strip()
            mode = str(args.get("mode") or "").strip()
            step_count = _workflow_step_count(args)
            if workflow_name and step_count:
                mode_part = f"{mode}, " if mode else ""
                return _short_text(f"{workflow_name} ({mode_part}{step_count} steps)")
            if workflow_name:
                return _short_text(workflow_name)
        if name in {"task_create", "task_update", "task_output", "task_get"}:
            return _short_text(str(args.get("title") or args.get("task_id") or ""))
        if name in {"team_create", "team_delete", "team_list"}:
            return _short_text(str(args.get("team_name") or ""))
        if name == "send_message":
            return _short_text(str(args.get("recipient") or args.get("message") or ""))
        if name == "message_list":
            return _short_text(str(args.get("participant_id") or args.get("team_name") or ""))
        if name in {"task_status", "task_stop"}:
            return _short_text(str(args.get("subagent_id") or ""))
        if name == "reply":
            return _short_text(str(args.get("message") or ""))
        if name in {"web_search", "search_web"}:
            return _short_text(str(args.get("query") or args.get("q") or ""))
        if name in {"web_fetch"} or "fetch" in name:
            url = str(args.get("url") or "")
            return _hostname(url) or _short_text(url)
        if "command" in name or "terminal" in name or name in {"bash", "powershell"}:
            return _short_text(str(args.get("command") or args.get("cmd") or ""))
        if name == "read_artifact":
            # Artifact IDs are protocol/debug references, not user-facing targets.
            return ""
        path_value = str(args.get("file_path") or args.get("path") or args.get("target") or args.get("directory") or "")
        if path_value:
            summary = _short_path(path_value)
            if name == "read_file":
                range_suffix = _read_file_line_range_suffix(args)
                if range_suffix:
                    summary += range_suffix
            return summary
        query = str(args.get("query") or args.get("pattern") or "")
        if query:
            return _short_text(query)
        return ""

    def display_hint_for_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        metadata = self._metadata_for_tool(tool_name)
        metadata_label = metadata.get("display_label")
        target = self.input_summary_for_tool(tool_name, args or {})
        if metadata_label:
            return _with_target(str(metadata_label), target)
        name = tool_name.lower()
        if name == "task":
            parallel = _parallel_task_descriptions(args or {})
            if parallel:
                label = f"Start {len(parallel)} subagents"
                return _with_target(label, parallel[0] if len(parallel) == 1 else f"{parallel[0]} +{len(parallel) - 1}")
            return _with_target("Start subagent", target)
        if name == "workflow":
            return _with_target("Start workflow", target) if (args or {}).get("steps") else _with_target("Resume workflow", target)
        if name == "task_create":
            return _with_target("Create workflow task", target)
        if name == "task_update":
            return _with_target("Update workflow task", target)
        if name == "task_output":
            return _with_target("Attach task result", target)
        if name == "task_get":
            return _with_target("Read workflow task", target)
        if name == "task_list":
            return "List workflow tasks"
        if name == "task_status":
            return _with_target("Check subagent", target)
        if name == "task_stop":
            return _with_target("Stop subagent", target)
        if name == "send_message":
            return _with_target("Message agent", target)
        if name == "message_list":
            return _with_target("Read agent messages", target)
        if name == "team_create":
            return _with_target("Create agent team", target)
        if name == "team_list":
            return _with_target("List agent teams", target)
        if name == "team_delete":
            return _with_target("Delete agent team", target)
        if name == "reply":
            return _with_target("Reply", target)
        if name == "todo_write":
            return "Update tasks"
        if name == "todo_read":
            return "Read tasks"
        if name == "read_artifact":
            return "Read full content"
        if name == "read_file":
            return _with_target("Read", target)
        if name == "list_files":
            return _with_target("List", target)
        if any(part in name for part in ("grep", "glob", "search")) and self.result_kind_for_tool(tool_name) == "file":
            return _with_target("Search", target)
        if name in {"web_search", "search_web"}:
            return _with_target("Search web", target)
        if name in {"web_fetch"} or "fetch" in name:
            return _with_target("Fetch", target)
        if "command" in name or "terminal" in name or name in {"bash", "powershell"}:
            return _with_target("Run", target)
        if name in {"write_file", "edit_file"} or any(part in name for part in ("write", "edit", "patch", "delete")):
            verb = "Write" if "write" in name else "Edit"
            return _with_target(verb, target)
        if name.startswith("mcp__"):
            server, tool = _mcp_server_and_tool(tool_name)
            label = f"{server}/{tool}" if server else tool_name
            return _with_target(label, target)
        return {
            "web": "Fetch",
            "search": "Search",
            "command": "Run",
            "file": "Read workspace",
            "edit": "Edit workspace",
            "mcp": "MCP tool",
            "skill": "Manage skill",
        }.get(self.result_kind_for_tool(tool_name), "Running tool")

    def project_tool_call(self, tool_name: str, args: dict[str, Any]) -> ToolProjection:
        metadata = self._metadata_for_tool(tool_name)
        result_kind = self.result_kind_for_tool(tool_name)
        if tool_name.lower() == "reply":
            return ToolProjection(
                result_kind=result_kind,
                display_hint=self.display_hint_for_tool(tool_name, args),
                input_summary=self.input_summary_for_tool(tool_name, args),
                activity_kind="",
                display_scope="silent",
                panel_hint="",
            )
        if tool_name.lower() in {"todo_write", "todo_read", "load_skill", "unload_skill", "list_skills"}:
            return ToolProjection(
                result_kind=result_kind,
                display_hint=self.display_hint_for_tool(tool_name, args),
                input_summary=self.input_summary_for_tool(tool_name, args),
                activity_kind=self.activity_kind_for_tool(tool_name),
                display_scope=str(metadata.get("display_scope") or "silent"),
                panel_hint=str(metadata.get("panel_hint") or ""),
            )
        return ToolProjection(
            result_kind=result_kind,
            display_hint=self.display_hint_for_tool(tool_name, args),
            input_summary=self.input_summary_for_tool(tool_name, args),
            activity_kind=self.activity_kind_for_tool(tool_name),
            display_scope=str(metadata.get("display_scope") or ("agents" if result_kind == "subagent" else "activity")),
            panel_hint=str(metadata.get("panel_hint") or ("subagents" if result_kind == "subagent" else "diff" if result_kind == "edit" else "inspector")),
        )

    def display_summary_for_result(
        self,
        tc: ToolCallEvent,
        result: ToolResult,
        *,
        status: str,
        diff: dict[str, Any] | None = None,
    ) -> str:
        if result.display_summary:
            return result.display_summary
        kind = result.result_kind or self.result_kind_for_tool(tc.name)
        name = tc.name.lower()
        args = tc.arguments or {}
        target = self.input_summary_for_tool(tc.name, args)
        if name == "reply":
            return "Sent reply"
        if name == "todo_write":
            return "Task update failed" if status == "failed" else "Updated tasks"
        if name == "todo_read":
            return "Task read failed" if status == "failed" else "Read tasks"
        if kind == "search":
            return _with_target("Search web", target) if target else "Search web"
        if kind == "web":
            url = str(args.get("url") or result.source_url or "")
            host = _hostname(url) or target
            verb = "Fetch failed" if status == "failed" else "Fetch"
            return _with_target(verb, host) if host else verb
        if kind == "command":
            prefix = "Command failed" if status == "failed" else "Ran command"
            return _with_target(prefix, target) if target else prefix
        if kind == "edit":
            path = target or _short_path(str(args.get("file_path") or args.get("path") or ""))
            summary = _with_target("Edited file", path) if path else "Edited file"
            if diff:
                plus = int(diff.get("plus") or diff.get("additions") or 0)
                minus = int(diff.get("minus") or diff.get("deletions") or 0)
                if plus or minus:
                    summary = f"{summary} (+{plus} -{minus})"
            return summary
        if kind == "file":
            if name == "list_files":
                return _with_target("List", target) if target else "List"
            if name == "read_artifact":
                return "Read full content failed" if status == "failed" else "Read full content"
            if name == "read_file":
                return _with_target("Read", target) if target else "Read"
            if any(part in name for part in ("grep", "glob", "search")):
                return _with_target("Search", target) if target else "Search"
            return _with_target("Workspace", target) if target else "Workspace"
        if kind == "mcp":
            server, tool = _mcp_server_and_tool(tc.name)
            label = f"{server}/{tool}" if server else tc.name
            if result.status == "timeout" and result.limitation == "non-critical timeout":
                return f"MCP tool timed out: {label}"
            return f"Ran MCP tool: {label}"
        if status == "blocked":
            return f"Blocked tool: {tc.name}"
        if status == "failed":
            return f"Tool failed: {tc.name}"
        return f"Ran tool: {tc.name}"


DEFAULT_PROJECTION_REGISTRY = ProjectionRegistry()


def result_kind_for_tool(tool_name: str) -> str:
    return DEFAULT_PROJECTION_REGISTRY.result_kind_for_tool(tool_name)


def input_summary_for_tool(tool_name: str, args: dict[str, Any]) -> str:
    return DEFAULT_PROJECTION_REGISTRY.input_summary_for_tool(tool_name, args)


def display_hint_for_tool(tool_name: str, args: dict[str, Any] | None = None) -> str:
    return DEFAULT_PROJECTION_REGISTRY.display_hint_for_tool(tool_name, args)


def activity_kind_for_tool(tool_name: str) -> str:
    return DEFAULT_PROJECTION_REGISTRY.activity_kind_for_tool(tool_name)


def display_summary_for_result(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    status: str,
    diff: dict[str, Any] | None = None,
) -> str:
    return DEFAULT_PROJECTION_REGISTRY.display_summary_for_result(tc, result, status=status, diff=diff)
