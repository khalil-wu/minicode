"""Custom agent definitions loaded from ``agents/*.md`` (frontmatter + prompt body).

Mirrors Claude Code's ``agents/`` directory: a user defines a subagent by
dropping a markdown file whose YAML frontmatter holds ``name`` / ``description``
/ ``model`` / ``tools`` and whose body is the subagent's system prompt.
Discovered agents become selectable ``subagent_type`` values in the Task tool,
alongside the built-in types (general-purpose / explore / plan / implement /
verification). Frontmatter parsing reuses the skill loader's dependency-free
YAML parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT
from backend.skills.loader import SkillLoader

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


@dataclass
class AgentDefinition:
    """A user-defined subagent."""

    name: str
    description: str
    prompt: str  # markdown body — the subagent's system prompt
    model: str = ""  # "" or "inherit" = inherit the session model
    tools: list[str] = field(default_factory=list)  # empty = all tools
    disallowed_tools: list[str] = field(default_factory=list)
    effort: str = ""  # "" = inherit; else reasoning effort (low/medium/high/…)
    source_path: Path | None = None


def _agent_search_dirs() -> list[Path]:
    """Project-local dirs first (higher priority), then user-global.

    ``.claude/agents`` is included for compatibility with the Claude Code
    ecosystem (cc walks ``.claude/agents`` from cwd up to the home dir).
    """
    return [
        PROJECT_ROOT / ".mini-code" / "agents",
        PROJECT_ROOT / ".codex" / "agents",
        PROJECT_ROOT / ".claude" / "agents",
        PROJECT_ROOT / "agents",
        Path.home() / ".mini-code" / "agents",
        Path.home() / ".codex" / "agents",
        Path.home() / ".claude" / "agents",
    ]


def discover_agents() -> dict[str, AgentDefinition]:
    """Scan agent directories and return {name: AgentDefinition}.

    Project-local directories take precedence over user-global (first wins),
    matching cc's project-over-user priority.
    """
    agents: dict[str, AgentDefinition] = {}
    for directory in _agent_search_dirs():
        if not directory.is_dir():
            continue
        for md_file in sorted(directory.glob("*.md")):
            agent = _parse_agent_file(md_file)
            if agent and agent.name:
                agents.setdefault(agent.name, agent)
    return agents


def _parse_agent_file(path: Path) -> AgentDefinition | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    name = path.stem
    description = ""
    model = ""
    tools: list[str] = []
    disallowed: list[str] = []
    effort = ""

    fm_match = _FRONTMATTER_RE.match(raw)
    if fm_match:
        fm = SkillLoader._parse_simple_yaml(fm_match.group(1))
        name = str(fm.get("name") or name).strip()
        description = str(fm.get("description") or "").strip()
        model = str(fm.get("model") or "").strip()
        tools = SkillLoader._to_list(fm.get("tools"))
        disallowed = SkillLoader._to_list(
            fm.get("disallowed_tools") or fm.get("disallowedTools") or []
        )
        effort = str(fm.get("effort") or "").strip()

    body = SkillLoader._extract_body(raw)
    if not body and not description:
        return None  # empty file — skip

    return AgentDefinition(
        name=name,
        description=description,
        prompt=body,
        model=model,
        tools=tools,
        disallowed_tools=disallowed,
        effort=effort,
        source_path=path,
    )


def get_custom_agent(name: str) -> AgentDefinition | None:
    """Look up a single custom agent by name (None if not defined)."""
    return discover_agents().get(name)


# ── Write API (Agent editor) ────────────────────────────────────────

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _user_agents_dir() -> Path:
    """Writable directory for user-defined agents (project-local)."""
    return PROJECT_ROOT / ".mini-code" / "agents"


def _yaml_escape(value: str) -> str:
    """Quote a scalar for frontmatter when it contains YAML-significant chars."""
    if value and not re.search(r"[:#\n\"']", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_agent_markdown(agent: AgentDefinition) -> str:
    lines = ["---", f"name: {_yaml_escape(agent.name)}"]
    if agent.description:
        lines.append(f"description: {_yaml_escape(agent.description)}")
    if agent.model:
        lines.append(f"model: {_yaml_escape(agent.model)}")
    if agent.effort:
        lines.append(f"effort: {_yaml_escape(agent.effort)}")
    if agent.tools:
        lines.append("tools: [" + ", ".join(_yaml_escape(t) for t in agent.tools) + "]")
    if agent.disallowed_tools:
        lines.append(
            "disallowed_tools: [" + ", ".join(_yaml_escape(t) for t in agent.disallowed_tools) + "]"
        )
    lines.append("---")
    lines.append("")
    lines.append(agent.prompt.strip())
    lines.append("")
    return "\n".join(lines)


def save_custom_agent(
    name: str,
    *,
    description: str = "",
    prompt: str = "",
    model: str = "",
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    effort: str = "",
) -> AgentDefinition:
    """Create or overwrite a user-defined agent markdown file.

    Raises ValueError on an invalid name or an attempt to overwrite a
    built-in agent file living outside the writable user directory.
    """
    clean_name = (name or "").strip()
    if not _AGENT_NAME_RE.match(clean_name):
        raise ValueError(
            "Agent name must be 1-64 chars: letters, digits, dash or underscore."
        )

    existing = get_custom_agent(clean_name)
    target_dir = _user_agents_dir()
    if existing and existing.source_path is not None:
        # Edit in place only when the file already lives in a writable dir.
        if _user_agents_dir() in existing.source_path.parents:
            target_dir = existing.source_path.parent

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{clean_name}.md"

    agent = AgentDefinition(
        name=clean_name,
        description=(description or "").strip(),
        prompt=(prompt or "").strip(),
        model=(model or "").strip(),
        tools=[str(t).strip() for t in (tools or []) if str(t).strip()],
        disallowed_tools=[str(t).strip() for t in (disallowed_tools or []) if str(t).strip()],
        effort=(effort or "").strip(),
        source_path=target_path,
    )
    target_path.write_text(_render_agent_markdown(agent), encoding="utf-8")
    return agent


def delete_custom_agent(name: str) -> bool:
    """Delete a user-defined agent file. Returns False if not found or not deletable.

    Only deletes files inside the writable user directory; built-in/global
    definitions elsewhere are left untouched.
    """
    clean_name = (name or "").strip()
    if not _AGENT_NAME_RE.match(clean_name):
        return False
    agent = get_custom_agent(clean_name)
    if agent is None or agent.source_path is None:
        return False
    if _user_agents_dir() not in agent.source_path.parents:
        return False
    try:
        agent.source_path.unlink()
        return True
    except OSError:
        return False
