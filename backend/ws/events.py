"""
Centralized WebSocket event protocol (Wave 1 of full-stack refactor).

Single source of truth for every event type that flows between backend and
frontend. Mirror this file at frontend/src.v2/protocol/events.ts.

Conventions:
- Server → Client: SERVER_EVENT_TYPES + TypedDicts named *Event
- Client → Server: CLIENT_COMMAND_TYPES + TypedDicts named *Command
- Event names use lowercase dot.notation (terminal.output, conversation.list)
- New events added in this refactor are grouped under "Wave 1+ new"
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# ──────────────────────────────────────────────────────────────────
# Server → Client event type union
# ──────────────────────────────────────────────────────────────────

ServerEventType = Literal[
    # Streaming text + tool execution
    "text_chunk",
    "final_answer_started",
    "final_answer_delta",
    "final_answer_retracted",
    "final_answer_committed",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "tool_result",
    "agent.loop.started",
    "agent.loop.completed",
    "agent.item",
    "agent.progress",
    "task.update",
    "approval_request",
    "approval.cancelled",
    "approval.file_diff",
    "ask_user",
    # Skills
    "skill_activated",
    "skill_deactivated",
    # Context lifecycle
    "context_usage",
    "context_compacted",
    "budget_update",
    "budget.warning",            # Wave 1+ new: pre-compaction warning
    # Commands / artifacts
    "command.result",
    "command_output_chunk",
    "artifact_content",
    "artifact.preview",          # Wave 1+ new: lightweight artifact summary
    # Loop terminal events
    "done",
    "error",
    "stream_resume",
    # MCP
    "mcp_status",
    "mcp.lifecycle",
    "mcp.progress",
    # Environment / Git status
    "env.list",
    "git.pr_status",
    # Scheduler
    "scheduler.list",
    # Connectors marketplace
    "connectors.marketplace.list",
    # Checkpoints
    "checkpoint.list",
    "checkpoint.rewound",
    # File watcher
    "file.changed",
    # Terminal
    "terminal.output",
    "terminal.exit",
    "terminal.created",
    "terminal.killed",
    "terminal.list",
    "terminal.snapshot",
    "terminal.resized",
    # Background commands
    "background.completed",
    # Guidelines / permissions
    "guidelines.updated",
    "permission.mode.updated",
    "permission.rules.updated",
    # Conversation runtime
    "conversation.hydration.updated",
    "conversation.compaction.updated",
    "conversation.summary.updated",
    "goal.updated",
    "conversation.list",
    "conversation.switched",
    # LLM settings
    "llm.model.updated",
    # Workspace
    "workspace.imported",
    "workspace.recent.list",
    # Session / control plane
    "session.restored",
    "session.synced",
    "client.command.ack",
    "pong",
    "control_request",
    # Wave 1+ new: Subagents + Inspector + Citations
    "plan_step_updated",
    "plan_updated",
    "subagent.start",
    "subagent.event",
    "subagent.progress",
    "subagent.done",
    "citation.add",
    "inspector.update",
    # UI catalogs / notices
    "skills.list",
    "skills.marketplace.list",
    "commands.list",
    "system_notice",
    # Preview
    "preview.servers.updated",
    "preview.server.detected",
    "preview.server.stopped",
    "preview.navigated",
    "preview.refreshed",
    "preview.launch.config",
    "preview.launch.started",
    "preview.launch.stopped",
    "preview.server.ready",
    "preview.server.output",
    "preview.server.crashed",
    "preview.server.unhealthy",
    "preview.verified",
    # Git diff
    "diff.git_working_tree",
    "diff.git_staged",
    "diff.git_stage_file",
    "diff.git_unstage_file",
    "diff.git_stage_all",
    "diff.git_unstage_all",
    "diff.git_revert_file",
]


# ──────────────────────────────────────────────────────────────────
# Client → Server command type union
# ──────────────────────────────────────────────────────────────────

ClientCommandType = Literal[
    # Chat
    "user_message",
    "approval",
    "answer",
    "interrupt",
    "ping",
    # Control plane
    "control_response",
    "control_cancel_request",
    # Skills
    "load_skill",
    "unload_skill",
    # Artifacts / approvals
    "read_artifact",
    "approval.file_diff",
    # Conversation lifecycle
    "conversation.create",
    "conversation.switch",
    "conversation.list",
    "conversation.clear",
    "conversation.delete",
    "conversation.archive",
    "conversation.unarchive",
    "conversation.rename",
    "conversation.memory_mode.set",
    "conversation.permission_mode.set",
    "conversation.goal.set",
    "conversation.worktree.cleanup",
    # Session inspection
    "session.tasks.inspect",
    "session.status.inspect",
    "session.usage.inspect",
    "session.permissions.inspect",
    # LLM
    "llm.model.set",
    # Permission rules
    "conversation.permission.rules.list",
    "conversation.permission.rules.add",
    "conversation.permission.rules.remove",
    # Checkpoints
    "checkpoint.list",
    "checkpoint.rewind",
    # Terminal
    "terminal.create",
    "terminal.input",
    "terminal.resize",
    "terminal.kill",
    "terminal.list",
    "terminal.snapshot.request",
    "terminal.mirror.created",
    "terminal.mirror.output",
    "terminal.mirror.exit",
    # Workspace
    "workspace.import",
    "workspace.switch",
    "workspace.recent",
    # Session restore / sync
    "session.restore",
    "session.sync",
    # Wave 1+ new
    "task.edit",                 # legacy todo-state edit command
    "task.stop",
    "subagent.cancel",           # user kills a subagent
    "inspector.focus",           # UI tells backend which target the user is viewing
    # Frontend UI commands
    "workspace.set",
    "llm.config.set",
    "terminal.exec",
    "skills.list",
    "skills.install",
    "skills.marketplace.list",
    "commands.list",
    # Preview
    "preview.detect",
    "preview.navigate",
    "preview.refresh",
    "preview.launch.config",
    "preview.launch.start",
    "preview.launch.stop",
    "preview.verify",
    # Git diff
    "diff.git_working_tree",
    "diff.git_staged",
    "diff.git_stage_file",
    "diff.git_unstage_file",
    "diff.git_stage_all",
    "diff.git_unstage_all",
    "diff.git_revert_file",
    # MCP / Environment / Git status
    "mcp.list",
    "mcp.add",
    "mcp.remove",
    "mcp.restart",
    "env.list",
    "env.set",
    "env.delete",
    "git.pr_status",
    "approval.respond",
    # Scheduler
    "scheduler.list",
    "scheduler.add",
    "scheduler.remove",
    "scheduler.toggle",
    # Connectors marketplace
    "connectors.marketplace.list",
    "connectors.marketplace.install",
]


# ──────────────────────────────────────────────────────────────────
# TypedDict payloads for the new (Wave 1+) events
# ──────────────────────────────────────────────────────────────────


class TaskUpdateData(TypedDict, total=False):
    todo_id: str
    status: Literal["pending", "in_progress", "completed", "blocked"]
    content: str
    activeForm: str


class AgentProgressData(TypedDict, total=False):
    id: str
    stage: Literal["status", "planning", "tool", "approval", "verification", "final"]
    phase: Literal["orienting", "planning", "model", "tool", "approval", "verify", "final", "recover", "status", "iteration"]
    status: Literal["running", "completed", "failed", "info"]
    message: str
    label: str
    summary: str
    visibility: Literal["timeline", "compact", "debug"]
    detail: str
    tool_call_id: str
    tool_name: str
    group_id: str
    step_id: str
    count: int


class AgentLoopData(TypedDict, total=False):
    loop_id: str
    iteration_id: str
    status: Literal["running", "completed", "failed", "interrupted"]
    title: str
    summary: str
    started_at: int
    completed_at: int
    duration_ms: int
    item_count: int
    tool_call_count: int
    default_collapsed: bool


class AgentItemData(TypedDict, total=False):
    id: str
    loop_id: str
    iteration_id: str
    parent_id: str
    kind: Literal["process_text", "action_summary", "observation", "status", "plan", "tool_group"]
    role: Literal["assistant", "runtime", "system", "tool"]
    source: Literal["model", "runtime", "system", "tool"]
    status: Literal["running", "completed", "failed", "info"]
    title: str
    content: str
    summary: str
    visibility: Literal["timeline", "compact", "debug"]
    created_at: int
    order: int
    seq: int
    default_collapsed: bool
    group_id: str
    step_id: str
    tool_call_ids: list[str]


class ThinkingDeltaData(TypedDict, total=False):
    content: str
    source: Literal["provider", "model_preamble", "runtime"]
    visibility: Literal["debug", "timeline", "compact"]
    is_raw_provider_reasoning: bool


class ToolCallData(TypedDict, total=False):
    id: str
    name: str
    args: dict[str, Any]
    status: str
    started_at: int
    display_hint: str
    input_summary: str
    result_kind: str
    activity_kind: str
    group_id: str
    step_id: str
    iteration_id: str
    phase: str


class ToolOutputDeltaData(TypedDict, total=False):
    id: str
    output: str
    stream: Literal["stdout", "stderr"]


class ToolResultData(TypedDict, total=False):
    id: str
    summary: str
    artifact_id: str
    is_error: bool
    diff: Any
    source_url: str
    extraction_status: Literal["ok", "partial", "failed"]
    content_preview: str
    evidence_type: Literal["candidate", "fetched", "artifact", "command", "file"]
    status: str
    duration_ms: int
    display_summary: str
    result_kind: str
    group_id: str
    step_id: str
    limitation: str
    provider: str
    provider_error_type: str
    error_info: dict[str, Any]
    error_kind: str
    user_summary: str
    developer_detail: str
    recoverable: bool
    projection: Literal["silent", "status", "warning", "error", "approval"]
    iteration_id: str
    phase: str


class SubagentStartData(TypedDict, total=False):
    subagent_id: str
    parent_id: str
    role: str
    prompt: str


class SubagentEventData(TypedDict, total=False):
    subagent_id: str
    event: dict[str, Any]      # nested AgentEvent payload


class SubagentDoneData(TypedDict, total=False):
    subagent_id: str
    summary: str
    error: str


class CitationData(TypedDict, total=False):
    message_id: str
    source: str
    range: tuple[int, int]
    label: str
    url: str
    title: str


class ArtifactPreviewData(TypedDict, total=False):
    artifact_id: str
    kind: Literal["file", "diff", "image", "json", "code", "text"]
    summary: str
    bytes: int


class InspectorUpdateData(TypedDict, total=False):
    target_kind: Literal["message", "tool_call", "artifact", "subagent", "budget"]
    target_id: str
    payload: dict[str, Any]


class BudgetWarningData(TypedDict, total=False):
    bucket: str
    percent: float
    will_compact: bool
    threshold: float


class TaskEditCommand(TypedDict, total=False):
    todo_id: str
    status: Literal["pending", "in_progress", "completed", "blocked"]


class PreviewServerDetectedData(TypedDict, total=False):
    port: int
    url: str
    name: str
    framework: str


class PreviewServersUpdatedData(TypedDict, total=False):
    servers: list[PreviewServerDetectedData]


class PreviewNavigateCommand(TypedDict, total=False):
    url: str


class PreviewLaunchConfigData(TypedDict, total=False):
    name: str
    command: str
    cwd: str
    port: int
    url: str
    auto_port: bool
    source: str


class PreviewLaunchProcessData(TypedDict, total=False):
    id: str
    name: str
    command: str
    cwd: str
    port: int
    url: str
    pid: int
    status: str


class PreviewServerReadyData(TypedDict, total=False):
    id: str
    url: str
    port: int


class PreviewServerOutputData(TypedDict, total=False):
    id: str
    stream: Literal["stdout", "stderr"]
    line: str


class PreviewServerCrashedData(TypedDict, total=False):
    id: str
    exit_code: int
    stderr_tail: list[str]


class PreviewVerifiedData(TypedDict, total=False):
    url: str
    ok: bool
    status_code: int
    elapsed_ms: int
    error: str


class McpLifecycleData(TypedDict, total=False):
    server_name: str
    status: str
    phase: Literal[
        "connecting", "connected", "reconnecting",
        "auth_required", "expired", "failed", "stopped",
    ]
    message: str
    recoverable: bool
    requires_user_action: bool


class McpProgressData(TypedDict, total=False):
    server_name: str
    operation: str
    message: str
    progress: float  # optional 0-1; omitted when the transport reports no fraction
    status: Literal["running", "completed", "failed"]



# ──────────────────────────────────────────────────────────────────
# Convenience: full sets for runtime validation
# ──────────────────────────────────────────────────────────────────

SERVER_EVENT_TYPES: frozenset[str] = frozenset(ServerEventType.__args__)  # type: ignore[attr-defined]
CLIENT_COMMAND_TYPES: frozenset[str] = frozenset(ClientCommandType.__args__)  # type: ignore[attr-defined]


def is_server_event(t: str) -> bool:
    return t in SERVER_EVENT_TYPES


def is_client_command(t: str) -> bool:
    return t in CLIENT_COMMAND_TYPES


__all__ = [
    "ServerEventType",
    "ClientCommandType",
    "SERVER_EVENT_TYPES",
    "CLIENT_COMMAND_TYPES",
    "is_server_event",
    "is_client_command",
    "TaskUpdateData",
    "AgentProgressData",
    "AgentLoopData",
    "AgentItemData",
    "ThinkingDeltaData",
    "ToolCallData",
    "ToolOutputDeltaData",
    "ToolResultData",
    "SubagentStartData",
    "SubagentEventData",
    "SubagentDoneData",
    "CitationData",
    "ArtifactPreviewData",
    "InspectorUpdateData",
    "BudgetWarningData",
    "TaskEditCommand",
    "PreviewServerDetectedData",
    "PreviewServersUpdatedData",
    "PreviewNavigateCommand",
    "PreviewLaunchConfigData",
    "PreviewLaunchProcessData",
    "PreviewServerReadyData",
    "PreviewServerOutputData",
    "PreviewServerCrashedData",
    "PreviewVerifiedData",
    "McpLifecycleData",
    "McpProgressData",
]
