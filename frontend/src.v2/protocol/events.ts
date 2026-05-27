/**
 * WebSocket protocol — frontend mirror of backend/ws/events.py.
 *
 * Single source of truth for the TypeScript types of every event that flows
 * between backend and frontend. Keep in lockstep with backend/ws/events.py.
 *
 * Run `python scripts/check-protocol-sync.py` to verify the two stay aligned.
 */

// ──────────────────────────────────────────────────────────────────
// Server → Client event type union (mirror SERVER_EVENT_TYPES)
// ──────────────────────────────────────────────────────────────────

export type ServerEventType =
  // Streaming text + tool execution
  | "text_chunk"
  | "image_chunk"
  | "thinking_delta"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "agent.progress"
  | "task.update"
  | "approval_request"
  | "approval.cancelled"
  | "approval.file_diff"
  | "ask_user"
  // Skills
  | "skill_activated"
  | "skill_deactivated"
  // Context lifecycle
  | "context_usage"
  | "context_compacted"
  | "budget_update"
  | "budget.warning"
  // Commands / artifacts
  | "command.result"
  | "command_output_chunk"
  | "artifact_content"
  | "artifact.preview"
  // Loop terminal
  | "done"
  | "error"
  | "stream_resume"
  // MCP
  | "mcp_status"
  | "env.list"
  | "git.pr_status"
  | "scheduler.list"
  | "connectors.marketplace.list"
  | "checkpoint.list"
  | "checkpoint.rewound"
  // File watcher
  | "file.changed"
  // Terminal
  | "terminal.output"
  | "terminal.exit"
  | "terminal.created"
  | "terminal.killed"
  | "terminal.list"
  // Background commands
  | "background.completed"
  // Guidelines / permissions
  | "guidelines.updated"
  | "permission.mode.updated"
  | "permission.rules.updated"
  // Conversation runtime
  | "conversation.hydration.updated"
  | "conversation.compaction.updated"
  | "conversation.summary.updated"
  | "conversation.list"
  | "conversation.switched"
  // LLM settings
  | "llm.model.updated"
  // Workspace
  | "workspace.imported"
  | "workspace.recent.list"
  // Session / control plane
  | "session.restored"
  | "session.synced"
  | "pong"
  | "control_request"
  // Wave 1+ new
  | "plan.update"
  | "subagent.start"
  | "subagent.event"
  | "subagent.done"
  | "citation.add"
  | "inspector.update"
  | "skills.list"
  | "skills.marketplace.list"
  | "commands.list"
  | "system_notice"
  // Preview
  | "preview.servers.updated"
  | "preview.server.detected"
  | "preview.server.stopped"
  | "preview.navigated"
  | "preview.refreshed"
  | "preview.launch.config"
  | "preview.launch.started"
  | "preview.launch.stopped"
  | "preview.server.ready"
  | "preview.server.output"
  | "preview.server.crashed"
  | "preview.server.unhealthy"
  | "preview.verified"
  // Git diff
  | "diff.git_working_tree"
  | "diff.git_staged"
  | "diff.git_stage_file"
  | "diff.git_unstage_file";

// ──────────────────────────────────────────────────────────────────
// Client → Server command type union (mirror CLIENT_COMMAND_TYPES)
// ──────────────────────────────────────────────────────────────────

