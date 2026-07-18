/**
 * WebSocket protocol — frontend mirror of backend/ws/events.py.
 *
 * Barrel file: re-exports all domain-grouped type files and composes the
 * master union types, runtime sets, and type guards.
 *
 * Domain files:
 *   - streaming-types.ts   — text streaming, thinking, tool call/result,
 *                            subagents, task/plan step tracking
 *   - conversation-types.ts — conversation lifecycle, context, budget, goals
 *   - preview-types.ts     — preview server lifecycle and health
 *   - terminal-types.ts    — terminal sessions and background commands
 *   - workspace-types.ts   — workspace, environment, git, file watcher
 *   - common-types.ts      — session, control plane, skills, MCP, scheduler
 *
 * Run `python scripts/check-protocol-sync.py` to verify the two stay aligned.
 */

// ──────────────────────────────────────────────────────────────────
// Re-export all domain types
// ──────────────────────────────────────────────────────────────────

export type {
  StreamingServerEventType,
  StreamingClientCommandType,
  TextChunkEvent,
  TextReplaceEvent,
  ThinkingDeltaEvent,
  ToolCallEvent,
  ToolErrorInfo,
  ToolResultEvent,
  ToolOutputDeltaEvent,
  AgentLoopEvent,
  AgentItemEvent,
  AgentProgressEvent,
  AgentRunStartedEvent,
  AgentRunUpdatedEvent,
  AgentRunCompletedEvent,
  AgentPhaseUpdatedEvent,
  VerificationStartedEvent,
  VerificationResultEvent,
  ApprovalRequestEvent,
  ApprovalFileDiffEvent,
  ApprovalCancelledEvent,
  AskUserEvent,
  DoneEvent,
  ErrorEvent,
  StreamResumeEvent,
  SubagentStartEvent,
  SubagentEventEvent,
  SubagentProgressEvent,
  SubagentDoneEvent,
  RuntimeSpanEvent,
  StreamEventEvent,
  RateLimitEvent,
  SessionStateEvent,
  ToolUseSummaryEvent,
  CitationAddEvent,
  ArtifactPreviewEvent,
  InspectorUpdateEvent,
  TodoTaskUpdateEvent,
  RuntimePendingApprovalSnapshot,
  RuntimeSessionSnapshot,
  SessionTaskUpdateEvent,
  TaskUpdateEvent,
  PlanStep,
  PlanStepUpdatedEvent,
  PlanUpdatedEvent,
  TaskEditCommand,
  PlanEditCommand,
  AgentResumeCommand,
  VerificationRunCommand,
  SubagentCancelCommand,
  InspectorFocusCommand,
} from "./streaming-types";

export type {
  ConversationServerEventType,
  ConversationClientCommandType,
  ContextUsageEvent,
  ContextCompactedEvent,
  BudgetUpdateEvent,
  BudgetWarningEvent,
  GoalInfo,
  GoalUpdatedEvent,
  ConversationTranscriptMessage,
  ConversationSummaryPayload,
  ConversationRecordPayload,
  ConversationListEvent,
  ConversationSwitchedEvent,
  LlmModelUpdatedEvent,
  UserMessageCommand,
  ApprovalCommand,
  AnswerCommand,
  InterruptCommand,
  PingCommand,
  ReadArtifactCommand,
  ConversationCreateCommand,
  ConversationSwitchCommand,
  ConversationClearCommand,
  ConversationDeleteCommand,
  ConversationWorktreeCleanupCommand,
  ConversationArchiveCommand,
  ConversationRenameCommand,
  ConversationGoalSetCommand,
  LlmModelSetCommand,
  LlmConfigSetCommand,
} from "./conversation-types";

