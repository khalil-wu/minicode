"""Tool registry construction and attachment store factory."""

from __future__ import annotations

import logging
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.commands.catalog import get_builtin_command_catalog
from backend.config import load_config
from backend.permissions.checker import PermissionChecker
from backend.tools.agent_tools import AskUserTool, BriefTool, ReadArtifactTool, TaskTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GrepFilesTool, GlobFilesTool
from backend.tools.terminal_tools import ReadTerminalTool

from . import _state

logger = logging.getLogger(__name__)


def _get_attachment_store() -> AttachmentStore:
    """Create an attachment store from the current configured base dir."""
    return AttachmentStore()


def _build_tool_registry(
    artifact_store: ArtifactStore,
    vector_memory: Any | None = None,
    *,
    llm_provider: Any | None = None,
    mcp_manager: Any | None = None,
) -> ToolRegistry:
    """
    Build the default tool registry (DESIGN.md section 8.2).

    Built-in tools:
      read_file / write_file / edit_file / list_files - file operations
      grep_files    - search
      run_command   - command execution
      ask_user      - proactive questions
      read_artifact - read artifact
      read_memory / save_memory - memory operations

    MCP tools are registered dynamically after connection.
    """
    registry = ToolRegistry()

    # File tools
    registry.register(ReadFileTool(artifact_store))
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ListFilesTool())

    # Search tools
    registry.register(GrepFilesTool())
    registry.register(GlobFilesTool())

    # Command tools
    registry.register(RunCommandTool(artifact_store))
    registry.register(ReadTerminalTool())

    # Agent helper tools
    registry.register(AskUserTool())
    registry.register(BriefTool())
    registry.register(
        ReadArtifactTool(
            artifact_store,
            attachment_store=_get_attachment_store(),
        )
    )
    registry.register(
        TaskTool(
            llm_provider=llm_provider,
            tool_registry_provider=lambda: registry,
            artifact_store=artifact_store,
            permission_checker_provider=lambda: PermissionChecker(load_config().permissions),
            agent_settings_provider=lambda: load_config().agent,
            token_budget_provider=lambda: load_config().token_budget,
        )
    )

    # ── Memory tools (DESIGN.md section 2.2) ──
    bootstrap = _state.bootstrap
    file_memory = bootstrap.file_memory if bootstrap else None
    if file_memory:
        from backend.tools.memory_tools import (
            ReadMemoryTool, SaveMemoryTool,
            RecallMemoryTool, RememberMemoryTool,
        )

        registry.register(ReadMemoryTool(file_memory))
        registry.register(SaveMemoryTool(file_memory))
        if vector_memory is not None:
            registry.register(RecallMemoryTool(vector_memory))
            registry.register(RememberMemoryTool(vector_memory))

    # ── Web tools (DESIGN.md section 8.2) ──
    from backend.tools.web_tools import WebFetchTool, WebSearchTool
    registry.register(WebFetchTool(artifact_store))
    registry.register(WebSearchTool(artifact_store))

    # ── AST lightweight code analysis tools (newplan.md section 4.1) ──
    from backend.tools.ast_tools import GoToDefinitionTool, FindReferencesTool
    registry.register(GoToDefinitionTool())
    registry.register(FindReferencesTool())

    # ── Git tools ──
    from backend.tools.git_tools import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitCommitTool())

    # ── Fuzzy search tools ──
    from backend.tools.fuzzy_search_tool import FuzzySearchTool
    from pathlib import Path
    workspace_root = Path.cwd()
    registry.register(FuzzySearchTool(workspace_root))

    # ── Worktree tools ──
    from backend.tools.worktree_tools import (
        ListWorktreesTool,
        CreateWorktreeTool,
        RemoveWorktreeTool,
        SnapshotWorktreeTool,
        RestoreWorktreeTool,
        ListWorktreeSnapshotsTool,
    )
    registry.register(ListWorktreesTool())
    registry.register(CreateWorktreeTool())
    registry.register(RemoveWorktreeTool())
    registry.register(SnapshotWorktreeTool())
    registry.register(RestoreWorktreeTool())
    registry.register(ListWorktreeSnapshotsTool())

    # ── Preview tools ──
    from backend.tools.preview_tool import PreviewServerTool
    registry.register(PreviewServerTool(workspace_root=str(workspace_root)))

    # ── Task management tools ──
    from backend.tools.todo_tool import TodoReadTool, TodoWriteTool
    todo_tool = TodoWriteTool()
    registry.register(todo_tool)
    registry.register(TodoReadTool(todo_write_tool=todo_tool))

    # ── Plan state machine (user-visible execution plan) ──
    from backend.tools.plan_tool import UpdatePlanTool
    registry.register(UpdatePlanTool(workspace_root=workspace_root))

    # ── MCP resource bridge ──
    # MCP tools are registered dynamically below as per-server proxies. Resources
    # are a separate MCP capability, so expose a stable read-only bridge that lets
    # the agent discover and fetch server-provided context without knowing the
    # connected server topology ahead of time.
    effective_mcp_manager = mcp_manager if mcp_manager is not None else (bootstrap.mcp_manager if bootstrap else None)
    from backend.tools.mcp_tools import ListMcpResourcesTool, ReadMcpResourceTool
    registry.register(ListMcpResourcesTool(effective_mcp_manager))
    registry.register(ReadMcpResourceTool(effective_mcp_manager, artifact_store))

    # ── Tool search (lazy discovery) ──
    from backend.tools.tool_search import ToolCallTool, ToolDescribeTool, ToolSearchTool
    tool_search = ToolSearchTool(registry)
    registry.register(tool_search)
    registry.register(ToolDescribeTool(registry))
    registry.register(ToolCallTool())

    # ── Skill tools (DESIGN.md section 5/8.2) ──
    skill_manager = bootstrap.skill_manager if bootstrap else None
    from backend.tools.skill_tools import LoadSkillTool, UnloadSkillTool, ListSkillsTool
    registry.register(LoadSkillTool(skill_manager))
    registry.register(UnloadSkillTool(skill_manager))
    registry.register(ListSkillsTool(skill_manager))
    for command_definition in get_builtin_command_catalog():
        registry.register_command(command_definition["name"], command_definition)
    if skill_manager:
        try:
            for skill in skill_manager.list_all():
                if isinstance(skill, dict):
                    skill_name = str(skill.get("name", "")).strip()
                    if skill_name:
                        registry.register_skill(skill_name, skill)
        except Exception as exc:
            logger.debug("Skill metadata registration failed: %s", exc)

    # ── MCP dynamic tools (Phase 3.1) ──
    # Register tools from connected MCP servers as proxies so the agent can
    # discover and call them, not just the status API. Disconnected/error
    # servers contribute nothing. Proxies carry artifact_store for large
    # MCP results.
    if mcp_manager is not None:
        try:
            _register_mcp_tools(registry, mcp_manager, artifact_store)
        except Exception as exc:  # pragma: no cover - never block registry build
            logger.warning("MCP tool registration failed: %s", exc)

    return registry


def _register_mcp_tools(
    registry: ToolRegistry,
    mcp_manager: Any,
    artifact_store: ArtifactStore,
) -> None:
    """Register connected MCP servers' tools as MCPToolProxy instances."""
    from backend.mcp.registry import MCPToolRegistry

    get_all_tools = getattr(mcp_manager, "get_all_tools", None)
    get_client = getattr(mcp_manager, "get_client", None)
    if not callable(get_all_tools) or not callable(get_client):
        return

    mcp_registry = MCPToolRegistry(registry, artifact_store=artifact_store, mcp_manager=mcp_manager)
    for server_name, tools in (get_all_tools() or {}).items():
        client = get_client(server_name)
        if client is None or not tools:
            continue
        mcp_registry.register_server_tools(server_name, tools, client)
