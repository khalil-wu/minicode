from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def postprocess_tool_schema(schema: dict[str, Any], *, visible_tool_names: Iterable[str]) -> dict[str, Any]:
    """Return a model-facing schema adjusted to the currently visible tools.

    Tool descriptions must not instruct the model to call tools that are not in
    this request's schema. Keep this conservative: remove hard references to
    unavailable tool names, then add a small availability hint to descriptions
    that mention tool routing.
    """

    visible = {str(name) for name in visible_tool_names if str(name).strip()}
    result = deepcopy(schema)
    function = result.get("function")
    if not isinstance(function, dict):
        return result
    description = str(function.get("description") or "")
    if not description:
        return result
    description = _strip_unavailable_tool_references(description, visible)
    hint = _availability_hint(visible)
    if hint and "Available direct tools:" not in description:
        description = f"{description.rstrip()} {hint}".strip()
    function["description"] = description
    return result


def _strip_unavailable_tool_references(description: str, visible: set[str]) -> str:
    text = description
    known_tool_names = {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "grep_files",
        "glob_files",
        "run_command",
        "web_search",
        "web_fetch",
        "read_artifact",
        "tool_search",
        "tool_describe",
        "tool_call",
        "task",
        "ask_user",
    }
    for name in sorted(known_tool_names - visible, key=len, reverse=True):
        text = text.replace(name, "an available tool")
    return text


def _availability_hint(visible: set[str]) -> str:
    categories: list[str] = []
    workspace = [name for name in ("list_files", "grep_files", "glob_files", "read_file") if name in visible]
    if workspace:
        categories.append("workspace=" + ",".join(workspace))
    web = [name for name in ("web_search", "web_fetch") if name in visible]
    if web:
        categories.append("web=" + ",".join(web))
    deferred = [name for name in ("tool_search", "tool_describe", "tool_call") if name in visible]
    if deferred:
        categories.append("deferred=" + ",".join(deferred))
    if not categories:
        return ""
    return "Available direct tools: " + "; ".join(categories) + "."
