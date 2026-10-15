"""Select the bundled, verbatim instructions from the local Codex source."""

import json
import logging
from pathlib import Path


_RESOURCES = Path(__file__).with_name("codex_prompt_resources")
_FALLBACK_PROMPT = (_RESOURCES / "prompt.md").read_bytes().decode("utf-8")
_MODEL_MESSAGES = json.loads((_RESOURCES / "model-instructions.json").read_text(encoding="utf-8"))
_MULTI_AGENT_MODES = json.loads((_RESOURCES / "multi-agent-mode.json").read_text(encoding="utf-8"))
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


def codex_multi_agent_mode(model_slug: str, proactive: bool) -> str:
    """Render the official effort-dependent developer mode fragment."""
    mode = _MULTI_AGENT_MODES["models"].get(model_slug.rsplit("/", 1)[-1])
    selected = "proactive" if proactive else "explicit"
    if mode is not None and mode.get("hint") is not None:
        text = mode["hint"]
    elif mode is not None and mode.get(selected) is not None:
        text = mode[selected]
    else:
        text = _MULTI_AGENT_MODES["bundled"][selected]
    return f"<multi_agent_mode>{text}</multi_agent_mode>" if text else ""


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
