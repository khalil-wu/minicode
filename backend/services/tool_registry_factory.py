"""Tool registry construction and attachment store factory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.commands.catalog import get_builtin_command_catalog
from backend.config import AppConfig, load_config
from backend.mcp import MCP_MANAGER_UNSET
from backend.permissions.checker import PermissionChecker
from backend.tools.agent_tools import RequestUserInputTool, BriefTool, ReadArtifactTool, TaskStatusTool, TaskStopTool, TaskTool
from backend.tools.agent_artifact_tools import PresentFileTool
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.browser_control_tool import BrowserControlTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.context_window_tools import GetContextRemainingTool, NewContextTool
from backend.agent.context_window import ContextWindowConfig
from backend.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from backend.tools.monitor_tool import MonitorTool
from backend.tools.request_permissions import RequestPermissionsTool
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.registry import ToolRegistry
from backend.tools.search_tools import GrepFilesTool, GlobFilesTool
from backend.tools.sleep_tool import CurrentTimeTool, SleepTool
from backend.tools.swarm_tools import (
    FollowupTaskTool,
    MessageListTool,
    SendMessageTool,
    SourceSendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskOutputTool,
    TaskUpdateTool,
    TeamCreateTool,
    TeamDeleteTool,
    TeamListTool,
)
from backend.tools.terminal_tools import ReadTerminalTool

logger = logging.getLogger(__name__)
_WORKSPACE_ROOT_UNSET = object()


def _current_bootstrap() -> Any | None:
    from backend.api import _state

    return _state.bootstrap


def get_attachment_store() -> AttachmentStore:
    """Create an attachment store from the current configured base dir."""
    return AttachmentStore()


def build_tool_registry(
    artifact_store: ArtifactStore,
    *,
    workspace_root: str | Path | None | object = _WORKSPACE_ROOT_UNSET,
    config: AppConfig | None = None,
    llm_provider: Any | None = None,
    mcp_manager: Any = MCP_MANAGER_UNSET,
    skill_manager: Any | None = None,
) -> ToolRegistry:
    """
    Build the default tool registry (DESIGN.md section 8.2).

    Built-in tools:
      read_file / write_file / edit_file / list_files - file operations
      grep_files    - search
      run_command   - command execution
      request_user_input - structured root-thread questions
      read_artifact - read artifact
      memory_list / memory_read / memory_search / memory_add_ad_hoc_note

    MCP tools are registered dynamically after connection.
    """
    if workspace_root is _WORKSPACE_ROOT_UNSET:
        from backend.workspace.state import get_active_workspace_root

        resolved_workspace_root = get_active_workspace_root()
    elif workspace_root is None:
        resolved_workspace_root = None
    else:
        resolved_workspace_root = Path(workspace_root).expanduser().resolve()
    config_snapshot = config or load_config(cwd=resolved_workspace_root)
    from backend.agent.token_budget_activation import prepare_token_budget_for_startup, resolve_context_window_config
    config_snapshot = prepare_token_budget_for_startup(config_snapshot)
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
    registry.register(ToolExecTool())
    registry.register(ToolWaitTool())
    registry.register(SleepTool())
    registry.register(MonitorTool())
    registry.register(ReadTerminalTool())

    registry.register(RequestUserInputTool(default_mode_enabled=config_snapshot.feature_flags.enabled("default_mode_request_user_input")))
    if config_snapshot.feature_flags.enabled("request_permissions_tool"):
        registry.register(RequestPermissionsTool())
    if config_snapshot.feature_flags.enabled("token_budget"):
        registry.register(NewContextTool(resolve_context_window_config(config_snapshot)))
        registry.register(GetContextRemainingTool())
    registry.register(BriefTool())
    from backend.tools.goal_tools import GoalTool
    for name in ("create_goal", "get_goal", "update_goal"):
        registry.register(GoalTool(name, config_snapshot))
    from backend.tools.agent_user_tools import RequestUserInputAsyncTool, SendMessageToUserAsyncTool
    from backend.llm.model_request_policy import source_model_request_capabilities
    supported = source_model_request_capabilities(config_snapshot.llm.model).get("experimental_supported_tools", [])
    if "clock" in supported:
        registry.register(CurrentTimeTool())
    if "send_user_message_async" in supported or "request_user_input_async" in supported:
        registry.register(RequestUserInputAsyncTool())
        registry.register(SendMessageToUserAsyncTool())
    registry.register(
        ReadArtifactTool(
            artifact_store,
            attachment_store=get_attachment_store(),
        )
    )
    registry.register(PresentFileTool())
    from backend.tools.realtime_tools import EndRealtimeVoiceCallTool, CaptureScreenContextTool, TransferVoiceCallTool
    registry.register(EndRealtimeVoiceCallTool())
    registry.register(CaptureScreenContextTool())
    registry.register(TransferVoiceCallTool())
    from backend.tools.thread_resource_tools import ThreadResourceTool
    for name in ("list_artifacts", "attach_artifact", "remove_artifact"):
        registry.register(ThreadResourceTool(name))
    task_tool = TaskTool(
        config_snapshot=config_snapshot,
        llm_provider=llm_provider,
        tool_registry_provider=lambda: registry,
        artifact_store=artifact_store,
        permission_checker_provider=PermissionChecker(
            config_snapshot.permissions,
            resolved_workspace_root,
        ),
        agent_settings_provider=config_snapshot.agent,
        token_budget_provider=config_snapshot.token_budget,
        workspace_root=resolved_workspace_root,
    )
    registry.register(task_tool)
    from backend.agents.source_roles import source_agent_options
    from backend.tools.collaboration_tools import SpawnAgentTool, ListAgentsTool, WaitAgentTool, InterruptAgentTool
    coordination = source_agent_options(config_snapshot)
    if coordination["enabled"] and coordination["version"] == "v2":
        for tool in (SpawnAgentTool(task_tool), ListAgentsTool(), WaitAgentTool(config_snapshot), InterruptAgentTool()):
            registry.register(tool)
        if coordination["message_board_enabled"]:
            from backend.tools.agent_message_board_tools import SUPPORTED_BOARD_BACKENDS, message_board_tools

            if coordination["message_board_backend"] in SUPPORTED_BOARD_BACKENDS:
                for tool in message_board_tools():
                    registry.register(tool)
    if coordination["enabled"]:
        registry.register(TaskStopTool())
        registry.register(TaskStatusTool())
        send_message_tool = SourceSendMessageTool() if coordination["version"] == "v2" else SendMessageTool()
        registry.register(send_message_tool)
        registry.register(FollowupTaskTool())
        registry.register(MessageListTool())
        registry.register(TaskCreateTool())
        registry.register(TaskListTool())
        registry.register(TaskGetTool())
        registry.register(TaskUpdateTool())
        registry.register(TaskOutputTool())
        registry.register(TeamCreateTool())
        registry.register(TeamListTool())
        registry.register(TeamDeleteTool())
    bootstrap = _current_bootstrap()
    file_memory = bootstrap.file_memory if bootstrap else None
    if file_memory:
        from backend.tools.memory_tools import (
            MemoryAddAdHocNoteTool,
            MemoryListTool,
            MemoryReadTool,
            MemorySearchTool,
        )

        registry.register(MemoryAddAdHocNoteTool(file_memory))
        registry.register(MemoryListTool(file_memory))
        registry.register(MemoryReadTool(file_memory))
        registry.register(MemorySearchTool(file_memory))

    from backend.tools.web_tools import WebFetchTool, WebSearchTool
    registry.register(WebFetchTool(artifact_store))
    from backend.tools.image_generation_tool import GenerateImageTool
    from backend.tools.hatch_pet_tool import HatchPetTool
    registry.register(HatchPetTool())
    registry.register(GenerateImageTool(
        settings_snapshot=(config_snapshot.config_layer_stack.effective_config()
                           if config_snapshot.config_layer_stack is not None else None),
        provider=config_snapshot.llm.provider,
    ))
    registry.register(WebSearchTool(llm_provider))
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
    registry.register(GitStatusTool(resolved_workspace_root))
    registry.register(GitDiffTool(resolved_workspace_root))
    registry.register(GitLogTool(resolved_workspace_root))
    registry.register(GitCommitTool(resolved_workspace_root))

    from backend.tools.fuzzy_search_tool import FuzzySearchTool
    registry.register(FuzzySearchTool(resolved_workspace_root))

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
    registry.register(
        PreviewServerTool(
            workspace_root=(
                str(resolved_workspace_root)
                if resolved_workspace_root is not None
                else None
            )
        )
    )

    from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool, UpdatePlanTool
    from backend.tools.schedule_cron_tool import (
        ScheduleCronTool,
        ScheduleCronListTool,
        ScheduleCronDeleteTool,
    )
    registry.register(UpdatePlanTool())
    registry.register(ExitPlanModeTool(workspace_root=resolved_workspace_root))
    registry.register(EnterPlanModeTool(workspace_root=resolved_workspace_root))
    registry.register(ScheduleCronTool())
    registry.register(ScheduleCronListTool())
    registry.register(ScheduleCronDeleteTool())

    effective_mcp_manager = (
        bootstrap.get_mcp_manager_for_workspace(resolved_workspace_root)
        if mcp_manager is MCP_MANAGER_UNSET and bootstrap is not None
        else None if mcp_manager is MCP_MANAGER_UNSET else mcp_manager
    )
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
    if skill_manager is not None:
        from backend.tools.skill_tools import bind_skill_tools

        bind_skill_tools(registry, skill_manager)

    from backend.tools.tool_search import ToolSearchTool
    tool_search = ToolSearchTool(registry)
    registry.register(tool_search)

    for command_definition in get_builtin_command_catalog():
        registry.register_command(command_definition["name"], command_definition)

    if effective_mcp_manager is not None:
        register_mcp_tools(registry, effective_mcp_manager, artifact_store)

    return registry


def register_mcp_tools(
    registry: ToolRegistry,
    mcp_manager: Any,
    artifact_store: ArtifactStore,
    *,
    prepare_call=None,
) -> None:
    """Register connected MCP servers' tools as MCPToolProxy instances."""
    from backend.mcp.registry import MCPToolRegistry

    get_all_tools = getattr(mcp_manager, "get_all_tools", None)
    get_client = getattr(mcp_manager, "get_client", None)
    if not callable(get_all_tools) or not callable(get_client):
        return

    mcp_registry = MCPToolRegistry(registry, artifact_store=artifact_store, mcp_manager=mcp_manager,
        prepare_call=prepare_call)
    for server_name, tools in (get_all_tools() or {}).items():
        client = get_client(server_name)
        if not tools:
            continue
        mcp_registry.register_server_tools(server_name, tools, client)