export type ClientCommandType =
  | "user_message"
  | "approval"
  | "answer"
  | "interrupt"
  | "ping"
  | "control_response"
  | "control_cancel_request"
  | "load_skill"
  | "unload_skill"
  | "read_artifact"
  | "approval.file_diff"
  | "conversation.create"
  | "conversation.switch"
  | "conversation.list"
  | "conversation.clear"
  | "conversation.delete"
  | "conversation.archive"
  | "conversation.unarchive"
  | "conversation.rename"
  | "conversation.memory_mode.set"
  | "conversation.permission_mode.set"
  | "conversation.worktree.cleanup"
  | "session.tasks.inspect"
  | "session.status.inspect"
  | "session.usage.inspect"
  | "session.permissions.inspect"
  | "llm.model.set"
  | "conversation.permission.rules.list"
  | "conversation.permission.rules.add"
  | "conversation.permission.rules.remove"
  | "checkpoint.list"
  | "checkpoint.rewind"
  | "terminal.create"
  | "terminal.input"
  | "terminal.resize"
  | "terminal.kill"
  | "terminal.list"
  | "workspace.import"
  | "workspace.switch"
  | "workspace.recent"
  | "session.restore"
  | "session.sync"
  | "plan.edit"
  | "task.edit"
  | "task.stop"
  | "subagent.cancel"
  | "inspector.focus"
  | "workspace.set"
  | "terminal.exec"
  | "skills.list"
  | "skills.install"
  | "skills.marketplace.list"
  | "commands.list"
  // Preview
  | "preview.detect"
  | "preview.navigate"
  | "preview.refresh"
  | "preview.launch.config"
  | "preview.launch.start"
  | "preview.launch.stop"
  | "preview.verify"
  | "llm.config.set"
  // Git diff
  | "diff.git_working_tree"
  | "diff.git_staged"
  | "diff.git_stage_file"
  | "diff.git_unstage_file"
  // MCP Connectors
  | "mcp.list"
  | "mcp.add"
  | "mcp.remove"
  | "mcp.restart"
  | "env.list"
  | "env.set"
  | "env.delete"
  // Git CI
  | "git.pr_status"
  | "approval.respond"
  // Scheduler
  | "scheduler.list"
  | "scheduler.add"
  | "scheduler.remove"
  | "scheduler.toggle"
  // Connectors Marketplace
  | "connectors.marketplace.list"
  | "connectors.marketplace.install";

// ──────────────────────────────────────────────────────────────────
// Event payload types
// ──────────────────────────────────────────────────────────────────

export interface TextChunkEvent {
  type: "text_chunk";
  content: string;
}

export interface ThinkingDeltaEvent {
  type: "thinking_delta" | "thinking";
  content: string;
}

export interface ToolCallEvent {
  type: "tool_call";
  id: string;
  name: string;
  args: Record<string, unknown>;
  status?: "running" | string;
  started_at?: number;
  display_hint?: string;
  input_summary?: string;
}

export interface ToolResultEvent {
  type: "tool_result";
  id: string;
  summary: string;
  artifact_id?: string;
  is_error?: boolean;
  diff?: unknown;
  source_url?: string;
  extraction_status?: "ok" | "partial" | "failed" | string;
  content_preview?: string;
  evidence_type?: "candidate" | "fetched" | "artifact" | "command" | "file" | string;
  status?: "success" | "failed" | "blocked" | string;
  duration_ms?: number;
  display_summary?: string;
  result_kind?: "web" | "command" | "file" | "edit" | "search" | "mcp" | "generic" | string;
  limitation?: string;
}

export interface AgentProgressEvent {
  type: "agent.progress";
  id: string;
  stage: "status" | "planning" | "tool" | "approval" | "verification" | "final";
  phase?: "orienting" | "planning" | "model" | "tool" | "approval" | "verify" | "final" | "recover" | "status";
  status: "running" | "completed" | "failed" | "info";
  message: string;
  label?: string;
  summary?: string;
  visibility?: "timeline" | "compact" | "debug";
  detail?: string;
  tool_call_id?: string;
  tool_name?: string;
  group_id?: string;
  step_id?: string;
  count?: number;
}

export interface ApprovalRequestEvent {
  type: "approval_request";
  tool_call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  diff?: unknown;
}

export interface ApprovalCancelledEvent {
  type: "approval.cancelled";
  request_ids: string[];
  reason?: string;
}

export interface AskUserEvent {
  type: "ask_user";
  tool_call_id: string;
  question: string;
}

export interface DoneEvent {
  type: "done";
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
  };
}

export interface ErrorEvent {
  type: "error";
  message: string;
  recoverable: boolean;
  error_type: "api" | "tool" | "budget" | "stagnant" | string;
  error_code?: string;
}

export interface StreamResumeEvent {
  type: "stream_resume";
  conversation_id: string;
  message_id: string | null;
  tool_calls_pending: Array<{ id: string; name: string; args: Record<string, unknown> }>;
}

export interface ContextUsageEvent {
  type: "context_usage";
  used: number;
  limit: number;
}

