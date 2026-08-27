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

from typing import Any, Literal, NotRequired, TypedDict


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
    "permission.decision",
    "approval.cancelled",
    "approval.file_diff",
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
    # MCP
    "mcp_status",
    "mcp.lifecycle",
    "mcp.progress",
    "llm.provider.oauth.auth",
    "llm.provider.oauth.device_code",
    "llm.provider.oauth.info",
    "llm.provider.oauth.progress",
    # Environment / Git status
    "env.list",
    "git.pr_status",
    # Scheduler
    "scheduler.list",
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
    "background.stalled",
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
    "turn.plan.updated",
    "turn.diff.updated",
    "subagent.start",
    "subagent.event",
    "subagent.mailbox",
    "subagent.progress",
    "subagent.done",
    # A teammate plan awaiting the user's decision. Answered by the
    # `subagent.plan_review` client command; it is the only approval path for a
    # session whose permission mode does not pre-authorize broad execution.
    "subagent.plan_approval_requested",
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
    "memory.reset",
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
    "llm.provider.oauth.login",
    "llm.provider.oauth.logout",
    "llm.provider.oauth.status",
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
    "terminal.restart",
    "terminal.list",
    "terminal.snapshot.request",
    "terminal.clear",
    "terminal.mirror.created",
    "terminal.mirror.output",
    "terminal.mirror.exit",
    # Workspace
    "workspace.import",
    "workspace.switch",
    "workspace.recent",
    "workspace.recent.remove",
    "workspace.recent.clear",
    # Session restore / sync
    "session.restore",
    "session.sync",
    # Wave 1+ new
    "subagent.cancel",           # user kills a subagent
    "subagent.status",           # user refreshes/collects a subagent result
    "subagent.transcript",       # user opens a read-only child-thread replay
    "subagent.plan_review",      # user approves/rejects a teammate plan
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
    "mcp.inventory.list",
    "mcp.inventory.cancel",
    "mcp.add",
    "mcp.update",
    "mcp.toggle",
    "mcp.remove",
    "mcp.restart",
    "mcp.oauth.login",
    "mcp.oauth.logout",
    "mcp.project.approve",
    "mcp.project.approve_all",
    "mcp.project.reject",
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
]


# ──────────────────────────────────────────────────────────────────
# TypedDict payloads for the new (Wave 1+) events
# ──────────────────────────────────────────────────────────────────


class AgentMessageItemData(TypedDict, total=False):
    id: str
    type: Literal["agent_message"]
    text: str
    source: Literal["model_final", "reply", "partial", "commentary", "cancelled"]
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


class TurnDiffUpdatedData(TypedDict):
    """MiniCode ``turn/diff/updated`` notification payload."""

    thread_id: str
    turn_id: str
    diff: str


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


class ImageChunkLiveData(TypedDict):
    conversation_id: str
    message_id: str
    image_data: str
    media_type: str


class ImageChunkReplayData(TypedDict):
    conversation_id: str
    message_id: str
    media_type: str
    image_data_omitted: Literal[True]
    image_data_size: int


ImageChunkData = ImageChunkLiveData | ImageChunkReplayData


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
    kind: Literal["process_text", "observation", "status", "plan", "tool_group", "skill"]
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
    visibility: Literal["timeline", "compact", "debug"]
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


class CommandOutputChunkData(TypedDict):
    conversation_id: str
    message_id: str
    content: str
    stream: Literal["stdout", "stderr"]
    turn_id: NotRequired[str]
    id: NotRequired[str]
    tool_call_id: NotRequired[str]


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
    activity_kind: str
    visibility: Literal["timeline", "compact", "debug"]
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


class ParentNotificationsData(TypedDict):
    count: int
    parent_run_id: str
    conversation_id: str


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
    media_type: str
    url: str
    conversation_id: str
    message_id: str


class CommandAvailabilityData(TypedDict):
    kind: str
    scope: str
    reason: NotRequired[str]


class CommandArgumentData(TypedDict):
    value: str
    description: str


class CommandCatalogEntryData(TypedDict):
    name: str
    command: str
    label: str
    description: str
    type: Literal["local", "template", "protocol"]
    source: str
    enabled: bool
    availability: CommandAvailabilityData
    id: NotRequired[str]
    kind: NotRequired[str]
    panel: NotRequired[str]
    args: NotRequired[list[CommandArgumentData]]
    extension_path: NotRequired[str]
    source_path: NotRequired[str]
    template: NotRequired[str]
    search_text: NotRequired[str]
    argument_hint: NotRequired[str]
    argument_names: NotRequired[list[str]]
    base_dir: NotRequired[str]
    is_skill_file: NotRequired[bool]


