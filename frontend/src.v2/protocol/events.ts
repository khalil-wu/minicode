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
  ItemStartedEvent,
  AgentMessageDeltaEvent,
  ItemCompletedEvent,
  ImageChunkLiveEvent,
  ImageChunkReplayEvent,
  ImageChunkEvent,
  ThinkingDeltaEvent,
  ToolCallEvent,
  ToolErrorInfo,
  ToolResultEvent,
  ToolOutputDeltaEvent,
  CommandOutputChunkEvent,
  AgentItemEvent,
  AgentProgressEvent,
  RuntimeSpanEvent,
  AgentRunStartedEvent,
  AgentRunCompletedEvent,
  ApprovalRequestEvent,
  PermissionDecisionEvent,
    ApprovalFileDiffEvent,
  ApprovalCancelledEvent,
  AskUserEvent,
  DoneEvent,
  ErrorEvent,
  StreamResumeEvent,
  StreamEventEvent,
  RateLimitEvent,
  SessionStateEvent,
  SubagentStartEvent,
  SubagentEventEvent,
  SubagentMailboxEvent,
  SubagentProgressEvent,
  SubagentDoneEvent,
  SubagentPlanApprovalRequestedEvent,
  ParentNotificationsEvent,
  CitationAddEvent,
  ArtifactPreviewEvent,
  InspectorUpdateEvent,
  TodoTaskUpdateEvent,
  RuntimePendingApprovalSnapshot,
  RuntimeSessionSnapshot,
  SessionTaskUpdateEvent,
  TaskUpdateEvent,
  TurnPlanStep,
  TurnPlanUpdatedEvent,
  TurnDiffUpdatedEvent,
  AgentResumeCommand,
  SubagentCancelCommand,
  SubagentStatusCommand,
  SubagentTranscriptCommand,
  SubagentPlanReviewCommand,
  SendMessageCommand,
  InspectorFocusCommand,
} from "./streaming-types";

export type {
  ConversationServerEventType,
  ConversationClientCommandType,
  ContextUsageEvent,
  ContextCompactedEvent,
  ContextForkedEvent,
  ContextLedgerCategoryPayload,
  ContextLedgerEntryPayload,
  ContextLedgerEvent,
  ContextSideQueryResultEvent,
  BudgetUpdateEvent,
  BudgetWarningEvent,
  GoalInfo,
  GoalUpdatedEvent,
  ConversationHydrationUpdatedEvent,
  ConversationCompactionUpdatedEvent,
  ConversationSummaryUpdatedEvent,
  ConversationTranscriptMessage,
  ConversationSummaryPayload,
  ConversationRecordPayload,
  ConversationListEvent,
  ConversationSwitchedEvent,
  LlmModelUpdatedEvent,
  UserMessageQueueUpdatedEvent,
  UserMessageCommand,
  UserMessageQueueCancelCommand,
  UserMessageQueueSteerCommand,
  InterruptCommand,
  PingCommand,
  ReadArtifactCommand,
  ConversationCreateCommand,
  ConversationCloneCommand,
  ConversationMergeCommand,
  ConversationExportCommand,
  ConversationSwitchCommand,
  ConversationClearCommand,
  ConversationTruncateCommand,
  ContextForkCommand,
  ConversationDeleteCommand,
  ConversationWorktreeCleanupCommand,
  ConversationWorktreeHandoffPreflightCommand,
  ConversationWorktreeHandoffExecuteCommand,
  ConversationArchiveCommand,
  ConversationRenameCommand,
  ConversationMemoryModeSetCommand,
  MemoryResetCommand,
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
  BackgroundStartedEvent,
  BackgroundStalledEvent,
  BackgroundCompletedEvent,
  TerminalCreateCommand,
  TerminalListCommand,
  TerminalInputCommand,
  TerminalResizeCommand,
  TerminalKillCommand,
  TerminalRestartCommand,
  TerminalSnapshotRequestCommand,
  TerminalClearCommand,
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
  ArtifactContentEvent,
  EnvListEvent,
  GitPrStatusEvent,
  GitDiffFilePayload,
  GitDiffWorkingTreeEvent,
  GitDiffStagedEvent,
  GitDiffActionEvent,
  WorkspaceRecentProjectPayload,
  WorkspaceRecentListEvent,
  WorkspaceProjectPayload,
  WorkspaceImportedEvent,
  CheckpointRecordPayload,
  CheckpointCreatedEvent,
  CheckpointListEvent,
  CheckpointRewoundEvent,
  RunCheckpointRecord,
  RunCheckpointListEvent,
  RunCheckpointResumeEvent,
  GuidelinesUpdatedEvent,
  PermissionModeUpdatedEvent,
  PermissionRulePayload,
  PermissionRulesPayload,
  PermissionRulesUpdatedEvent,
  EnvListCommand,
  EnvSetCommand,
  EnvDeleteCommand,
  RunCheckpointListCommand,
} from "./workspace-types";