export interface ContextCompactedEvent {
  type: "context_compacted";
  summary: string;
}

export interface BudgetUpdateEvent {
  type: "budget_update";
  used?: number;
  total?: number;
  breakdown?: Record<string, number>;
  buckets?: Record<string, { used: number; limit: number }>;
  total_used?: number;
  total_limit?: number;
}

export interface BudgetWarningEvent {
  type: "budget.warning";
  bucket: string;
  percent: number;
  will_compact: boolean;
}

export interface PlanStep {
  id: string;
  title: string;
  detail?: string;
  status: "pending" | "running" | "done" | "skipped" | "failed";
  tool_hint?: string;
}

export interface PlanUpdateEvent {
  type: "plan.update";
  plan_id: string;
  status: "draft" | "accepted" | "executing" | "completed" | "cancelled";
  steps: PlanStep[];
  current_step?: number;
  note?: string;
}

export interface TaskUpdateEvent {
  type: "task.update";
  todo_id: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
  content: string;
  activeForm?: string;
}

export interface SubagentStartEvent {
  type: "subagent.start";
  subagent_id: string;
  parent_id: string;
  role: string;
  prompt?: string;
}

export interface SubagentEventEvent {
  type: "subagent.event";
  subagent_id: string;
  event: ServerEvent;
}

export interface SubagentDoneEvent {
  type: "subagent.done";
  subagent_id: string;
  summary?: string;
  error?: string;
}

export interface CitationAddEvent {
  type: "citation.add";
  message_id: string;
  source: string;
  range: [number, number];
  label?: string;
  url?: string;
  title?: string;
}

export interface ArtifactPreviewEvent {
  type: "artifact.preview";
  artifact_id: string;
  kind: "file" | "diff" | "image" | "json" | "code" | "text";
  summary: string;
  bytes?: number;
  media_type?: string;
  url?: string;
}

export interface InspectorUpdateEvent {
  type: "inspector.update";
  target_kind: "message" | "tool_call" | "artifact" | "subagent" | "budget";
  target_id: string;
  payload: Record<string, unknown>;
}

export interface SkillActivatedEvent {
  type: "skill_activated";
  skill_name: string;
}

export interface SkillDeactivatedEvent {
  type: "skill_deactivated";
  skill_name: string;
}

export interface McpStatusEvent {
  type: "mcp_status";
  servers?: { name: string; status: string; tools?: number; tools_count?: number; transport?: string; error?: string; source?: string; priority?: number }[];
  data?: unknown;
}

export interface EnvListEvent {
  type: "env.list";
  entries?: { name: string; description: string; scope: string }[];
}

export interface GitPrStatusEvent {
  type: "git.pr_status";
  pr?: { number: number; title: string; state: string; url: string; branch: string } | null;
  checks?: { name: string; status: string; url: string }[];
  error?: string;
}

export interface SchedulerListEvent {
  type: "scheduler.list";
  tasks?: { id: string; name: string; prompt: string; schedule: string; permission_mode: string; enabled: boolean; last_run_at?: string | null; created_at?: string }[];
}

export interface ConnectorsMarketplaceListEvent {
  type: "connectors.marketplace.list";
  connectors?: { name: string; title: string; description: string; transport: string; command?: string; args?: string[]; url?: string; tags?: string[]; installed: boolean }[];
}

export interface FileChangedEvent {
  type: "file.changed";
  path: string;
  event: string;
}

export interface TerminalOutputEvent {
  type: "terminal.output";
  session_id?: string;
  data?: string;
  command?: string;
  output?: string;
  exit_code?: number;
}

export interface TerminalExitEvent {
  type: "terminal.exit";
  session_id: string;
  exit_code: number;
}

export interface TerminalCreatedEvent {
  type: "terminal.created";
  session_id: string;
  pid?: number;
  shell?: string;
  cwd?: string;
}

export interface TerminalKilledEvent {
  type: "terminal.killed";
  session_id: string;
}

export interface TerminalListEvent {
  type: "terminal.list";
  sessions: {
    session_id?: string;
    pid?: number;
    shell?: string;
    cwd?: string;
    is_alive?: boolean;
    started_at?: number;
  }[];
}

