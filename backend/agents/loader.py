"""Custom agent definitions loaded from ``agents/*.md`` (frontmatter + prompt body).

Mirrors Claude Code's ``agents/`` directory: a user defines a subagent by
dropping a markdown file whose YAML frontmatter holds ``name`` / ``description``
/ ``model`` / ``tools`` and whose body is the subagent's system prompt.
Discovered agents become selectable ``subagent_type`` values in the Task tool,
alongside the built-in types (general-purpose / explore / plan).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.agent.claude_md import _find_project_root, _get_managed_claude_dir
from backend.workspace.state import get_explicit_active_workspace_root

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        separator = "," if "," in value else None
        return [item.strip() for item in value.split(separator) if item.strip()]
    return []


def _extract_body(raw: str) -> str:
    return re.sub(r"^---\s*\n.*?\n---\s*\n?", "", raw, count=1, flags=re.DOTALL).strip()


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


def _project_agent_dirs(workspace_root: Path | None) -> list[Path]:
    """Return CC project agent directories, closest scope first."""
    if workspace_root is None:
        return []
    current = workspace_root.expanduser().resolve()
    boundary = _find_project_root(current)
    directories: list[Path] = []
    home = Path.home().resolve()
    while current != home:
        directory = current / ".claude" / "agents"
        if directory.is_dir():
            directories.append(directory)
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return directories


def _agent_search_dirs(workspace_root: Path | None = None) -> list[Path]:
    """Return Claude Code agent scopes in effective precedence order.

    Managed definitions override project definitions; the closest project scope
    then overrides parent project and user definitions. This is CC's active
    agent precedence. The install tree is deliberately absent: it is not
    a project scope and previously made agents depend on where MiniCode itself
    happened to be installed.
    """
    root = workspace_root or get_explicit_active_workspace_root()
    return [
        _get_managed_claude_dir() / ".claude" / "agents",
        *_project_agent_dirs(root),
        Path.home() / ".claude" / "agents",
    ]


def discover_agents(workspace_root: str | Path | None = None) -> dict[str, AgentDefinition]:
    """Scan agent directories and return {name: AgentDefinition}.

    Managed and project-local directories take precedence over user-global
    definitions (first wins), matching CC's active-agent priority.
    """
    agents: dict[str, AgentDefinition] = {}
    root = Path(workspace_root).resolve() if workspace_root is not None else None
    for directory in _agent_search_dirs(root):
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
        try:
            payload = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(payload, Mapping):
            return None
        fm = payload
        name = str(fm.get("name") or name).strip()
        description = str(fm.get("description") or "").strip()
        model = str(fm.get("model") or "").strip()
        tools = _string_list(fm.get("tools"))
        disallowed = _string_list(
            fm.get("disallowed_tools") or fm.get("disallowedTools") or []
        )
        effort = str(fm.get("effort") or "").strip()

    body = _extract_body(raw)
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


def get_custom_agent(
    name: str, workspace_root: str | Path | None = None
) -> AgentDefinition | None:
    """Look up a single custom agent by name (None if not defined)."""
    return discover_agents(workspace_root).get(name)


# ── Write API (Agent editor) ────────────────────────────────────────

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _writable_agents_dir(workspace_root: str | Path | None = None) -> Path:
    """Return the active project's standard Claude Code agent directory."""
    root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else get_explicit_active_workspace_root()
    )
    if root is None:
        raise ValueError("Open a workspace before creating or editing a project agent.")
    return root / ".claude" / "agents"


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
    workspace_root: str | Path | None = None,
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

    writable_dir = _writable_agents_dir(workspace_root)
    existing = get_custom_agent(clean_name, workspace_root)
    target_dir = writable_dir
    if existing and existing.source_path is not None:
        # Edit in place only when the file already lives in a writable dir.
        if writable_dir in existing.source_path.parents:
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


def delete_custom_agent(
    name: str, workspace_root: str | Path | None = None
) -> bool:
    """Delete a user-defined agent file. Returns False if not found or not deletable.

    Only deletes files inside the writable user directory; built-in/global
    definitions elsewhere are left untouched.
    """
    clean_name = (name or "").strip()
    if not _AGENT_NAME_RE.match(clean_name):
        return False
    writable_dir = _writable_agents_dir(workspace_root)
    agent = get_custom_agent(clean_name, workspace_root)
    if agent is None or agent.source_path is None:
        return False
    if writable_dir not in agent.source_path.parents:
        return False
    try:
        agent.source_path.unlink()
        return True
    except OSError:
        return False