export type {
  CommonServerEventType,
  CommonClientCommandType,
  McpTransport,
  McpEnvVarReference,
  McpServerMutationPayload,
  McpAddCommand,
  McpUpdateCommand,
  McpInventoryListCommand,
  McpInventoryCancelCommand,
  McpInventoryPayload,
  McpStatusEvent,
  McpLifecycleEvent,
  McpProgressEvent,
  SchedulerListEvent,
  SkillsListEvent,
  SkillsMarketplaceListEvent,
  CommandAvailabilityPayload,
  CommandArgumentPayload,
  CommandCatalogEntryPayload,
  CommandsListEvent,
  CheckpointOriginPayload,
  SystemNoticeEvent,
  PongEvent,
  RuntimeCapabilitiesEvent,
  ClientCommandAckEvent,
  ProviderOAuthInfoLink,
  ProviderOAuthAuthEvent,
  ProviderOAuthDeviceCodeEvent,
  ProviderOAuthInfoEvent,
  ProviderOAuthProgressEvent,
  ControlCanUseToolRequest,
  ControlElicitationRequest,
  ControlProviderAuthPromptRequest,
  ControlRequestPayload,
  ControlRequestEvent,
  ControlResponseCommand,
  ControlCancelRequestCommand,
  SessionSnapshotPayload,
  SessionWorkspacePayload,
  SessionRestoredEvent,
  SessionReplayEvent,
  SessionSyncedEvent,
  SkillsListCommand,
  SkillsMarketplaceListCommand,
  SkillsInstallCommand,
  SchedulerListCommand,
  SchedulerAddCommand,
  SchedulerRemoveCommand,
  SchedulerToggleCommand,
  SchedulerRunNowCommand,
  SchedulerRetryCommand,
  SchedulerCancelCommand,
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
    | "item.started"
    | "agent_message.delta"
    | "item.completed"
    | "image_chunk"
    | "thinking_delta"
    | "thinking"
    | "tool_call"
    | "tool_result"
    | "tool_output_delta"
    | "command_output_chunk"
    | "agent.run.started"
    | "agent.run.completed"
    | "user_message.queue.updated"
    | "agent.item"
    | "agent.progress"
    | "runtime.span"
      | "permission.decision"
    | "approval.cancelled"
    | "approval.file_diff"
      | "done"
    | "error"
    | "stream_event"
    | "rate_limit"
    | "session.state_changed"
    | "context_usage"
    | "context_compacted"
    | "context_forked"
    | "context_ledger"
    | "context_side_query_result"
    | "budget_update"
    | "budget.warning"
    | "turn.plan.updated"
    | "turn.diff.updated"
    | "task.update"
    | "subagent.start"
    | "subagent.event"
    | "subagent.mailbox"
    | "subagent.progress"
    | "subagent.done"
    | "parent.notifications"
    | "citation.add"
    | "artifact.preview"
    | "inspector.update"
    | "mcp_status"
    | "mcp.lifecycle"
    | "mcp.progress"
    | "env.list"
    | "git.pr_status"
    | "diff.git_working_tree"
    | "diff.git_staged"
    | "diff.git_stage_file"
    | "diff.git_unstage_file"
    | "diff.git_stage_all"
    | "diff.git_unstage_all"
    | "diff.git_revert_file"
    | "scheduler.list"
    | "workspace.recent.list"
    | "workspace.imported"
    | "checkpoint.created"
    | "checkpoint.list"
    | "checkpoint.rewound"
    | "checkpoint.run.list"
    | "checkpoint.run.resume"
    | "guidelines.updated"
    | "permission.mode.updated"
    | "permission.rules.updated"
    | "file.changed"
    | "terminal.output"
    | "terminal.exit"
    | "terminal.created"
    | "terminal.killed"
    | "terminal.list"
    | "terminal.snapshot"
    | "terminal.resized"
    | "background.started"
    | "background.stalled"
    | "background.completed"
    | "command.result"
    | "artifact_content"
    | "goal.updated"
    | "conversation.hydration.updated"
    | "conversation.compaction.updated"
    | "conversation.summary.updated"
    | "conversation.list"
    | "conversation.switched"
    | "llm.model.updated"
    | "skills.list"
    | "skills.marketplace.list"
    | "commands.list"
    | "system_notice"
    | "client.command.ack"
    | "session.restored"
    | "session.replay"
    | "session.synced"
    | "runtime.capabilities"
    | "pong"
    | "control_request"
    | "llm.provider.oauth.auth"
    | "llm.provider.oauth.device_code"
    | "llm.provider.oauth.info"
    | "llm.provider.oauth.progress"
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
    | "stream_resume"
  >;
  [key: string]: unknown;
}