export interface BackgroundCompletedEvent {
  type: "background.completed";
  command_id: string;
  exit_code: number;
  status: string;
}

export interface CommandResultEvent {
  type: "command.result";
  command: string;
  level: string;
  message: string;
  title?: string;
  data?: Record<string, unknown>;
}

export interface SkillsListEvent {
  type: "skills.list";
  skills: {
    name: string;
    description: string;
    version?: string;
    triggers?: string[];
    tools_required?: string[];
    source_level?: string;
    active?: boolean;
  }[];
}

export interface SkillsMarketplaceListEvent {
  type: "skills.marketplace.list";
  skills: {
    name: string;
    title: string;
    description: string;
    triggers: string[];
    installed: boolean;
  }[];
}

export interface PreviewServerInfo {
  port: number;
  url: string;
  name: string;
  framework?: string;
}

export interface PreviewServersUpdatedEvent {
  type: "preview.servers.updated";
  servers: PreviewServerInfo[];
}

export interface PreviewServerDetectedEvent extends PreviewServerInfo {
  type: "preview.server.detected";
}

export interface PreviewServerStoppedEvent {
  type: "preview.server.stopped";
  port: number;
}

export interface PreviewNavigatedEvent {
  type: "preview.navigated";
  url: string;
}

export interface PreviewRefreshedEvent {
  type: "preview.refreshed";
  url?: string;
}

export interface PreviewLaunchConfigInfo {
  name: string;
  command: string;
  cwd: string;
  port: number;
  url: string;
  auto_port?: boolean;
  source?: string;
}

export interface PreviewLaunchProcessInfo extends PreviewLaunchConfigInfo {
  id: string;
  pid?: number;
  status: "starting" | "running" | "ready" | "exited" | "crashed";
  stderr_tail?: string[];
  output_tail?: PreviewServerOutputLine[];
}

export interface PreviewLaunchConfigEvent {
  type: "preview.launch.config";
  workspace_root?: string;
  configs: PreviewLaunchConfigInfo[];
  running?: PreviewLaunchProcessInfo[];
}

export interface PreviewLaunchStartedEvent extends PreviewLaunchProcessInfo {
  type: "preview.launch.started";
}

export interface PreviewLaunchStoppedEvent extends PreviewLaunchProcessInfo {
  type: "preview.launch.stopped";
}

export interface PreviewServerReadyEvent {
  type: "preview.server.ready";
  id: string;
  url: string;
  port: number;
}

export interface PreviewServerOutputLine {
  stream: "stdout" | "stderr";
  line: string;
  timestamp?: number;
}

export interface PreviewServerOutputEvent extends PreviewServerOutputLine {
  type: "preview.server.output";
  id: string;
}

export interface PreviewServerCrashedEvent {
  type: "preview.server.crashed";
  id: string;
  exit_code?: number | null;
  stderr_tail?: string[];
}

export interface PreviewServerUnhealthyEvent {
  type: "preview.server.unhealthy";
  id: string;
  url?: string;
  consecutive_failures?: number;
  last_error?: string;
}

export interface PreviewVerifiedEvent {
  type: "preview.verified";
  url: string;
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string;
}

// Catch-all for events we have not given a precise payload to yet (terminal
// admin, conversation lifecycle, session sync, control plane). They share the
// shape `{ type: ServerEventType, ...rest }`.
export interface UntypedServerEvent {
  type: Exclude<
    ServerEventType,
    | "text_chunk"
    | "thinking_delta"
    | "thinking"
    | "tool_call"
    | "tool_result"
    | "agent.progress"
    | "approval_request"
    | "approval.cancelled"
    | "ask_user"
    | "done"
    | "error"
    | "context_usage"
    | "context_compacted"
    | "budget_update"
    | "budget.warning"
    | "plan.update"
    | "task.update"
    | "subagent.start"
    | "subagent.event"
    | "subagent.done"
    | "citation.add"
    | "artifact.preview"
    | "inspector.update"
    | "skill_activated"
    | "skill_deactivated"
    | "mcp_status"
    | "env.list"
    | "git.pr_status"
    | "scheduler.list"
    | "file.changed"
    | "terminal.output"
    | "terminal.exit"
    | "terminal.created"
    | "terminal.killed"
    | "terminal.list"
    | "background.completed"
    | "command.result"
    | "skills.list"
    | "skills.marketplace.list"
    | "preview.servers.updated"
    | "preview.server.detected"
    | "preview.server.stopped"
    | "preview.navigated"
    | "preview.refreshed"
    | "preview.launch.config"
    | "preview.launch.started"
    | "preview.launch.stopped"
    | "preview.server.ready"
    | "preview.server.output"
    | "preview.server.crashed"
    | "preview.server.unhealthy"
    | "preview.verified"
  >;
  [key: string]: unknown;
}

