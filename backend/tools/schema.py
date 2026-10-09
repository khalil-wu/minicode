from __future__ import annotations

import re
import json
from copy import deepcopy
from typing import Any, Iterable


def code_mode_parameters(parameters: dict[str, Any] | bool) -> str:
    """Render the object actually accepted by tools.name(args), including unions.

    This directory is model input. Positional-looking signatures and an arbitrary
    four-field cutoff caused invalid search calls and omitted delegation prompts.
    ALL_TOOLS retains the complete JSON Schema, including numeric constraints.
    """
    if isinstance(parameters, bool):
        return "unknown" if parameters else "never"
    if "const" in parameters:
        return json.dumps(parameters["const"], ensure_ascii=False)
    if "enum" in parameters:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in parameters["enum"])
    alternatives = parameters.get("anyOf") or parameters.get("oneOf")
    if alternatives:
        base = {key: value for key, value in parameters.items() if key not in {"anyOf", "oneOf"}}
        return " | ".join(code_mode_parameters({**base, **branch} if isinstance(branch, dict) else branch) for branch in alternatives)
    if intersections := parameters.get("allOf"):
        types = [code_mode_parameters(branch) for branch in intersections]
        return " & ".join(f"({kind})" if " | " in kind else kind for kind in types)
    kind = parameters.get("type", "object" if "properties" in parameters else "unknown")
    if isinstance(kind, list):
        return " | ".join(code_mode_parameters({**parameters, "type": item}) for item in kind)
    if kind == "object" and "properties" in parameters:
        required = parameters.get("required", [])
        fields = [
            f"{name if re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', name) else json.dumps(name)}"
            f"{'' if name in required else '?'}: {code_mode_parameters(value)}"
            for name, value in parameters["properties"].items()
        ]
        if any(isinstance(value, dict) and value.get("description") for value in parameters["properties"].values()):
            lines = ["{"]
            for (name, value), field in zip(parameters["properties"].items(), fields):
                if isinstance(value, dict):
                    lines.extend(f"  // {line.strip()}" for line in str(value.get("description") or "").splitlines() if line.strip())
                lines.append(f"  {field};")
            lines.append("}")
            return "\n".join(lines)
        return "{ " + ", ".join(fields) + " }"
    if kind == "array":
        return f"Array<{code_mode_parameters(parameters.get('items', {}))}>"
    return "number" if kind == "integer" else kind


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
    stripped_description = _strip_unavailable_tool_references(description, visible)
    stripped_unavailable_reference = stripped_description != description
    description = stripped_description
    hint = _availability_hint(visible) if stripped_unavailable_reference else ""
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
        "ask_user",
        "task_status",
        "task_stop",
        "send_message",
        "sleep",
    }
    for name in sorted(known_tool_names - visible, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])")
        text = pattern.sub("an available tool", text)
    if "task" not in visible:
        text = re.sub(
            r"(?i)(\b(?:use|call|invoke|via)\s+)task\b",
            r"\1an available tool",
            text,
        )
        text = re.sub(r"`task`", "an available tool", text)
    return text


def _availability_hint(visible: set[str]) -> str:
    categories: list[str] = []
    workspace = [name for name in ("list_files", "grep_files", "glob_files", "read_file") if name in visible]
    if workspace:
        categories.append("workspace=" + ",".join(workspace))
    web = [name for name in ("web_search", "web_fetch") if name in visible]
    if web:
        categories.append("web=" + ",".join(web))
    deferred = [name for name in ("tool_search",) if name in visible]
    if deferred:
        categories.append("deferred=" + ",".join(deferred))
    if not categories:
        return ""
    return "Available direct tools: " + "; ".join(categories) + "."