class CommandsListData(TypedDict):
    conversation_id: str | None
    commands: list[CommandCatalogEntryData]
    request_id: NotRequired[str]


class CheckpointOriginData(TypedDict):
    run_id: str
    conversation_id: str
    session_id: str
    sequence: int
    timestamp: int | float
    stopped_reason: str


class SystemNoticeData(TypedDict):
    conversation_id: str
    content: NotRequired[str]
    title: NotRequired[str]
    message: NotRequired[str]
    data: NotRequired[dict[str, Any]]
    checkpoint_origin: NotRequired[CheckpointOriginData]


class PongData(TypedDict):
    pass


class WorkspaceProjectData(TypedDict):
    root_path: str
    project_type: str
    name: str
    description: str
    file_count: int
    total_size: int
    has_project_instructions: bool
    index_truncated: bool


class WorkspaceImportedData(TypedDict):
    conversation_id: str
    workspace_root: str
    project: WorkspaceProjectData
    summary: str
    file_count: int
    request_id: NotRequired[str]


class InspectorUpdateData(TypedDict, total=False):
    target_kind: Literal[
        "message",
        "tool_call",
        "artifact",
        "file",
        "diff",
        "subagent",
        "budget",
        "provider",
        "permission",
        "checkpoint",
        "workspace",
        "guidelines",
        "session",
    ]
    target_id: str
    payload: dict[str, Any]


class BudgetWarningData(TypedDict, total=False):
    bucket: str
    percent: float
    will_compact: bool
    threshold: float


class ConversationHydrationUpdatedData(TypedDict):
    conversation_id: str
    is_hydrating: bool


class ConversationCompactionUpdatedData(TypedDict):
    conversation_id: str
    state: Literal["compacted"]
    summary: str


class ConversationSummaryUpdatedData(TypedDict):
    conversation_id: str
    summary: str
    title: str
    updated_at: str
    memory_mode: Literal["enabled", "disabled", "polluted"]
    memory_polluted: bool
    memory_pollution_sources: list[str]


class ContextForkedData(TypedDict):
    conversation_id: str
    fork_id: str
    message_index: int
    context_history_index: int
    history_length: int
    estimated_tokens: int
    parent_conversation_id: str
    branch_created: bool
    branch_activated: bool
    message_id: NotRequired[str]
    created_at: NotRequired[str]
    status: NotRequired[str]
    branch_conversation_id: NotRequired[str]


ContextLedgerCategory = Literal[
    "system_runtime",
    "guidelines",
    "skills",
    "files_attachments",
    "history",
    "tool_results",
    "memory",
    "compaction_summaries",
]


class ContextLedgerEntryData(TypedDict):
    category: ContextLedgerCategory
    label: str
    estimated_tokens: int
    item_count: int
    source_count: int
    sources: list[str]


class ContextLedgerData(TypedDict):
    conversation_id: str
    schema_version: Literal[1]
    estimated_tokens: int
    actual_tokens: int
    compaction_count: int
    native_attachment_tokens: int
    native_attachment_count: int
    entries: list[ContextLedgerEntryData]


class ContextSideQueryResultData(TypedDict):
    conversation_id: str
    query: str
    result: str
    focus: str


class ControlCanUseToolRequestData(TypedDict):
    subtype: Literal["can_use_tool"]
    tool_name: str
    input: dict[str, Any]
    tool_use_id: str
    diff: NotRequired[str | dict[str, Any]]
    source_agent: NotRequired[str]
    source_thread: NotRequired[str]
    source_tool: NotRequired[str]


class ControlElicitationRequestData(TypedDict):
    subtype: Literal["elicitation"]
    tool_use_id: str
    prompt: str
    question: str
    schema: NotRequired[dict[str, Any]]
    options: NotRequired[list[Any]]
    choices: NotRequired[list[Any]]
    allowed_values: NotRequired[list[Any]]


class ControlProviderAuthPromptRequestData(TypedDict):
    subtype: Literal["provider_auth_prompt"]
    prompt: str
    provider: str
    prompt_type: Literal["text", "secret", "select", "manual_code"]
    placeholder: NotRequired[str]
    allow_empty: bool
    allow_custom: bool
    options: NotRequired[list[dict[str, str]]]


class ProviderOAuthAuthEventData(TypedDict):
    conversation_id: str
    provider: str
    url: str
    instructions: NotRequired[str]


class ProviderOAuthDeviceCodeEventData(TypedDict):
    conversation_id: str
    provider: str
    userCode: str
    verificationUri: str
    intervalSeconds: NotRequired[int | float]
    expiresInSeconds: NotRequired[int | float]


class ProviderOAuthInfoLinkData(TypedDict):
    url: str
    label: NotRequired[str]