export interface UntypedClientCommand {
  type: Exclude<
    ClientCommandType,
    | "user_message"
    | "user_message.queue.cancel"
    | "user_message.queue.steer"
    | "interrupt"
    | "ping"
    | "control_response"
    | "control_cancel_request"
    | "agent.resume"
    | "subagent.cancel"
    | "subagent.status"
    | "subagent.transcript"
    | "subagent.plan_review"
    | "send_message"
    | "inspector.focus"
    | "terminal.create"
    | "terminal.list"
    | "terminal.input"
    | "terminal.resize"
    | "terminal.kill"
    | "terminal.restart"
    | "terminal.snapshot.request"
    | "terminal.clear"
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
  | "scheduler.run_now"
    | "scheduler.retry"
    | "scheduler.cancel"
    | "mcp.add"
    | "mcp.update"
    | "mcp.project.approve"
    | "mcp.project.approve_all"
    | "mcp.project.reject"
    | "env.list"
    | "env.set"
    | "env.delete"
    | "read_artifact"
    | "conversation.create"
    | "conversation.clone"
    | "conversation.merge"
    | "conversation.export"
    | "conversation.switch"
    | "conversation.clear"
    | "conversation.truncate"
    | "context.fork"
    | "conversation.delete"
    | "conversation.worktree.cleanup"
    | "conversation.worktree.handoff.preflight"
    | "conversation.worktree.handoff.execute"
    | "conversation.archive"
    | "conversation.unarchive"
    | "conversation.rename"
    | "conversation.memory_mode.set"
    | "memory.reset"
    | "conversation.goal.set"
    | "preview.detect"
    | "preview.navigate"
    | "preview.refresh"
    | "preview.launch.config"
    | "preview.launch.start"
    | "preview.launch.stop"
    | "preview.verify"
    | "checkpoint.list"
    | "checkpoint.rewind"
    | "checkpoint.run.list"
  >;
  [key: string]: unknown;
}

export type ProviderOAuthCommand = {
  type: "llm.provider.oauth.login" | "llm.provider.oauth.logout" | "llm.provider.oauth.status";
  provider: string;
  conversation_id?: string;
  client_command_id?: string;
};

// ──────────────────────────────────────────────────────────────────
// Discriminated unions of all typed payloads + catch-all
// ──────────────────────────────────────────────────────────────────

import type {
  ItemStartedEvent,
  AgentMessageDeltaEvent,
  ItemCompletedEvent,
  ImageChunkEvent,
  ThinkingDeltaEvent,
  ToolCallEvent,
  ToolResultEvent,
  ToolOutputDeltaEvent,
  CommandOutputChunkEvent,
  AgentItemEvent,
  AgentProgressEvent,
  RuntimeSpanEvent,
  AgentRunStartedEvent,
  AgentRunCompletedEvent,
  ApprovalRequestEvent,
  PermissionDecisionEvent,
  ApprovalFileDiffEvent,
  ApprovalCancelledEvent,
  AskUserEvent,
  DoneEvent,
  ErrorEvent,
  StreamResumeEvent,
  StreamEventEvent,
  RateLimitEvent,
  SessionStateEvent,
  TurnPlanUpdatedEvent,
  TurnDiffUpdatedEvent,
  TaskUpdateEvent,
  SubagentStartEvent,
  SubagentEventEvent,
  SubagentMailboxEvent,
  SubagentProgressEvent,
  SubagentDoneEvent,
  SubagentPlanApprovalRequestedEvent,
  ParentNotificationsEvent,
  CitationAddEvent,
  ArtifactPreviewEvent,
  InspectorUpdateEvent,
} from "./streaming-types";

