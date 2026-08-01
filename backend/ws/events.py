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
    "item.started",
    "agent_message.delta",
    "item.completed",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "tool_result",
    "agent.run.started",
    "agent.run.completed",
    "user_message.queue.updated",
    "agent.item",
    "agent.progress",
    "runtime.span",
    "task.update",
    "approval_request",
    "permission.decision",
    "approval.cancelled",
    "approval.file_diff",
    "ask_user",
    # Context lifecycle
    "context_usage",
    "context_compacted",
    "context_forked",
    "context_ledger",
    "context_side_query_result",
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
    # SDK / provider transparency
    "stream_event",             # raw provider stream event passthrough (SDK mode)
    "rate_limit",               # rate-limit / quota event for differentiated UI
    "session.state_changed",    # idle/working state signal
    # Tool-use summary (user-facing, async-generated)
    "tool_use_summary",
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
    "checkpoint.created",
    "checkpoint.list",
    "checkpoint.rewound",
    "checkpoint.run.list",
    "checkpoint.run.resume",
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
    "background.started",
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
    "session.replay",
    "session.synced",
    "runtime.capabilities",
    "client.command.ack",
    "pong",
    "control_request",
    # Wave 1+ new: Subagents + Inspector + Citations
    "plan_step_updated",
    "plan_updated",
    "subagent.start",
    "subagent.event",
    "subagent.mailbox",
    "subagent.progress",
    "subagent.done",
    "parent.notifications",
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
    "user_message.queue.cancel",
    "user_message.queue.steer",
    "approval",
    "answer",
    "interrupt",
    "ping",
    # Control plane
    "control_response",
    "control_cancel_request",
    # Artifacts / approvals
    "read_artifact",
    "approval.file_diff",
    # Conversation lifecycle
    "conversation.create",
    "conversation.clone",
    "conversation.merge",
    "conversation.export",
    "conversation.switch",
    "conversation.list",
    "conversation.clear",
    "conversation.truncate",
    "conversation.delete",
    "conversation.archive",
    "conversation.unarchive",
    "conversation.rename",
    "conversation.memory_mode.set",
    "conversation.permission_mode.set",
    "conversation.goal.set",
    "conversation.worktree.cleanup",
    "conversation.worktree.handoff.preflight",
    "conversation.worktree.handoff.execute",
    # Context control
    "context.compact",
    "context.fork",
    "context.side_query",
    "context.ledger",
    # Session inspection
    "session.tasks.inspect",
    "session.status.inspect",
    "session.usage.inspect",
    "session.permissions.inspect",
    "runtime.capabilities.inspect",
    # LLM
    "llm.model.set",
    # Permission rules
    "conversation.permission.rules.list",
    "conversation.permission.rules.add",
    "conversation.permission.rules.remove",
    "permissions.content_rule.add",
    # Checkpoints
    "checkpoint.list",
    "checkpoint.rewind",
    "checkpoint.run.list",
    "agent.resume",
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
    "plan.edit",                 # user accepts/rejects a proposed plan
    "task.stop",
    "subagent.cancel",           # user kills a subagent
    "subagent.status",           # user refreshes/collects a subagent result
    "send_message",              # user steers a running subagent through its mailbox
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
    "mcp.oauth.login",
    "env.list",
    "env.set",
    "env.delete",
    "git.pr_status",
    "git.pr_automation.set",
    # Scheduler
    "scheduler.list",
    "scheduler.add",
    "scheduler.remove",
    "scheduler.toggle",
    "scheduler.run_now",
    "scheduler.retry",
    "scheduler.cancel",
    # Connectors marketplace
    "connectors.marketplace.list",
    "connectors.marketplace.install",
]


# ──────────────────────────────────────────────────────────────────
# TypedDict payloads for the new (Wave 1+) events
# ──────────────────────────────────────────────────────────────────


class AgentMessageItemData(TypedDict, total=False):
    id: str
    type: Literal["agent_message"]
    text: str
    source: Literal["model_final", "reply", "partial"]
    status: Literal["in_progress", "completed", "partial"]


class ItemStartedData(TypedDict, total=False):
    item: AgentMessageItemData


class AgentMessageDeltaData(TypedDict, total=False):
    item_id: str
    delta: str


class ItemCompletedData(TypedDict, total=False):
    item: AgentMessageItemData
    finish_reason: str
    provider_raw: dict[str, Any]
    attachments: list[dict[str, Any]]


class TaskUpdateData(TypedDict, total=False):
    todo_id: str
    status: Literal["pending", "in_progress", "completed", "blocked"]
    content: str
    activeForm: str


