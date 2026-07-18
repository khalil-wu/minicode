"""Tool registry construction and attachment store factory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.commands.catalog import get_builtin_command_catalog
from backend.config import load_config
from backend.permissions.checker import PermissionChecker
from backend.tools.agent_tools import AskUserTool, BriefTool, ReadArtifactTool, TaskStatusTool, TaskStopTool, TaskTool
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.browser_control_tool import BrowserControlTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.environment_tools import DetectPythonEnvironmentTool
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.monitor_tool import MonitorTool
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.registry import ToolRegistry
from backend.tools.repl_tool import ReplTool
from backend.tools.search_tools import GrepFilesTool, GlobFilesTool
from backend.tools.sleep_tool import SleepTool
from backend.tools.swarm_tools import (
    MessageListTool,
    SendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskOutputTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamDeleteTool,
    TeamListTool,
)
from backend.tools.team_memory_sync_tool import TeamMemorySyncTool
from backend.tools.terminal_tools import ReadTerminalTool
from backend.tools.workflow_tool import WorkflowTool

logger = logging.getLogger(__name__)


def _current_bootstrap() -> Any | None:
    try:
        from backend.api import _state
    except Exception:
        return None
    return getattr(_state, "bootstrap", None)


def get_attachment_store() -> AttachmentStore:
    """Create an attachment store from the current configured base dir."""
    return AttachmentStore()


def build_tool_registry(
    artifact_store: ArtifactStore,
    vector_memory: Any | None = None,
    *,
    llm_provider: Any | None = None,
    mcp_manager: Any | None = None,
    enable_vector_memory_tools: bool = False,
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

    registry.register(ReadFileTool(artifact_store))
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ApplyPatchTool())
    registry.register(ListFilesTool())
    registry.register(NotebookEditTool())

    registry.register(GrepFilesTool())
    registry.register(GlobFilesTool())

    registry.register(RunCommandTool(artifact_store))
    registry.register(ReplTool())
    registry.register(SleepTool())
    registry.register(MonitorTool())
    registry.register(ReadTerminalTool())
    registry.register(DetectPythonEnvironmentTool())

    registry.register(AskUserTool())
    registry.register(BriefTool())
    registry.register(
        ReadArtifactTool(
            artifact_store,
            attachment_store=get_attachment_store(),
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
    registry.register(TaskStopTool())
    registry.register(TaskStatusTool())
    registry.register(SendMessageTool())
    registry.register(MessageListTool())
    registry.register(TaskCreateTool())
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskUpdateTool())
    registry.register(TaskOutputTool())
    registry.register(TeamCreateTool())
    registry.register(TeamListTool())
    registry.register(TeamDeleteTool())
    registry.register(TeamMemorySyncTool())
    registry.register(
        WorkflowTool(
            llm_provider=llm_provider,
            tool_registry_provider=lambda: registry,
            artifact_store=artifact_store,
            permission_checker_provider=lambda: PermissionChecker(load_config().permissions),
            agent_settings_provider=lambda: load_config().agent,
            token_budget_provider=lambda: load_config().token_budget,
        )
    )

    bootstrap = _current_bootstrap()
    file_memory = bootstrap.file_memory if bootstrap else None
    if file_memory:
        from backend.tools.memory_tools import ReadMemoryTool, SaveMemoryTool

        # File memory is the primary, CC-aligned memory path: MEMORY.md index
        # + on-demand read_memory, no vector similarity. The vector tools stay
        # opt-in (enable_vector_memory_tools) so recall/remember don't compete
        # as a parallel memory route by default — see the memory design doc.
        registry.register(ReadMemoryTool(file_memory))
        registry.register(SaveMemoryTool(file_memory))
        if vector_memory is not None and enable_vector_memory_tools:
            from backend.tools.memory_tools import RecallMemoryTool, RememberMemoryTool

            registry.register(RecallMemoryTool(vector_memory))
            registry.register(RememberMemoryTool(vector_memory))

    from backend.tools.web_tools import WebFetchTool, WebSearchTool
    registry.register(WebFetchTool(artifact_store))
    registry.register(WebSearchTool(artifact_store))
    registry.register(BrowserControlTool())

    from backend.tools.ast_tools import GoToDefinitionTool, FindReferencesTool
    registry.register(GoToDefinitionTool())
    registry.register(FindReferencesTool())

    from backend.tools.lsp_tools import (
        LSPGoToDefinitionTool,
        LSPFindReferencesTool,
        LSPHoverTool,
        LSPDocumentSymbolsTool,
    )
    registry.register(LSPGoToDefinitionTool())
    registry.register(LSPFindReferencesTool())
    registry.register(LSPHoverTool())
    registry.register(LSPDocumentSymbolsTool())

    from backend.tools.git_tools import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitCommitTool())

    from backend.tools.fuzzy_search_tool import FuzzySearchTool
    workspace_root = Path.cwd()
    registry.register(FuzzySearchTool(workspace_root))

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

    from backend.tools.preview_tool import PreviewServerTool
    registry.register(PreviewServerTool(workspace_root=str(workspace_root)))

    from backend.tools.todo_tool import TodoReadTool, TodoWriteTool
    todo_tool = TodoWriteTool()
    registry.register(todo_tool)
    registry.register(TodoReadTool(todo_write_tool=todo_tool))

    from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool, UpdatePlanTool
    from backend.tools.prompt_pack_tool import PromptPackLoadTool
    from backend.tools.schedule_cron_tool import ScheduleCronTool
    registry.register(UpdatePlanTool(workspace_root=workspace_root))
    registry.register(ExitPlanModeTool(workspace_root=workspace_root))
    registry.register(EnterPlanModeTool(workspace_root=workspace_root))
    registry.register(PromptPackLoadTool())
    registry.register(ScheduleCronTool())

    effective_mcp_manager = mcp_manager if mcp_manager is not None else (bootstrap.mcp_manager if bootstrap else None)
    from backend.tools.mcp_tools import (
        GetMcpPromptTool,
        ListMcpResourceNotificationsTool,
        ListMcpResourceTemplatesTool,
        ListMcpPromptsTool,
        ListMcpResourcesTool,
        ReadMcpResourceTool,
        SubscribeMcpResourceTool,
        UnsubscribeMcpResourceTool,
    )
    registry.register(ListMcpResourcesTool(effective_mcp_manager))
    registry.register(ReadMcpResourceTool(effective_mcp_manager, artifact_store))
    registry.register(ListMcpResourceTemplatesTool(effective_mcp_manager))
    registry.register(SubscribeMcpResourceTool(effective_mcp_manager))
    registry.register(UnsubscribeMcpResourceTool(effective_mcp_manager))
    registry.register(ListMcpResourceNotificationsTool(effective_mcp_manager))
    registry.register(ListMcpPromptsTool(effective_mcp_manager))
    registry.register(GetMcpPromptTool(effective_mcp_manager))

    from backend.tools.tool_search import ToolCallTool, ToolDescribeTool, ToolSearchTool
    tool_search = ToolSearchTool(registry)
    registry.register(tool_search)
    registry.register(ToolDescribeTool(registry))
    registry.register(ToolCallTool())

    skill_manager = bootstrap.skill_manager if bootstrap else None
    from backend.tools.skill_tools import SkillSearchTool, SkillTool, LoadSkillTool, UnloadSkillTool, ListSkillsTool
    registry.register(SkillSearchTool(skill_manager))
    registry.register(SkillTool(skill_manager))
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

    if mcp_manager is not None:
        try:
            register_mcp_tools(registry, mcp_manager, artifact_store)
        except Exception as exc:  # pragma: no cover - never block registry build
            logger.warning("MCP tool registration failed: %s", exc)

    return registry


def register_mcp_tools(
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