export type ServerEvent =
  | TextChunkEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | AgentProgressEvent
  | ApprovalRequestEvent
  | ApprovalCancelledEvent
  | AskUserEvent
  | DoneEvent
  | ErrorEvent
  | ContextUsageEvent
  | ContextCompactedEvent
  | BudgetUpdateEvent
  | BudgetWarningEvent
  | PlanUpdateEvent
  | TaskUpdateEvent
  | SubagentStartEvent
  | SubagentEventEvent
  | SubagentDoneEvent
  | CitationAddEvent
  | ArtifactPreviewEvent
  | InspectorUpdateEvent
  | SkillActivatedEvent
  | SkillDeactivatedEvent
  | McpStatusEvent
  | EnvListEvent
  | GitPrStatusEvent
  | SchedulerListEvent
  | ConnectorsMarketplaceListEvent
  | FileChangedEvent
  | TerminalOutputEvent
  | TerminalExitEvent
  | TerminalCreatedEvent
  | TerminalKilledEvent
  | TerminalListEvent
  | BackgroundCompletedEvent
  | CommandResultEvent
  | SkillsListEvent
  | SkillsMarketplaceListEvent
  | PreviewServersUpdatedEvent
  | PreviewServerDetectedEvent
  | PreviewServerStoppedEvent
  | PreviewNavigatedEvent
  | PreviewRefreshedEvent
  | PreviewLaunchConfigEvent
  | PreviewLaunchStartedEvent
  | PreviewLaunchStoppedEvent
  | PreviewServerReadyEvent
  | PreviewServerOutputEvent
  | PreviewServerCrashedEvent
  | PreviewServerUnhealthyEvent
  | PreviewVerifiedEvent
  | UntypedServerEvent;

// ──────────────────────────────────────────────────────────────────
// Client command payloads
// ──────────────────────────────────────────────────────────────────

export interface UserMessageCommand {
  type: "user_message";
  content: string;
  conversation_id?: string;
  workspace_root?: string;
  permission_mode?: "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits";
  attachments?: Record<string, unknown>[];
}

export interface ApprovalCommand {
  type: "approval";
  tool_call_id: string;
  action: "approve" | "reject" | "partial";
  decisions?: Record<string, "approved" | "rejected">;
}

export interface AnswerCommand {
  type: "answer";
  tool_call_id: string;
  answer: string;
}

export interface InterruptCommand {
  type: "interrupt";
  conversation_id?: string;
}

export interface PingCommand {
  type: "ping";
}

export interface PlanEditCommand {
  type: "plan.edit";
  plan_id: string;
  steps?: PlanStep[];
  accept?: boolean;
  regenerate?: boolean;
  action?: "accept" | "reject";
}

export interface TaskEditCommand {
  type: "task.edit";
  todo_id: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
}

export interface SubagentCancelCommand {
  type: "subagent.cancel";
  subagent_id: string;
}

export interface InspectorFocusCommand {
  type: "inspector.focus";
  target_kind: "message" | "tool_call" | "artifact" | "subagent" | "budget";
  target_id: string;
}

export interface TerminalCreateCommand {
  type: "terminal.create";
  cwd?: string;
}

export interface TerminalInputCommand {
  type: "terminal.input";
  session_id: string;
  data: string;
}

export interface TerminalResizeCommand {
  type: "terminal.resize";
  session_id: string;
  cols: number;
  rows: number;
}

export interface TerminalKillCommand {
  type: "terminal.kill";
  session_id: string;
}

export interface TerminalExecCommand {
  type: "terminal.exec";
  command: string;
  cwd?: string;
}

export interface LlmModelSetCommand {
  type: "llm.model.set";
  model: string;
}