class AgentProgressData(TypedDict, total=False):
    id: str
    stage: Literal["status", "planning", "tool", "approval", "verification", "final"]
    phase: Literal["orienting", "planning", "model", "tool", "approval", "verify", "final", "recover", "status", "iteration", "subagent", "cache"]
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
    ephemeral: bool       # if True, UI should replace (not append) this progress message


class RuntimeSpanData(TypedDict, total=False):
    event: str
    span_id: str
    parent_span_id: str
    run_id: str
    turn_id: str
    message_id: str
    iteration_id: str
    phase: str
    status: Literal["running", "completed", "failed", "info"]
    label: str
    summary: str
    started_at: int
    ended_at: int
    duration_ms: int
    tool_call_id: str
    tool_name: str
    agent_id: str
    waiting_on: str
    blocking_reason: str
    ui_visible: bool
    debug_only: bool
    data: dict[str, Any]


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


class AgentRunData(TypedDict, total=False):
    run_id: str
    conversation_id: str
    parent_run_id: str
    role: str
    phase: Literal["plan", "execute", "verify", "recover", "final"]
    status: Literal["running", "completed", "partial", "failed", "cancelled", "interrupted"]
    budget: dict[str, Any]
    started_at: int
    completed_at: int | None
    task_id: str
    session_id: str
    summary: str
    error: str


class AgentItemData(TypedDict, total=False):
    id: str
    loop_id: str
    iteration_id: str
    parent_id: str
    kind: Literal["process_text", "action_summary", "observation", "status", "plan", "tool_group", "skill"]
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
    skill_name: str
    trigger_mode: str
    source_level: str
    reason: str
    token_estimate: int


class ThinkingDeltaData(TypedDict, total=False):
    content: str
    source: Literal["provider", "model_preamble", "post_tool", "runtime"]
    visibility: Literal["debug", "timeline", "compact"]
    is_raw_provider_reasoning: bool
    provider_reasoning_type: str


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
    turn_id: str
    iteration_id: str
    phase: str


class ToolOutputDeltaData(TypedDict, total=False):
    id: str
    output: str
    stream: Literal["stdout", "stderr"]
    turn_id: str
    iteration_id: str
    step_id: str


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
    turn_id: str
    iteration_id: str
    phase: str


class SubagentStartData(TypedDict, total=False):
    subagent_id: str
    parent_id: str
    role: str
    prompt: str
    current_activity: str
    waiting_on: str
    last_progress_at: int


class SubagentEventData(TypedDict, total=False):
    subagent_id: str
    event: dict[str, Any]      # nested AgentEvent payload


class SubagentDoneData(TypedDict, total=False):
    subagent_id: str
    summary: str
    error: str
    duration_ms: int
    iterations: int
    tool_call_count: int
    timed_out: bool
    status: Literal["completed", "partial", "failed", "cancelled"]
    termination_reason: str
    initiator: str
    result: dict[str, Any]
    record: dict[str, Any]
    prompt_cache_fork: dict[str, Any]


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
    target_kind: Literal["message", "tool_call", "artifact", "file", "diff", "subagent", "budget", "provider"]
    target_id: str
    payload: dict[str, Any]


class BudgetWarningData(TypedDict, total=False):
    bucket: str
    percent: float
    will_compact: bool
    threshold: float


class StreamEventData(TypedDict, total=False):
    provider: str
    event_type: str          # e.g. "message_start", "content_block_delta", "message_delta"
    data: dict[str, Any]     # raw provider event payload
    sdk_only: bool           # if True, UI should not render this


class RateLimitData(TypedDict, total=False):
    provider: str
    error_type: str          # "rate_limit" | "quota_exceeded" | "concurrency_limit"
    retry_after_seconds: float
    retry_at: int            # epoch ms when retry is expected
    message: str
    recoverable: bool


class SessionStateData(TypedDict, total=False):
    state: Literal["idle", "working"]
    conversation_id: str
    run_id: str
    reason: str


class ToolUseSummaryData(TypedDict, total=False):
    summary: str
    iteration_id: str
    tool_call_ids: list[str]
    tool_count: int
    generated_by: Literal["runtime", "llm"]


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


class RuntimeCapabilitiesData(TypedDict, total=False):
    session_id: str
    source: str
    capabilities: dict[str, Any]



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
    "AgentMessageItemData",
    "ItemStartedData",
    "AgentMessageDeltaData",
    "ItemCompletedData",
    "TaskUpdateData",
    "AgentProgressData",
    "AgentLoopData",
    "AgentRunData",
    "AgentItemData",
    "RuntimeSpanData",
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
    "StreamEventData",
    "RateLimitData",
    "SessionStateData",
    "ToolUseSummaryData",
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
    "RuntimeCapabilitiesData",
]
