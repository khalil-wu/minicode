"""Select the bundled, verbatim instructions from the local Codex source."""

import json
import logging
from pathlib import Path


_RESOURCES = Path(__file__).with_name("codex_prompt_resources")
_FALLBACK_PROMPT = (_RESOURCES / "prompt.md").read_bytes().decode("utf-8")
_MODEL_MESSAGES = json.loads((_RESOURCES / "model-instructions.json").read_text(encoding="utf-8"))
_MULTI_AGENT_MODES = json.loads((_RESOURCES / "multi-agent-mode.json").read_text(encoding="utf-8"))
_MULTI_AGENT_ROLES = json.loads((_RESOURCES / "multi-agent-role.json").read_text(encoding="utf-8"))
_COLLABORATION_MODES = {
    mode: (_RESOURCES / f"collaboration-{mode}.md").read_bytes().decode("utf-8")
    for mode in ("plan", "default")
}
_SIDE_CHAT_BOUNDARY = (_RESOURCES / "side-chat-boundary.md").read_text(encoding="utf-8").rstrip("\n")
_NO_CAPTURED_MESSAGES = object()


def codex_side_chat_instructions() -> str:
    """Verbatim desktop $Os instructions for a real side conversation."""
    return _SIDE_CHAT_BOUNDARY


def codex_collaboration_mode(mode: str, model_messages=None) -> str:
    """Render the verbatim Codex collaboration-mode instructions."""
    modes = (model_messages or {}).get("collaboration_modes") or {}
    supplied = modes.get(mode)
    text = supplied if supplied is not None else _COLLABORATION_MODES[mode]
    return f"<collaboration_mode>\n{text}\n</collaboration_mode>"


def codex_multi_agent_mode(model_slug: str, proactive: bool, *, model_messages=None, options=None) -> str:
    """Render the official effort-dependent developer mode fragment."""
    if options is not None:
        mode = ((model_messages or {}).get("multi_agent") or {}).get("mode") or {}
        text = options["multi_agent_mode_hint_text"]
        if text is None:
            text = mode.get("hint_text")
        if text is None:
            selected = "proactive" if proactive else "explicit"
            text = mode.get(selected)
            if text is None:
                text = _MULTI_AGENT_MODES["bundled"][selected]
        return f"<multi_agent_mode>{text}</multi_agent_mode>" if text else ""
    mode = _MULTI_AGENT_MODES["models"].get(model_slug.rsplit("/", 1)[-1])
    selected = "proactive" if proactive else "explicit"
    if mode is not None and mode.get("hint") is not None:
        text = mode["hint"]
    elif mode is not None and mode.get(selected) is not None:
        text = mode[selected]
    else:
        text = _MULTI_AGENT_MODES["bundled"][selected]
    return f"<multi_agent_mode>{text}</multi_agent_mode>" if text else ""


def _without_update_plan_instructions(text: str) -> str:
    """Source's checklist omission, applied only to Codex-owned role text."""
    lines = text.splitlines(keepends=True)
    rendered, index = [], 0
    while index < len(lines):
        line = lines[index].rstrip()
        if line in {"## Planning", "## `update_plan`", "## Plan tool", "## Plan Mode vs update_plan tool"}:
            end = next((next_line for next_line in range(index + 1, len(lines))
                if lines[next_line].startswith(("# ", "## "))), len(lines))
            if line != "## Planning" or any(item.startswith(("You have access to an `update_plan` tool",
                    "When `update_plan` is available, follow this section")) for item in lines[index:end]):
                index = end
                continue
        if line == "Progress visibility:" and index + 1 < len(lines) and lines[index + 1].startswith("If update_plan is available"):
            index += 2
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue
        if line.startswith(("- Use the plan tool ", "- If you create a checklist or task list,")):
            index += 1
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                index += 1
            continue
        rendered.append(lines[index])
        index += 1
    return "".join(rendered)


def codex_multi_agent_role(options, model_messages, *, subagent: bool) -> str:
    selected = "subagent" if subagent else "root"
    configured = options["subagent_usage_hint_text" if subagent else "root_agent_usage_hint_text"]
    if configured is not None:
        return configured
    roles = ((model_messages or {}).get("multi_agent") or {}).get("role") or {}
    supplied = roles.get(selected)
    base = _MULTI_AGENT_ROLES[selected] if supplied is None else supplied
    if not base:
        return ""
    if options["omit_update_plan_instructions"]:
        base = _without_update_plan_instructions(base)
    wait = _MULTI_AGENT_ROLES["wait"] + "\n\n" if options["wait_agent_enabled"] else ""
    total = options["max_children"] + 1
    text = f"{base}\n{_MULTI_AGENT_ROLES['shared']}\n{wait}There are {total} available concurrency slots, meaning that up to {total} agents can be active at once, including you."
    if options["expose_spawn_agent_model_overrides"]:
        text += "\n\n" + _MULTI_AGENT_ROLES["model_overrides"]
    return f"<multi_agent_role>\n{text}\n</multi_agent_role>" if supplied is not None else text


def codex_model_instructions(model_slug: str = "", personality: str | None = None, *, model_messages=_NO_CAPTURED_MESSAGES) -> str:
    """Use exact catalog slugs, then Codex's official unknown-model prompt."""
    if model_messages is _NO_CAPTURED_MESSAGES:
        template = _MODEL_MESSAGES[model_slug]["instructions_template"] if model_slug in _MODEL_MESSAGES else _FALLBACK_PROMPT
    else:
        template = (model_messages or {}).get("instructions_template")
        if template is None:
            logging.getLogger(__name__).warning("Model %s has no instruction template; returning empty instructions.", model_slug)
            template = ""
    if personality != "none":
        return template
    # Codex disables personality by removing its H1 section, not by substituting
    # a synthesized personality into the literal catalog template.
    lines = template.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if line.rstrip("\r\n") == "# Personality":
            end = start + 1
            while end < len(lines) and not (
                lines[end].startswith(("# ", "#\t")) or lines[end].rstrip("\r\n") == "#"
            ):
                end += 1
            return "".join(lines[:start] + lines[end:])
    return template