export interface LlmConfigSetCommand {
  type: "llm.config.set";
  provider: string;
  api_key?: string;
  base_url?: string;
  model?: string;
  [key: string]: unknown;
}

export interface SkillsListCommand {
  type: "skills.list";
}

export interface SkillsMarketplaceListCommand {
  type: "skills.marketplace.list";
}

export interface SkillsInstallCommand {
  type: "skills.install";
  name: string;
}

export interface ReadArtifactCommand {
  type: "read_artifact";
  artifact_id: string;
}

export interface ConversationCreateCommand {
  type: "conversation.create";
  conversation_id?: string;
  title?: string;
  side_chat?: boolean;
  git_isolated?: boolean;
  workspace_root?: string;
  permission_mode?: "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits";
}

export interface ConversationSwitchCommand {
  type: "conversation.switch";
  conversation_id: string;
}

export interface ConversationClearCommand {
  type: "conversation.clear";
  conversation_id?: string;
}

export interface ConversationDeleteCommand {
  type: "conversation.delete";
  conversation_id: string;
  cleanup_worktree?: boolean;
  force?: boolean;
}

export interface ConversationWorktreeCleanupCommand {
  type: "conversation.worktree.cleanup";
  conversation_id: string;
  force?: boolean;
}

export interface ConversationArchiveCommand {
  type: "conversation.archive" | "conversation.unarchive";
  conversation_id: string;
  archived?: boolean;
}

export interface ConversationRenameCommand {
  type: "conversation.rename";
  conversation_id: string;
  title: string;
}

export interface PreviewDetectCommand {
  type: "preview.detect";
}

export interface PreviewNavigateCommand {
  type: "preview.navigate";
  url: string;
}

export interface PreviewRefreshCommand {
  type: "preview.refresh";
  url?: string;
}

export interface PreviewLaunchConfigCommand {
  type: "preview.launch.config";
  workspace_root?: string;
}

export interface PreviewLaunchStartCommand {
  type: "preview.launch.start";
  name?: string;
  workspace_root?: string;
}

export interface PreviewLaunchStopCommand {
  type: "preview.launch.stop";
  name?: string;
}

export interface PreviewVerifyCommand {
  type: "preview.verify";
  url: string;
}

export interface UntypedClientCommand {
  type: Exclude<
    ClientCommandType,
    | "user_message"
    | "approval"
    | "answer"
    | "interrupt"
    | "ping"
    | "plan.edit"
    | "task.edit"
    | "subagent.cancel"
    | "inspector.focus"
    | "terminal.create"
    | "terminal.input"
    | "terminal.resize"
    | "terminal.kill"
    | "terminal.exec"
    | "llm.model.set"
    | "llm.config.set"
    | "skills.list"
    | "skills.marketplace.list"
    | "skills.install"
    | "read_artifact"
    | "conversation.create"
    | "conversation.switch"
    | "conversation.clear"
    | "conversation.delete"
    | "conversation.worktree.cleanup"
    | "conversation.archive"
    | "conversation.unarchive"
    | "conversation.rename"
    | "preview.detect"
    | "preview.navigate"
    | "preview.refresh"
    | "preview.launch.config"
    | "preview.launch.start"
    | "preview.launch.stop"
    | "preview.verify"
  >;
  [key: string]: unknown;
}

export type ClientCommand =
  | UserMessageCommand
  | ApprovalCommand
  | AnswerCommand
  | InterruptCommand
  | PingCommand
  | PlanEditCommand
  | TaskEditCommand
  | SubagentCancelCommand
  | InspectorFocusCommand
  | TerminalCreateCommand
  | TerminalInputCommand
  | TerminalResizeCommand
  | TerminalKillCommand
  | TerminalExecCommand
  | LlmModelSetCommand
  | LlmConfigSetCommand
  | SkillsListCommand
  | SkillsMarketplaceListCommand
  | SkillsInstallCommand
  | ReadArtifactCommand
  | ConversationCreateCommand
  | ConversationSwitchCommand
  | ConversationClearCommand
  | ConversationDeleteCommand
  | ConversationWorktreeCleanupCommand
  | ConversationArchiveCommand
  | ConversationRenameCommand
  | PreviewDetectCommand
  | PreviewNavigateCommand
  | PreviewRefreshCommand
  | PreviewLaunchConfigCommand
  | PreviewLaunchStartCommand
  | PreviewLaunchStopCommand
  | PreviewVerifyCommand
  | UntypedClientCommand;