import type {
  ContextUsageEvent,
  ContextCompactedEvent,
  ContextForkedEvent,
  ContextLedgerEvent,
  ContextSideQueryResultEvent,
  BudgetUpdateEvent,
  BudgetWarningEvent,
  GoalUpdatedEvent,
  ConversationHydrationUpdatedEvent,
  ConversationCompactionUpdatedEvent,
  ConversationSummaryUpdatedEvent,
  ConversationListEvent,
  ConversationSwitchedEvent,
  LlmModelUpdatedEvent,
  UserMessageQueueUpdatedEvent,
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
  BackgroundStartedEvent,
  BackgroundStalledEvent,
  BackgroundCompletedEvent,
} from "./terminal-types";

import type {
  FileChangedEvent,
  CommandResultEvent,
  ArtifactContentEvent,
  EnvListEvent,
  GitPrStatusEvent,
  GitDiffWorkingTreeEvent,
  GitDiffStagedEvent,
  GitDiffActionEvent,
  WorkspaceRecentListEvent,
  WorkspaceImportedEvent,
  CheckpointCreatedEvent,
  CheckpointListEvent,
  CheckpointRewoundEvent,
  RunCheckpointListEvent,
  RunCheckpointResumeEvent,
  GuidelinesUpdatedEvent,
  PermissionModeUpdatedEvent,
  PermissionRulesUpdatedEvent,
} from "./workspace-types";

import type {
  McpStatusEvent,
  McpLifecycleEvent,
  McpProgressEvent,
  SchedulerListEvent,
  SkillsListEvent,
  SkillsMarketplaceListEvent,
  CommandsListEvent,
  SystemNoticeEvent,
  PongEvent,
  RuntimeCapabilitiesEvent,
  ClientCommandAckEvent,
  ControlRequestEvent,
  ProviderOAuthAuthEvent,
  ProviderOAuthDeviceCodeEvent,
  ProviderOAuthInfoEvent,
  ProviderOAuthProgressEvent,
  SessionRestoredEvent,
  SessionReplayEvent,
  SessionSyncedEvent,
} from "./common-types";

export interface ServerEventEnvelope {
  seq?: number;
  previous_replay_seq?: number;
  event_id?: string;
  timestamp?: string;
  task_id?: string;
  turn_id?: string;
  client_command_id?: string;
  client_command_type?: string;
}

type ServerEventPayload =
  | ItemStartedEvent
  | AgentMessageDeltaEvent
  | ItemCompletedEvent
  | ImageChunkEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ToolOutputDeltaEvent
  | CommandOutputChunkEvent
  | AgentItemEvent
  | AgentProgressEvent
  | RuntimeSpanEvent
  | AgentRunStartedEvent
  | AgentRunCompletedEvent
  | ApprovalRequestEvent
  | PermissionDecisionEvent
  | ApprovalFileDiffEvent
  | ApprovalCancelledEvent
  | AskUserEvent
  | DoneEvent
  | ErrorEvent
  | StreamEventEvent
  | RateLimitEvent
  | SessionStateEvent
  | ContextUsageEvent
  | ContextCompactedEvent
  | ContextForkedEvent
  | ContextLedgerEvent
  | ContextSideQueryResultEvent
  | BudgetUpdateEvent
  | BudgetWarningEvent
  | TurnPlanUpdatedEvent
  | TurnDiffUpdatedEvent
  | TaskUpdateEvent
  | SubagentStartEvent
  | SubagentEventEvent
  | SubagentMailboxEvent
  | SubagentProgressEvent
  | SubagentDoneEvent
  | SubagentPlanApprovalRequestedEvent
  | ParentNotificationsEvent
  | CitationAddEvent
  | ArtifactPreviewEvent
  | InspectorUpdateEvent
  | McpStatusEvent
  | McpLifecycleEvent
  | McpProgressEvent
  | EnvListEvent
  | GitPrStatusEvent
  | GitDiffWorkingTreeEvent
  | GitDiffStagedEvent
  | GitDiffActionEvent
  | WorkspaceRecentListEvent
  | WorkspaceImportedEvent
  | CheckpointCreatedEvent
  | CheckpointListEvent
  | CheckpointRewoundEvent
  | RunCheckpointListEvent
  | RunCheckpointResumeEvent
  | GuidelinesUpdatedEvent
  | PermissionModeUpdatedEvent
  | PermissionRulesUpdatedEvent
  | SchedulerListEvent
  | FileChangedEvent
  | TerminalOutputEvent
  | TerminalExitEvent
  | TerminalCreatedEvent
  | TerminalKilledEvent
  | TerminalListEvent
  | TerminalSnapshotEvent
  | TerminalResizedEvent
  | BackgroundStartedEvent
  | BackgroundStalledEvent
  | BackgroundCompletedEvent
  | CommandResultEvent
  | ArtifactContentEvent
  | GoalUpdatedEvent
  | ConversationHydrationUpdatedEvent
  | ConversationCompactionUpdatedEvent
  | ConversationSummaryUpdatedEvent
  | ConversationListEvent
  | ConversationSwitchedEvent
  | LlmModelUpdatedEvent
  | UserMessageQueueUpdatedEvent
  | SkillsListEvent
  | SkillsMarketplaceListEvent
  | CommandsListEvent
  | SystemNoticeEvent
  | PongEvent
  | RuntimeCapabilitiesEvent
  | ClientCommandAckEvent
  | ControlRequestEvent
  | ProviderOAuthAuthEvent
  | ProviderOAuthDeviceCodeEvent
  | ProviderOAuthInfoEvent
  | ProviderOAuthProgressEvent
  | SessionRestoredEvent
  | SessionReplayEvent
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
  UserMessageQueueCancelCommand,
  UserMessageQueueSteerCommand,
  InterruptCommand,
  PingCommand,
  ReadArtifactCommand,
  ConversationCreateCommand,
  ConversationCloneCommand,
  ConversationMergeCommand,
  ConversationExportCommand,
  ConversationSwitchCommand,
  ConversationClearCommand,
  ConversationTruncateCommand,
  ContextForkCommand,
  ConversationDeleteCommand,
  ConversationWorktreeCleanupCommand,
  ConversationWorktreeHandoffPreflightCommand,
  ConversationWorktreeHandoffExecuteCommand,
  ConversationArchiveCommand,
  ConversationRenameCommand,
  ConversationMemoryModeSetCommand,
  MemoryResetCommand,
  ConversationGoalSetCommand,
  LlmModelSetCommand,
  LlmConfigSetCommand,
} from "./conversation-types";

