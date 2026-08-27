"""MiniCode custom agents loaded from ``.minicode/agents/*.md``.

A user defines a subagent with YAML frontmatter for identity and execution
policy, followed by the subagent system prompt as Markdown.
Discovered agents become selectable ``subagent_type`` values in the Task tool,
alongside the built-in types (general-purpose / explore / plan).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from backend.agent.instruction_discovery import _get_managed_minicode_dir
from backend.agent.markdown_scopes import (
    file_identity,
    get_minicode_config_home_dir,
    get_markdown_directories,
)
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.workspace.state import get_explicit_active_workspace_root

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
AgentSource = Literal[
    "user",
    "project",
    "policy",
    "unknown",
]
EditableAgentSource = Literal["user", "project"]
_SUPPORTED_AGENT_EFFORT_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
logger = logging.getLogger(__name__)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        separator = "," if "," in value else None
        return [item.strip() for item in value.split(separator) if item.strip()]
    return []


def _agent_effort(value: Any) -> str:
    """Return a MiniCode reasoning level or inherit when invalid."""

    if value is None or value == "" or not isinstance(value, str):
        return ""
    text = str(value).strip().lower()
    return text if text in _SUPPORTED_AGENT_EFFORT_LEVELS else ""


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
    # Source identity lets editors update the exact file without creating an
    # accidental override in another scope.
    source: AgentSource = "unknown"
    filename: str = ""
    base_dir: Path | None = None
    # Optional runtime policy declared by the agent definition.
    permission_mode: str = ""
    background: bool | None = None
    has_output_schema: bool = False


def _project_agent_dirs(workspace_root: Path | None) -> list[Path]:
    """Return MiniCode project agent directories, closest scope first."""
    if workspace_root is None:
        return []
    return [
        scope.path
        for scope in get_markdown_directories(
            "agents",
            workspace_root,
            managed_root=_get_managed_minicode_dir(),
            session_project_root=get_explicit_active_workspace_root(),
        )
        if scope.source == "project"
    ]


def _agent_search_dirs(workspace_root: Path | None = None) -> list[Path]:
    """Return MiniCode agent scopes in managed, user, project order."""
    root = workspace_root or get_explicit_active_workspace_root()
    scopes = get_markdown_directories(
        "agents",
        root,
        managed_root=_get_managed_minicode_dir(),
        session_project_root=get_explicit_active_workspace_root(),
    )
    if _agents_restricted_to_plugins(root):
        scopes = [scope for scope in scopes if scope.source == "policy"]
    return [scope.path for scope in scopes]


def _agents_restricted_to_plugins(workspace_root: Path | None) -> bool:
    try:
        from backend.config import load_config_layer_stack

        requirements = load_config_layer_stack(cwd=workspace_root).requirements
        return requirements.restricts_customization_to_plugins("agents")
    except Exception:
        # Managed policy failures cannot safely widen executable Agent sources.
        logger.warning(
            "Unable to determine managed plugin-only Agent policy; "
            "restricting filesystem Agents to managed policy",
            exc_info=True,
        )
        return True


def _source_for_agent_dir(directory: Path, workspace_root: Path | None) -> AgentSource:
    resolved = directory.expanduser().resolve()
    for scope in get_markdown_directories(
        "agents",
        workspace_root,
        managed_root=_get_managed_minicode_dir(),
        session_project_root=get_explicit_active_workspace_root(),
    ):
        if resolved == scope.path.resolve():
            return scope.source
    return "unknown"


def discover_agent_definitions(
    workspace_root: str | Path | None = None,
) -> list[AgentDefinition]:
    """Return every discovered file with its MiniCode setting-source identity.

    ``discover_agents`` below still projects the effective name map used by the
    Task runtime. The editor needs the complete list so shadowed user/project
    files remain addressable by the editor.
    """

    root = (
        Path(workspace_root).expanduser().absolute()
        if workspace_root is not None
        else get_explicit_active_workspace_root()
    )
    definitions: list[AgentDefinition] = []
    seen_files: set[tuple[int, int]] = set()
    for directory in _agent_search_dirs(root):
        if not directory.is_dir():
            continue
        source = _source_for_agent_dir(directory, root)
        for md_file in sorted(
            directory.rglob("*.md"), key=lambda item: str(item).casefold()
        ):
            identity = file_identity(md_file)
            if identity is not None and identity in seen_files:
                continue
            if identity is not None:
                seen_files.add(identity)
            agent = _parse_agent_file(
                md_file,
                source=source,
                base_dir=directory,
            )
            if agent and agent.name:
                definitions.append(agent)
    return definitions


def discover_agents(workspace_root: str | Path | None = None) -> dict[str, AgentDefinition]:
    """Scan agent directories and return {name: AgentDefinition}.

    User definitions are overridden by the closest project definition and then
    by managed policy. Unknown sources remain first-wins for embedders.
    """
    definitions = discover_agent_definitions(workspace_root)
    agents: dict[str, AgentDefinition] = {}
    for agent in definitions:
        if agent.source == "unknown":
            agents.setdefault(agent.name, agent)
    for agent in definitions:
        if agent.source == "user":
            agents[agent.name] = agent
    for agent in reversed(definitions):
        if agent.source == "project":
            agents[agent.name] = agent
    for agent in definitions:
        if agent.source == "policy":
            agents[agent.name] = agent
    return agents


def _parse_agent_file(
    path: Path,
    *,
    source: AgentSource = "unknown",
    base_dir: Path | None = None,
) -> AgentDefinition | None:
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
            fm.get("disallowed_tools") or []
        )
        effort = _agent_effort(fm.get("effort"))
        permission_mode = str(fm.get("permission_mode") or "").strip()
        raw_background = fm.get("background")
        background = raw_background if isinstance(raw_background, bool) else None
        has_output_schema = fm.get("has_output_schema") is True
    else:
        # A Markdown file without structured identity is not an agent.
        return None

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
        source=source,
        filename=path.stem,
        base_dir=base_dir or path.parent,
        permission_mode=permission_mode,
        background=background,
        has_output_schema=has_output_schema,
    )


def get_custom_agent(
    name: str, workspace_root: str | Path | None = None
) -> AgentDefinition | None:
    """Look up a single custom agent by name (None if not defined)."""
    return discover_agents(workspace_root).get(name)


# ── Write API (Agent editor) ────────────────────────────────────────

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _agents_dir_for_source(
    source: EditableAgentSource,
    workspace_root: str | Path | None = None,
) -> Path:
    if source == "user":
        return get_minicode_config_home_dir() / "agents"
    root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else get_explicit_active_workspace_root()
    )
    if root is None:
        raise ValueError("Open a workspace before creating a project agent.")
    return root / ".minicode" / "agents"


def _render_agent_markdown(agent: AgentDefinition) -> str:
    frontmatter: dict[str, Any] = {
        "name": agent.name,
        "description": agent.description,
    }
    if agent.tools and agent.tools != ["*"]:
        frontmatter["tools"] = agent.tools
    if agent.disallowed_tools:
        frontmatter["disallowed_tools"] = agent.disallowed_tools
    if agent.model:
        frontmatter["model"] = agent.model
    if agent.effort:
        frontmatter["effort"] = agent.effort
    metadata = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    return f"---\n{metadata}\n---\n\n{agent.prompt.strip()}\n"


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
    source: EditableAgentSource = "project",
    source_path: str | Path | None = None,
) -> AgentDefinition:
    """Create a user/project agent or update the exact discovered source file."""
    clean_name = (name or "").strip()
    if not _AGENT_NAME_RE.match(clean_name):
        raise ValueError(
            "Agent name must be 1-64 chars: letters, digits, dash or underscore."
        )

    if source not in {"user", "project"}:
        raise ValueError("Only user and project Agent files are editable.")

    target_path: Path
    target_source: EditableAgentSource = source
    if source_path is not None and str(source_path).strip():
        requested_path = Path(source_path).expanduser().resolve()
        existing = next(
            (
                candidate
                for candidate in discover_agent_definitions(workspace_root)
                if candidate.source_path is not None
                and candidate.source_path.resolve() == requested_path
                and candidate.name == clean_name
            ),
            None,
        )
        if existing is None:
            raise ValueError("Agent source file is no longer available.")
        if existing.source not in {"user", "project"}:
            raise ValueError("This Agent source is read-only.")
        if existing.source != source:
            raise ValueError("Agent source does not match its discovered file.")
        target_path = requested_path
        target_source = existing.source
    else:
        target_dir = _agents_dir_for_source(source, workspace_root)
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
        source=target_source,
        filename=target_path.stem,
        base_dir=target_path.parent,
    )
    rendered = _render_agent_markdown(agent)
    if source_path is None or not str(source_path).strip():
        # Exclusive creation prevents concurrent creators from being silently
        # overwritten between an exists check and the atomic write.
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with file_mutation_locks([target_path]):
            try:
                atomic_write_text(target_path, rendered, overwrite=False)
            except FileExistsError as exc:
                raise ValueError(f"Agent file already exists: {target_path}") from exc
    else:
        with file_mutation_locks([target_path]):
            atomic_write_text(target_path, rendered)
    return agent


def delete_custom_agent(
    name: str,
    workspace_root: str | Path | None = None,
    *,
    source: EditableAgentSource | None = None,
    source_path: str | Path | None = None,
) -> bool:
    """Delete the exact user/project source file selected by the editor."""
    clean_name = (name or "").strip()
    if not _AGENT_NAME_RE.match(clean_name):
        return False
    definitions = discover_agent_definitions(workspace_root)
    requested_path = (
        Path(source_path).expanduser().resolve()
        if source_path is not None and str(source_path).strip()
        else None
    )
    agent = next(
        (
            candidate
            for candidate in definitions
            if candidate.name == clean_name
            and (
                requested_path is None
                or (
                    candidate.source_path is not None
                    and candidate.source_path.resolve() == requested_path
                )
            )
            and (source is None or candidate.source == source)
        ),
        None,
    )
    if agent is None or agent.source_path is None:
        return False
    if agent.source not in {"user", "project"}:
        return False
    try:
        with file_mutation_locks([agent.source_path]):
            agent.source_path.unlink()
            return True
    except OSError:
        return False