// ──────────────────────────────────────────────────────────────────
// Frozen sets for runtime checks (mirror SERVER_EVENT_TYPES /
// CLIENT_COMMAND_TYPES on the backend)
// ──────────────────────────────────────────────────────────────────

export const SERVER_EVENT_TYPES: ReadonlySet<ServerEventType> = new Set<ServerEventType>([
  "text_chunk",
  "thinking_delta",
  "thinking",
  "tool_call",
  "tool_result",
  "agent.progress",
  "task.update",
  "approval_request",
  "approval.file_diff",
  "ask_user",
  "skill_activated",
  "skill_deactivated",
  "context_usage",
  "context_compacted",
  "budget_update",
  "budget.warning",
  "command.result",
  "command_output_chunk",
  "artifact_content",
  "artifact.preview",
  "done",
  "error",
  "mcp_status",
  "env.list",
  "git.pr_status",
  "scheduler.list",
  "connectors.marketplace.list",
  "checkpoint.list",
  "checkpoint.rewound",
  "file.changed",
  "terminal.output",
  "terminal.exit",
  "terminal.created",
  "terminal.killed",
  "terminal.list",
  "background.completed",
  "guidelines.updated",
  "permission.mode.updated",
  "permission.rules.updated",
  "conversation.hydration.updated",
  "conversation.compaction.updated",
  "conversation.summary.updated",
  "conversation.list",
  "conversation.switched",
  "llm.model.updated",
  "workspace.imported",
  "workspace.recent.list",
  "session.restored",
  "session.synced",
  "pong",
  "control_request",
  "plan.update",
  "subagent.start",
  "subagent.event",
  "subagent.done",
  "citation.add",
  "inspector.update",
  "skills.list",
  "skills.marketplace.list",
  "commands.list",
  "system_notice",
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
]);

export const CLIENT_COMMAND_TYPES: ReadonlySet<ClientCommandType> = new Set<ClientCommandType>([
  "user_message",
  "approval",
  "answer",
  "interrupt",
  "ping",
  "control_response",
  "control_cancel_request",
  "load_skill",
  "unload_skill",
  "read_artifact",
  "approval.file_diff",
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
  "conversation.worktree.cleanup",
  "session.tasks.inspect",
  "session.status.inspect",
  "session.usage.inspect",
  "session.permissions.inspect",
  "llm.model.set",
  "conversation.permission.rules.list",
  "conversation.permission.rules.add",
  "conversation.permission.rules.remove",
  "checkpoint.list",
  "checkpoint.rewind",
  "terminal.create",
  "terminal.input",
  "terminal.resize",
  "terminal.kill",
  "terminal.list",
  "workspace.import",
  "workspace.switch",
  "workspace.recent",
  "session.restore",
  "session.sync",
  "plan.edit",
  "task.edit",
  "task.stop",
  "subagent.cancel",
  "inspector.focus",
  "workspace.set",
  "terminal.exec",
  "skills.list",
  "skills.install",
  "skills.marketplace.list",
  "commands.list",
  "llm.config.set",
  "preview.detect",
  "preview.navigate",
  "preview.refresh",
  "preview.launch.config",
  "preview.launch.start",
  "preview.launch.stop",
  "preview.verify",
  "mcp.list",
  "mcp.add",
  "mcp.remove",
  "mcp.restart",
  "env.list",
  "env.set",
  "env.delete",
  "git.pr_status",
  "approval.respond",
  "scheduler.list",
  "scheduler.add",
  "scheduler.remove",
  "scheduler.toggle",
  "connectors.marketplace.list",
  "connectors.marketplace.install",
]);

export const isServerEvent = (t: string): t is ServerEventType =>
  SERVER_EVENT_TYPES.has(t as ServerEventType);

export const isClientCommand = (t: string): t is ClientCommandType =>
  CLIENT_COMMAND_TYPES.has(t as ClientCommandType);
