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
    model: str = ""  # "" = inherit the session model
    tools: list[str] = field(default_factory=list)  # empty = all tools
    disallowed_tools: list[str] = field(default_factory=list)
    source_path: Path | None = None


def _agent_search_dirs() -> list[Path]:
    """Project-local dirs first (higher priority), then user-global."""
    return [
        PROJECT_ROOT / ".mini-code" / "agents",
        PROJECT_ROOT / ".codex" / "agents",
        PROJECT_ROOT / "agents",
        Path.home() / ".mini-code" / "agents",
        Path.home() / ".codex" / "agents",
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
        source_path=path,
    )


def get_custom_agent(name: str) -> AgentDefinition | None:
    """Look up a single custom agent by name (None if not defined)."""
    return discover_agents().get(name)