import type {
  AgentResumeCommand,
  SubagentCancelCommand,
  SubagentStatusCommand,
  SubagentTranscriptCommand,
  SubagentPlanReviewCommand,
  SendMessageCommand,
  InspectorFocusCommand,
} from "./streaming-types";

import type {
  TerminalCreateCommand,
  TerminalListCommand,
  TerminalInputCommand,
  TerminalResizeCommand,
  TerminalKillCommand,
  TerminalRestartCommand,
  TerminalSnapshotRequestCommand,
  TerminalClearCommand,
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
  SchedulerRunNowCommand,
  SchedulerRetryCommand,
  SchedulerCancelCommand,
  RuntimeCapabilitiesInspectCommand,
  ControlResponseCommand,
  ControlCancelRequestCommand,
  McpAddCommand,
  McpUpdateCommand,
  McpInventoryListCommand,
  McpInventoryCancelCommand,
  McpProjectDecisionCommand,
} from "./common-types";

import type {
  EnvListCommand,
  EnvSetCommand,
  EnvDeleteCommand,
  GitPrAutomationSetCommand,
  CheckpointListCommand,
  CheckpointRewindCommand,
  RunCheckpointListCommand,
} from "./workspace-types";

export interface ClientCommandEnvelope {
  client_command_id?: string;
}