export type {
  PreviewServerEventType,
  PreviewClientCommandType,
  PreviewServerInfo,
  PreviewLaunchConfigInfo,
  PreviewServerOutputLine,
  PreviewLaunchProcessInfo,
  PreviewServersUpdatedEvent,
  PreviewServerDetectedEvent,
  PreviewServerStoppedEvent,
  PreviewNavigatedEvent,
  PreviewRefreshedEvent,
  PreviewLaunchConfigEvent,
  PreviewLaunchStartedEvent,
  PreviewLaunchStoppedEvent,
  PreviewServerReadyEvent,
  PreviewServerOutputEvent,
  PreviewServerCrashedEvent,
  PreviewServerUnhealthyEvent,
  PreviewVerifiedEvent,
  PreviewDetectCommand,
  PreviewNavigateCommand,
  PreviewRefreshCommand,
  PreviewLaunchConfigCommand,
  PreviewLaunchStartCommand,
  PreviewLaunchStopCommand,
  PreviewVerifyCommand,
} from "./preview-types";

export type {
  TerminalServerEventType,
  TerminalClientCommandType,
  TerminalOutputEvent,
  TerminalExitEvent,
  TerminalCreatedEvent,
  TerminalKilledEvent,
  TerminalListEvent,
  TerminalSnapshotEvent,
  TerminalResizedEvent,
  BackgroundCompletedEvent,
  TerminalCreateCommand,
  TerminalInputCommand,
  TerminalResizeCommand,
  TerminalKillCommand,
  TerminalSnapshotRequestCommand,
  TerminalMirrorCreatedCommand,
  TerminalMirrorOutputCommand,
  TerminalMirrorExitCommand,
  TerminalExecCommand,
} from "./terminal-types";

export type {
  WorkspaceServerEventType,
  WorkspaceClientCommandType,
  FileChangedEvent,
  CommandResultEvent,
  EnvListEvent,
  GitPrStatusEvent,
  GitDiffFilePayload,
  GitDiffWorkingTreeEvent,
  GitDiffStagedEvent,
  GitDiffActionEvent,
  RunCheckpointRecord,
  RunCheckpointListEvent,
  RunCheckpointResumeEvent,
  EnvListCommand,
  EnvSetCommand,
  EnvDeleteCommand,
  RunCheckpointListCommand,
} from "./workspace-types";

export type {
  CommonServerEventType,
  CommonClientCommandType,
  SkillActivatedEvent,
  SkillDeactivatedEvent,
  McpStatusEvent,
  McpLifecycleEvent,
  McpProgressEvent,
  SchedulerListEvent,
  ConnectorsMarketplaceListEvent,
  SkillsListEvent,
  SkillsMarketplaceListEvent,
  RuntimeCapabilitiesEvent,
  ClientCommandAckEvent,
  SessionSnapshotPayload,
  SessionWorkspacePayload,
  SessionRestoredEvent,
  SessionSyncedEvent,
  SkillsListCommand,
  SkillsMarketplaceListCommand,
  SkillsInstallCommand,
  SchedulerListCommand,
  SchedulerAddCommand,
  SchedulerRemoveCommand,
  SchedulerToggleCommand,
  ConnectorsMarketplaceListCommand,
  ConnectorsMarketplaceInstallCommand,
  RuntimeCapabilitiesInspectCommand,
} from "./common-types";

// ──────────────────────────────────────────────────────────────────
// Server → Client event type union (mirror SERVER_EVENT_TYPES)
// Composed from domain-grouped subsets.
// ──────────────────────────────────────────────────────────────────

import type { StreamingServerEventType } from "./streaming-types";
import type { ConversationServerEventType } from "./conversation-types";
import type { PreviewServerEventType } from "./preview-types";
import type { TerminalServerEventType } from "./terminal-types";
import type { WorkspaceServerEventType } from "./workspace-types";
import type { CommonServerEventType } from "./common-types";

export type ServerEventType =
  | StreamingServerEventType
  | ConversationServerEventType
  | PreviewServerEventType
  | TerminalServerEventType
  | WorkspaceServerEventType
  | CommonServerEventType;

// ──────────────────────────────────────────────────────────────────
// Client → Server command type union (mirror CLIENT_COMMAND_TYPES)
// Composed from domain-grouped subsets.
// ──────────────────────────────────────────────────────────────────