class ProviderOAuthInfoEventData(TypedDict):
    conversation_id: str
    provider: str
    message: str
    links: NotRequired[list[ProviderOAuthInfoLinkData]]


class ProviderOAuthProgressEventData(TypedDict):
    conversation_id: str
    provider: str
    message: str


class ControlRequestData(TypedDict):
    request_id: str
    conversation_id: str
    request: (
        ControlCanUseToolRequestData
        | ControlElicitationRequestData
        | ControlProviderAuthPromptRequestData
    )
    turn_id: NotRequired[str]
    message_id: NotRequired[str]
    workspace_root: NotRequired[str]
    permission_mode: NotRequired[str]
    workspace_scope: NotRequired[str]
    timeout_seconds: NotRequired[float]
    expires_at: NotRequired[int]


class BackgroundStalledData(TypedDict):
    command_id: str
    conversation_id: str
    tail: str
    advice: str
    command: NotRequired[str]
    description: NotRequired[str]


class CheckpointRecordData(TypedDict):
    id: str
    conversation_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    workspace_root: str
    paths: list[str]
    created_at: str
    metadata: dict[str, Any]


class GuidelinesUpdatedData(TypedDict, total=False):
    message: str
    conversation_id: str
    workspace_root: str
    path: str
    cache_cleared: bool
    effective_from: Literal["next_turn"]
    source_kind: Literal["direct", "import"]
    parent_path: str


class PermissionRuleData(TypedDict, total=False):
    pattern: str
    source: str
    level: str
    tool: str
    rule_content: str
    behavior: str
    destination: str


class PermissionRulesData(TypedDict):
    mode: str
    context_source: str
    system_deny: list[PermissionRuleData]
    session_deny: list[PermissionRuleData]
    session_overrides: list[PermissionRuleData]
    session_prompt_rules: list[PermissionRuleData]


class StreamEventData(TypedDict):
    provider: str
    event_type: str          # e.g. "message_start", "content_block_delta", "message_delta"
    data: dict[str, Any]     # raw provider event payload
    sdk_only: NotRequired[bool]           # if True, UI should not render this
    conversation_id: NotRequired[str]


class RateLimitData(TypedDict):
    provider: str
    error_type: str          # "rate_limit" | "quota_exceeded" | "concurrency_limit"
    recoverable: bool
    retry_after_seconds: NotRequired[float]
    retry_at: NotRequired[int]            # epoch ms when retry is expected
    message: NotRequired[str]
    conversation_id: NotRequired[str]


class SessionStateData(TypedDict):
    state: Literal["idle", "working"]
    conversation_id: NotRequired[str]
    run_id: NotRequired[str]
    reason: NotRequired[str]


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
    auth_status: Literal["unsupported", "not_logged_in", "oauth"]


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
    "ImageChunkLiveData",
    "ImageChunkReplayData",
    "ImageChunkData",
    "TaskUpdateData",
    "TurnDiffUpdatedData",
    "AgentProgressData",
    "AgentLoopData",
    "AgentRunData",
    "AgentItemData",
    "RuntimeSpanData",
    "ThinkingDeltaData",
    "ToolCallData",
    "ToolOutputDeltaData",
    "CommandOutputChunkData",
    "ToolResultData",
    "SubagentStartData",
    "SubagentEventData",
    "SubagentDoneData",
    "ParentNotificationsData",
    "CitationData",
    "ArtifactPreviewData",
    "CommandAvailabilityData",
    "CommandArgumentData",
    "CommandCatalogEntryData",
    "CommandsListData",
    "CheckpointOriginData",
    "SystemNoticeData",
    "PongData",
    "WorkspaceProjectData",
    "WorkspaceImportedData",
    "InspectorUpdateData",
    "BudgetWarningData",
    "ConversationHydrationUpdatedData",
    "ConversationCompactionUpdatedData",
    "ConversationSummaryUpdatedData",
    "ContextForkedData",
    "ContextLedgerCategory",
    "ContextLedgerEntryData",
    "ContextLedgerData",
    "ContextSideQueryResultData",
    "ControlCanUseToolRequestData",
    "ControlElicitationRequestData",
    "ControlProviderAuthPromptRequestData",
    "ControlRequestData",
    "ProviderOAuthAuthEventData",
    "ProviderOAuthDeviceCodeEventData",
    "ProviderOAuthInfoLinkData",
    "ProviderOAuthInfoEventData",
    "ProviderOAuthProgressEventData",
    "BackgroundStalledData",
    "CheckpointRecordData",
    "GuidelinesUpdatedData",
    "PermissionRuleData",
    "PermissionRulesData",
    "StreamEventData",
    "RateLimitData",
    "SessionStateData",
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
