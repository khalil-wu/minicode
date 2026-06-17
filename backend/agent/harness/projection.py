from __future__ import annotations

from dataclasses import dataclass
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


class ProjectionRegistry:
    """Centralizes ordinary UI labels derived from tool metadata/results."""

    def result_kind_for_tool(self, tool_name: str) -> str:
        name = tool_name.lower()
        if name in {"load_skill", "unload_skill", "list_skills"}:
            return "skill"
        if name in {"todo_write", "todo_read"}:
            return "generic"
        if name.startswith("mcp__"):
            return "mcp"
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
        kind = self.result_kind_for_tool(tool_name)
        name = tool_name.lower()
        if name == "ask_user":
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
        if name in {"web_search", "search_web"}:
            return _short_text(str(args.get("query") or args.get("q") or ""))
        if name in {"web_fetch"} or "fetch" in name:
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

    def display_hint_for_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        name = tool_name.lower()
        target = self.input_summary_for_tool(tool_name, args or {})
        if name == "todo_write":
            return "Update tasks"
        if name == "todo_read":
            return "Read tasks"
        if name in {"read_file", "read_artifact"}:
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
        return ToolProjection(
            result_kind=self.result_kind_for_tool(tool_name),
            display_hint=self.display_hint_for_tool(tool_name, args),
            input_summary=self.input_summary_for_tool(tool_name, args),
            activity_kind=self.activity_kind_for_tool(tool_name),
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
            if name in {"read_file", "read_artifact"}:
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