type ClientCommandPayload =
  | UserMessageCommand
  | UserMessageQueueCancelCommand
  | UserMessageQueueSteerCommand
  | InterruptCommand
  | PingCommand
  | ControlResponseCommand
  | ControlCancelRequestCommand
  | AgentResumeCommand
  | SubagentCancelCommand
  | SubagentStatusCommand
  | SubagentTranscriptCommand
  | SubagentPlanReviewCommand
  | SendMessageCommand
  | InspectorFocusCommand
  | TerminalCreateCommand
  | TerminalListCommand
  | TerminalInputCommand
  | TerminalResizeCommand
  | TerminalKillCommand
  | TerminalRestartCommand
  | TerminalSnapshotRequestCommand
  | TerminalClearCommand
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
  | SchedulerRunNowCommand
  | SchedulerRetryCommand
  | SchedulerCancelCommand
  | RuntimeCapabilitiesInspectCommand
  | McpAddCommand
  | McpUpdateCommand
  | McpInventoryListCommand
  | McpInventoryCancelCommand
  | McpProjectDecisionCommand
  | EnvListCommand
  | EnvSetCommand
  | EnvDeleteCommand
  | GitPrAutomationSetCommand
  | CheckpointListCommand
  | CheckpointRewindCommand
  | RunCheckpointListCommand
  | ReadArtifactCommand
  | ConversationCreateCommand
  | ConversationCloneCommand
  | ConversationMergeCommand
  | ConversationExportCommand
  | ConversationSwitchCommand
  | ConversationClearCommand
  | ConversationTruncateCommand
  | ContextForkCommand
  | ConversationDeleteCommand
  | ConversationWorktreeCleanupCommand
  | ConversationWorktreeHandoffPreflightCommand
  | ConversationWorktreeHandoffExecuteCommand
  | ConversationArchiveCommand
  | ConversationRenameCommand
  | ConversationMemoryModeSetCommand
  | MemoryResetCommand
  | ConversationGoalSetCommand
  | ProviderOAuthCommand
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
  "item.started",
  "agent_message.delta",
  "item.completed",
  "image_chunk",
  "thinking_delta",
  "thinking",
  "tool_call",
  "tool_result",
  "tool_output_delta",
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
  "done",
  "error",
  "stream_resume",
  "stream_event",
  "rate_limit",
  "session.state_changed",
  // Subagents + citations + inspector
  "subagent.start",
  "subagent.event",
  "subagent.mailbox",
  "subagent.progress",
  "subagent.done",
  "subagent.plan_approval_requested",
  "parent.notifications",
  "citation.add",
  "inspector.update",
  "turn.plan.updated",
  "turn.diff.updated",
  // Context lifecycle
  "context_usage",
  "context_compacted",
  "context_forked",
  "context_ledger",
  "context_side_query_result",
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
  "background.started",
  "background.stalled",
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
  "checkpoint.created",
  "checkpoint.list",
  "checkpoint.rewound",
  "checkpoint.run.list",
  "checkpoint.run.resume",
  "guidelines.updated",
  "permission.mode.updated",
  "permission.rules.updated",
  // Common / infrastructure
  "mcp_status",
  "mcp.lifecycle",
  "mcp.progress",
  "scheduler.list",
  "session.restored",
  "session.replay",
  "session.synced",
  "runtime.capabilities",
  "client.command.ack",
  "pong",
  "control_request",
  "llm.provider.oauth.auth",
  "llm.provider.oauth.device_code",
  "llm.provider.oauth.info",
  "llm.provider.oauth.progress",
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
  "user_message.queue.cancel",
  "user_message.queue.steer",
  "interrupt",
  "ping",
  // Control plane
  "control_response",
  "control_cancel_request",
  // Skills
  "read_artifact",
  "approval.file_diff",
  // Conversation lifecycle
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
  "conversation.permission.rules.list",
  "conversation.permission.rules.add",
  "conversation.permission.rules.remove",
  "permissions.content_rule.add",
  "context.compact",
  "context.fork",
  "context.ledger",
  "context.side_query",
  // Session inspection
  "session.tasks.inspect",
  "session.status.inspect",
  "session.usage.inspect",
  "session.permissions.inspect",
  "runtime.capabilities.inspect",
  // LLM
  "llm.model.set",
  "llm.provider.oauth.login",
  "llm.provider.oauth.logout",
  "llm.provider.oauth.status",
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
  "terminal.restart",
  "terminal.list",
  "terminal.snapshot.request",
  "terminal.clear",
  "terminal.mirror.created",
  "terminal.mirror.output",
  "terminal.mirror.exit",
  "terminal.exec",
  // Workspace
  "workspace.import",
  "workspace.switch",
  "workspace.recent",
  "workspace.recent.remove",
  "workspace.recent.clear",
  "workspace.set",
  // Session restore / sync
  "session.restore",
  "session.sync",
  // Streaming / task management
  "agent.resume",
  "subagent.cancel",
  "subagent.status",
  "subagent.transcript",
  "subagent.plan_review",
  "send_message",
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
  // Scheduler
  "scheduler.list",
  "scheduler.add",
  "scheduler.remove",
  "scheduler.toggle",
  "scheduler.run_now",
  "scheduler.retry",
  "scheduler.cancel",
]);

// SERVER_EVENT_TYPES / CLIENT_COMMAND_TYPES have no runtime callers in this
// file: the sets are consumed by protocol/server-event-validation.ts and are
// parsed by scripts/check-protocol-sync.py to detect backend/frontend drift.
// Do not delete them because grep shows no in-app call site.