import type { StreamingClientCommandType } from "./streaming-types";
import type { ConversationClientCommandType } from "./conversation-types";
import type { PreviewClientCommandType } from "./preview-types";
import type { TerminalClientCommandType } from "./terminal-types";
import type { WorkspaceClientCommandType } from "./workspace-types";
import type { CommonClientCommandType } from "./common-types";

export type ClientCommandType =
  | StreamingClientCommandType
  | ConversationClientCommandType
  | PreviewClientCommandType
  | TerminalClientCommandType
  | WorkspaceClientCommandType
  | CommonClientCommandType;

// ──────────────────────────────────────────────────────────────────
// Catch-all for events without precise payload types.
// ──────────────────────────────────────────────────────────────────

export interface UntypedServerEvent {
  type: Exclude<
    ServerEventType,
    | "text_chunk"
    | "text_replace"
    | "thinking_delta"
    | "thinking"
    | "tool_call"
    | "tool_result"
    | "tool_output_delta"
    | "agent.loop.started"
    | "agent.loop.completed"
    | "agent.run.started"
    | "agent.run.updated"
    | "agent.run.completed"
    | "agent.phase.updated"
    | "agent.item"
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
    | "plan_step_updated"
    | "plan_updated"
    | "task.update"
    | "subagent.start"
    | "subagent.event"
    | "subagent.progress"
    | "subagent.done"
    | "runtime.span"
    | "stream_event"
    | "rate_limit"
    | "session.state_changed"
    | "tool_use_summary"
    | "verification.started"
    | "verification.result"
    | "citation.add"
    | "artifact.preview"
    | "inspector.update"
    | "skill_activated"
    | "skill_deactivated"
    | "mcp_status"
    | "mcp.lifecycle"
    | "mcp.progress"
    | "env.list"
    | "git.pr_status"
    | "scheduler.list"
    | "file.changed"
    | "terminal.output"
    | "terminal.exit"
    | "terminal.created"
    | "terminal.killed"
    | "terminal.list"
    | "terminal.snapshot"
    | "terminal.resized"
    | "background.completed"
    | "command.result"
    | "goal.updated"
    | "conversation.list"
    | "conversation.switched"
    | "llm.model.updated"
    | "skills.list"
    | "skills.marketplace.list"
    | "session.restored"
    | "session.synced"
    | "runtime.capabilities"
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

export interface UntypedClientCommand {
  type: Exclude<
    ClientCommandType,
    | "user_message"
    | "approval"
    | "answer"
    | "interrupt"
    | "ping"
    | "task.edit"
    | "agent.resume"
    | "verification.run"
    | "subagent.cancel"
    | "inspector.focus"
    | "terminal.create"
    | "terminal.input"
    | "terminal.resize"
    | "terminal.kill"
    | "terminal.snapshot.request"
    | "terminal.mirror.created"
    | "terminal.mirror.output"
    | "terminal.mirror.exit"
    | "terminal.exec"
    | "llm.model.set"
    | "llm.config.set"
    | "runtime.capabilities.inspect"
    | "skills.list"
    | "skills.marketplace.list"
    | "skills.install"
    | "scheduler.list"
    | "scheduler.add"
    | "scheduler.remove"
    | "scheduler.toggle"
    | "connectors.marketplace.list"
    | "connectors.marketplace.install"
    | "env.list"
    | "env.set"
    | "env.delete"
    | "read_artifact"
    | "conversation.create"
    | "conversation.switch"
    | "conversation.clear"
    | "conversation.delete"
    | "conversation.worktree.cleanup"
    | "conversation.archive"
    | "conversation.unarchive"
    | "conversation.rename"
    | "conversation.goal.set"
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

// ──────────────────────────────────────────────────────────────────
// Discriminated unions of all typed payloads + catch-all
// ──────────────────────────────────────────────────────────────────

import type {
  TextChunkEvent,
  TextReplaceEvent,
  ThinkingDeltaEvent,
  ToolCallEvent,
  ToolResultEvent,
  ToolOutputDeltaEvent,
  AgentLoopEvent,
  AgentItemEvent,
  AgentProgressEvent,
  AgentRunStartedEvent,
  AgentRunUpdatedEvent,
  AgentRunCompletedEvent,
  AgentPhaseUpdatedEvent,
  VerificationStartedEvent,
  VerificationResultEvent,
  ApprovalRequestEvent,
  ApprovalFileDiffEvent,
  ApprovalCancelledEvent,
  AskUserEvent,
  DoneEvent,
  ErrorEvent,
  StreamResumeEvent,
  PlanStepUpdatedEvent,
  PlanUpdatedEvent,
  TaskUpdateEvent,
  SubagentStartEvent,
  SubagentEventEvent,
  SubagentProgressEvent,
  SubagentDoneEvent,
  RuntimeSpanEvent,
  StreamEventEvent,
  RateLimitEvent,
  SessionStateEvent,
  ToolUseSummaryEvent,
  CitationAddEvent,
  ArtifactPreviewEvent,
  InspectorUpdateEvent,
} from "./streaming-types";

import type {
  ContextUsageEvent,
  ContextCompactedEvent,
  BudgetUpdateEvent,
  BudgetWarningEvent,
  GoalUpdatedEvent,
  ConversationListEvent,
  ConversationSwitchedEvent,
  LlmModelUpdatedEvent,
} from "./conversation-types";

import type {
  PreviewServersUpdatedEvent,
  PreviewServerDetectedEvent,
  PreviewServerStoppedEvent,
  PreviewNavigatedEvent,
  PreviewRefreshedEvent,
  PreviewLaunchConfigEvent,
  PreviewLaunchStartedEvent,
  PreviewLaunchStoppedEvent,
  PreviewServerReadyEvent,
  PreviewServerOutputEvent,
  PreviewServerCrashedEvent,
  PreviewServerUnhealthyEvent,
  PreviewVerifiedEvent,
} from "./preview-types";

import type {
  TerminalOutputEvent,
  TerminalExitEvent,
  TerminalCreatedEvent,
  TerminalKilledEvent,
  TerminalListEvent,
  TerminalSnapshotEvent,
  TerminalResizedEvent,
  BackgroundCompletedEvent,
} from "./terminal-types";

import type {
  FileChangedEvent,
  CommandResultEvent,
  EnvListEvent,
  GitPrStatusEvent,
  GitDiffWorkingTreeEvent,
  GitDiffStagedEvent,
  GitDiffActionEvent,
  RunCheckpointListEvent,
  RunCheckpointResumeEvent,
} from "./workspace-types";

import type {
  SkillActivatedEvent,
  SkillDeactivatedEvent,
  McpStatusEvent,
  McpLifecycleEvent,
  McpProgressEvent,
  SchedulerListEvent,
  ConnectorsMarketplaceListEvent,
  SkillsListEvent,
  SkillsMarketplaceListEvent,
  RuntimeCapabilitiesEvent,
  ClientCommandAckEvent,
  SessionRestoredEvent,
  SessionSyncedEvent,
} from "./common-types";

export interface ServerEventEnvelope {
  seq?: number;
  event_id?: string;
  timestamp?: string;
}

type ServerEventPayload =
  | TextChunkEvent
  | TextReplaceEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ToolOutputDeltaEvent
  | AgentLoopEvent
  | AgentItemEvent
  | AgentProgressEvent
  | AgentRunStartedEvent
  | AgentRunUpdatedEvent
  | AgentRunCompletedEvent
  | AgentPhaseUpdatedEvent
  | VerificationStartedEvent
  | VerificationResultEvent
  | ApprovalRequestEvent
  | ApprovalFileDiffEvent
  | ApprovalCancelledEvent
  | AskUserEvent
  | DoneEvent
  | ErrorEvent
  | ContextUsageEvent
  | ContextCompactedEvent
  | BudgetUpdateEvent
  | BudgetWarningEvent
  | PlanStepUpdatedEvent
  | PlanUpdatedEvent
  | TaskUpdateEvent
  | SubagentStartEvent
  | SubagentEventEvent
  | SubagentProgressEvent
  | SubagentDoneEvent
  | RuntimeSpanEvent
  | StreamEventEvent
  | RateLimitEvent
  | SessionStateEvent
  | ToolUseSummaryEvent
  | CitationAddEvent
  | ArtifactPreviewEvent
  | InspectorUpdateEvent
  | SkillActivatedEvent
  | SkillDeactivatedEvent
  | McpStatusEvent
  | McpLifecycleEvent
  | McpProgressEvent
  | EnvListEvent
  | GitPrStatusEvent
  | GitDiffWorkingTreeEvent
  | GitDiffStagedEvent
  | GitDiffActionEvent
  | RunCheckpointListEvent
  | RunCheckpointResumeEvent
  | SchedulerListEvent
  | ConnectorsMarketplaceListEvent
  | FileChangedEvent
  | TerminalOutputEvent
  | TerminalExitEvent
  | TerminalCreatedEvent
  | TerminalKilledEvent
  | TerminalListEvent
  | TerminalSnapshotEvent
  | TerminalResizedEvent
  | BackgroundCompletedEvent
  | CommandResultEvent
  | GoalUpdatedEvent
  | ConversationListEvent
  | ConversationSwitchedEvent
  | LlmModelUpdatedEvent
  | SkillsListEvent
  | SkillsMarketplaceListEvent
  | RuntimeCapabilitiesEvent
  | ClientCommandAckEvent
  | SessionRestoredEvent
  | SessionSyncedEvent
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
  | StreamResumeEvent
  | UntypedServerEvent;

export type ServerEvent = ServerEventPayload & ServerEventEnvelope;

import type {
  UserMessageCommand,
  ApprovalCommand,
  AnswerCommand,
  InterruptCommand,
  PingCommand,
  ReadArtifactCommand,
  ConversationCreateCommand,
  ConversationSwitchCommand,
  ConversationClearCommand,
  ConversationDeleteCommand,
  ConversationWorktreeCleanupCommand,
  ConversationArchiveCommand,
  ConversationRenameCommand,
  ConversationGoalSetCommand,
  LlmModelSetCommand,
  LlmConfigSetCommand,
} from "./conversation-types";

import type {
  TaskEditCommand,
  PlanEditCommand,
  AgentResumeCommand,
  VerificationRunCommand,
  SubagentCancelCommand,
  InspectorFocusCommand,
} from "./streaming-types";

import type {
  TerminalCreateCommand,
  TerminalInputCommand,
  TerminalResizeCommand,
  TerminalKillCommand,
  TerminalSnapshotRequestCommand,
  TerminalMirrorCreatedCommand,
  TerminalMirrorOutputCommand,
  TerminalMirrorExitCommand,
  TerminalExecCommand,
} from "./terminal-types";

import type {
  PreviewDetectCommand,
  PreviewNavigateCommand,
  PreviewRefreshCommand,
  PreviewLaunchConfigCommand,
  PreviewLaunchStartCommand,
  PreviewLaunchStopCommand,
  PreviewVerifyCommand,
} from "./preview-types";

import type {
  SkillsListCommand,
  SkillsMarketplaceListCommand,
  SkillsInstallCommand,
  SchedulerListCommand,
  SchedulerAddCommand,
  SchedulerRemoveCommand,
  SchedulerToggleCommand,
  ConnectorsMarketplaceListCommand,
  ConnectorsMarketplaceInstallCommand,
  RuntimeCapabilitiesInspectCommand,
} from "./common-types";

import type {
  EnvListCommand,
  EnvSetCommand,
  EnvDeleteCommand,
  RunCheckpointListCommand,
} from "./workspace-types";

export interface ClientCommandEnvelope {
  client_command_id?: string;
}

type ClientCommandPayload =
  | UserMessageCommand
  | ApprovalCommand
  | AnswerCommand
  | InterruptCommand
  | PingCommand
  | TaskEditCommand
  | PlanEditCommand
  | AgentResumeCommand
  | VerificationRunCommand
  | SubagentCancelCommand
  | InspectorFocusCommand
  | TerminalCreateCommand
  | TerminalInputCommand
  | TerminalResizeCommand
  | TerminalKillCommand
  | TerminalSnapshotRequestCommand
  | TerminalMirrorCreatedCommand
  | TerminalMirrorOutputCommand
  | TerminalMirrorExitCommand
  | TerminalExecCommand
  | LlmModelSetCommand
  | LlmConfigSetCommand
  | SkillsListCommand
  | SkillsMarketplaceListCommand
  | SkillsInstallCommand
  | SchedulerListCommand
  | SchedulerAddCommand
  | SchedulerRemoveCommand
  | SchedulerToggleCommand
  | ConnectorsMarketplaceListCommand
  | ConnectorsMarketplaceInstallCommand
  | RuntimeCapabilitiesInspectCommand
  | EnvListCommand
  | EnvSetCommand
  | EnvDeleteCommand
  | RunCheckpointListCommand
  | ReadArtifactCommand
  | ConversationCreateCommand
  | ConversationSwitchCommand
  | ConversationClearCommand
  | ConversationDeleteCommand
  | ConversationWorktreeCleanupCommand
  | ConversationArchiveCommand
  | ConversationRenameCommand
  | ConversationGoalSetCommand
  | PreviewDetectCommand
  | PreviewNavigateCommand
  | PreviewRefreshCommand
  | PreviewLaunchConfigCommand
  | PreviewLaunchStartCommand
  | PreviewLaunchStopCommand
  | PreviewVerifyCommand
  | UntypedClientCommand;

export type ClientCommand = ClientCommandPayload & ClientCommandEnvelope;

// ──────────────────────────────────────────────────────────────────
// Frozen sets for runtime checks (mirror SERVER_EVENT_TYPES /
// CLIENT_COMMAND_TYPES on the backend)
// ──────────────────────────────────────────────────────────────────

export const SERVER_EVENT_TYPES: ReadonlySet<ServerEventType> = new Set<ServerEventType>([
  // Streaming text + tool execution
  "text_chunk",
  "text_replace",
  "image_chunk",
  "thinking_delta",
  "thinking",
  "tool_call",
  "tool_result",
  "tool_output_delta",
  "agent.loop.started",
  "agent.loop.completed",
  "agent.run.started",
  "agent.run.updated",
  "agent.run.completed",
  "agent.phase.updated",
  "agent.item",
  "agent.progress",
  "task.update",
  "approval_request",
  "approval.cancelled",
  "approval.file_diff",
  "ask_user",
  "done",
  "error",
  "stream_resume",
  // Subagents + citations + inspector
  "subagent.start",
  "subagent.event",
  "subagent.progress",
  "subagent.done",
  "verification.started",
  "verification.result",
  "runtime.span",
  "stream_event",
  "rate_limit",
  "session.state_changed",
  "tool_use_summary",
  "citation.add",
  "inspector.update",
  "plan_step_updated",
  "plan_updated",
  // Context lifecycle
  "context_usage",
  "context_compacted",
  "budget_update",
  "budget.warning",
  // Conversation runtime
  "conversation.hydration.updated",
  "conversation.compaction.updated",
  "conversation.summary.updated",
  "goal.updated",
  "conversation.list",
  "conversation.switched",
  // LLM settings
  "llm.model.updated",
  // Preview
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
  // Terminal
  "terminal.output",
  "terminal.exit",
  "terminal.created",
  "terminal.killed",
  "terminal.list",
  "terminal.snapshot",
  "terminal.resized",
  "background.completed",
  // Workspace / file watcher
  "file.changed",
  "command.result",
  "command_output_chunk",
  "artifact_content",
  "artifact.preview",
  "workspace.imported",
  "workspace.recent.list",
  "env.list",
  "git.pr_status",
  "checkpoint.list",
  "checkpoint.rewound",
  "checkpoint.run.list",
  "checkpoint.run.resume",
  "guidelines.updated",
  "permission.mode.updated",
  "permission.rules.updated",
  // Common / infrastructure
  "skill_activated",
  "skill_deactivated",
  "mcp_status",
  "mcp.lifecycle",
  "mcp.progress",
  "scheduler.list",
  "connectors.marketplace.list",
  "session.restored",
  "session.synced",
  "runtime.capabilities",
  "client.command.ack",
  "pong",
  "control_request",
  "skills.list",
  "skills.marketplace.list",
  "commands.list",
  "system_notice",
  // Git diff
  "diff.git_working_tree",
  "diff.git_staged",
  "diff.git_stage_file",
  "diff.git_unstage_file",
  "diff.git_stage_all",
  "diff.git_unstage_all",
  "diff.git_revert_file",
]);

export const CLIENT_COMMAND_TYPES: ReadonlySet<ClientCommandType> = new Set<ClientCommandType>([
  // Chat
  "user_message",
  "approval",
  "answer",
  "interrupt",
  "ping",
  // Control plane
  "control_response",
  "control_cancel_request",
  // Skills
  "load_skill",
  "unload_skill",
  "read_artifact",
  "approval.file_diff",
  // Conversation lifecycle
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
  "conversation.permission.rules.list",
  "conversation.permission.rules.add",
  "conversation.permission.rules.remove",
  "permissions.content_rule.add",
  // Session inspection
  "session.tasks.inspect",
  "session.status.inspect",
  "session.usage.inspect",
  "session.permissions.inspect",
  "runtime.capabilities.inspect",
  // LLM
  "llm.model.set",
  "llm.config.set",
  // Checkpoints
  "checkpoint.list",
  "checkpoint.rewind",
  "checkpoint.run.list",
  // Terminal
  "terminal.create",
  "terminal.input",
  "terminal.resize",
  "terminal.kill",
  "terminal.list",
  "terminal.snapshot.request",
  "terminal.mirror.created",
  "terminal.mirror.output",
  "terminal.mirror.exit",
  "terminal.exec",
  // Workspace
  "workspace.import",
  "workspace.switch",
  "workspace.recent",
  "workspace.set",
  // Session restore / sync
  "session.restore",
  "session.sync",
  // Streaming / task management
  "task.edit",
  "plan.edit",
  "task.stop",
  "agent.resume",
  "verification.run",
  "subagent.cancel",
  "subagent.status",
  "inspector.focus",
  // Skills / commands catalog
  "skills.list",
  "skills.install",
  "skills.marketplace.list",
  "commands.list",
  // Preview
  "preview.detect",
  "preview.navigate",
  "preview.refresh",
  "preview.launch.config",
  "preview.launch.start",
  "preview.launch.stop",
  "preview.verify",
  // Git diff
  "diff.git_working_tree",
  "diff.git_staged",
  "diff.git_stage_file",
  "diff.git_unstage_file",
  "diff.git_stage_all",
  "diff.git_unstage_all",
  "diff.git_revert_file",
  // MCP / Environment / Git status
  "mcp.list",
  "mcp.add",
  "mcp.remove",
  "mcp.restart",
  "env.list",
  "env.set",
  "env.delete",
  "git.pr_status",
  "approval.respond",
  // Scheduler
  "scheduler.list",
  "scheduler.add",
  "scheduler.remove",
  "scheduler.toggle",
  // Connectors marketplace
  "connectors.marketplace.list",
  "connectors.marketplace.install",
]);

export const isServerEvent = (t: string): t is ServerEventType =>
  SERVER_EVENT_TYPES.has(t as ServerEventType);

export const isClientCommand = (t: string): t is ClientCommandType =>
  CLIENT_COMMAND_TYPES.has(t as ClientCommandType);
