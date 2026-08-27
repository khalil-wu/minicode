/**
 * Streaming, thinking, tool execution, subagent, and task/plan event types.
 *
 * Domain subset of the WebSocket protocol for events related to the agent
 * execution loop: text streaming, thinking deltas, tool calls and results,
 * approval flows, subagent orchestration, todo task updates, plan step
 * tracking, citations, and inspector focus.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

import type { AgentCapabilitiesPayload } from "./capabilities";
import type { InspectorTargetKind, ProviderRawMetadata } from "../stores/types";

export const AGENT_PROGRESS_STAGES = [
  "status",
  "planning",
  "tool",
  "approval",
  "verification",
  "image_generation",
  "cache",
  "final",
] as const;

export type AgentProgressStage = (typeof AGENT_PROGRESS_STAGES)[number];

export const AGENT_PROGRESS_STATUSES = [
  "running",
  "completed",
  "partial",
  "failed",
  "info",
] as const;

export type AgentProgressStatus = (typeof AGENT_PROGRESS_STATUSES)[number];

export const AGENT_PROGRESS_PHASES = [
  "orienting",
  "planning",
  "model",
  "tool",
  "image_generation",
  "approval",
  "verify",
  "final",
  "recover",
  "status",
  "iteration",
  "subagent",
  "cache",
] as const;

export type AgentProgressPhase = (typeof AGENT_PROGRESS_PHASES)[number];

export const isAgentProgressPhase = (value: unknown): value is AgentProgressPhase =>
  typeof value === "string" && (AGENT_PROGRESS_PHASES as readonly string[]).includes(value);

// ──────────────────────────────────────────────────────────────────
// Server event type strings (streaming domain)
// ──────────────────────────────────────────────────────────────────

export type StreamingServerEventType =
  // Streaming text + tool execution
  | "item.started"
  | "agent_message.delta"
  | "item.completed"
  | "image_chunk"
  | "thinking_delta"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "tool_output_delta"
  | "agent.run.started"
  | "agent.run.completed"
  | "agent.item"
  | "agent.progress"
  | "runtime.span"
  | "task.update"
  | "approval_request"
  | "permission.decision"
  | "approval.cancelled"
  | "approval.file_diff"
  | "ask_user"
  | "done"
  | "error"
  | "stream_resume"
  // SDK / provider transparency
  | "stream_event"
  | "rate_limit"
  | "session.state_changed"
  // Subagents
  | "subagent.start"
  | "subagent.event"
  | "subagent.mailbox"
  | "subagent.progress"
  | "subagent.done"
  | "subagent.plan_approval_requested"
  | "parent.notifications"
  // Citations + inspector
  | "citation.add"
  | "inspector.update"
  // MiniCode current-turn checklist and aggregate diff snapshots
  | "turn.plan.updated"
  | "turn.diff.updated";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (streaming domain)
// ──────────────────────────────────────────────────────────────────

export type StreamingClientCommandType =
  | "agent.resume"
  | "subagent.cancel"
  | "subagent.status"
  | "subagent.transcript"
  | "subagent.plan_review"
  | "send_message"
  | "inspector.focus";

// ──────────────────────────────────────────────────────────────────
// Event payload types
// ──────────────────────────────────────────────────────────────────

export interface AgentMessageItem {
  id: string;
  type: "agent_message";
  text: string;
  source?: "model_final" | "reply" | "partial" | "commentary" | "model_preamble" | "post_tool" | "runtime" | string;
  status?: "in_progress" | "completed" | "partial" | string;
}

export interface ItemStartedEvent {
  type: "item.started";
  conversation_id: string;
  item: AgentMessageItem;
  message_id: string;
}

export interface AgentMessageDeltaEvent {
  type: "agent_message.delta";
  conversation_id: string;
  item_id: string;
  delta: string;
  source?: AgentMessageItem["source"];
  message_id?: string;
}

export interface ItemCompletedEvent {
  type: "item.completed";
  conversation_id: string;
  item: AgentMessageItem;
  finish_reason?: string;
  provider_raw?: ProviderRawMetadata;
  attachments?: ReplyAttachment[];
  message_id: string;
}

export interface ImageChunkLiveEvent {
  type: "image_chunk";
  conversation_id: string;
  message_id: string;
  image_data: string;
  media_type: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  image_data_omitted?: never;
  image_data_size?: number;
}

export interface ImageChunkReplayEvent {
  type: "image_chunk";
  conversation_id: string;
  message_id: string;
  media_type: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  image_data?: never;
  image_data_omitted: true;
  image_data_size: number;
}

export type ImageChunkEvent = ImageChunkLiveEvent | ImageChunkReplayEvent;

export interface ReplyAttachment {
  /** Absolute or workspace-relative file path. */
  path: string;
  /** File size in bytes. */
  size: number;
  /** True for previewable image formats (.png/.jpg/.jpeg/.gif/.webp). */
  is_image: boolean;
}

export interface ThinkingDeltaEvent {
  type: "thinking_delta" | "thinking";
  conversation_id: string;
  message_id: string;
  content: string;
  source?: "provider" | "model_preamble" | "post_tool" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  phase?: string;
  item_id?: string;
  content_index?: number;
  lifecycle?: "start" | "delta" | "end" | string;
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
  result_kind?: "web" | "command" | "file" | "edit" | "search" | "mcp" | "generic" | string;
  activity_kind?: string;
  visibility?: "timeline" | "compact" | "debug" | string;
  group_id?: string;
  step_id?: string;
  turn_id?: string;
  task_id?: string;
  seq?: number;
  iteration_id?: string;
  phase?: "tool" | "approval" | "model" | "final" | "recover" | string;
  /** Live +/- line counts projected while the tool's arguments stream in. */
  diff?: { plus?: number; minus?: number } | null;
}

export interface ToolErrorInfo {
  error_kind?: string;
  code?: string;
  category?: string;
  user_summary?: string;
  user_message?: string;
  model_observation: string;
  developer_detail: string;
  recoverable: boolean;
  projection?: "silent" | "status" | "warning" | "error" | "approval" | string;
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
  status?: "success" | "failed" | "blocked" | "partial" | "timeout" | string;
  duration_ms?: number;
  display_summary?: string;
  result_kind?: "web" | "command" | "file" | "edit" | "search" | "mcp" | "generic" | string;
  activity_kind?: string;
  visibility?: "timeline" | "compact" | "debug" | string;
  group_id?: string;
  step_id?: string;
  limitation?: string;
  provider?: string;
  provider_error_type?: "busy" | "rate_limit" | "auth" | "network" | "billing" | "blocked" | "unknown" | string;
  error_info?: ToolErrorInfo;
  error_kind?: string;
  user_summary?: string;
  developer_detail?: string;
  recoverable?: boolean;
  projection?: "silent" | "status" | "warning" | "error" | "approval" | string;
  turn_id?: string;
  task_id?: string;
  seq?: number;
  iteration_id?: string;
  phase?: "tool" | "approval" | "model" | "final" | "recover" | string;
  output_files?: Array<{
    path: string;
    name?: string;
    size: number;
    mime_type?: string;
    is_image?: boolean;
  }>;
  superseded_tool_call_ids?: string[];
  removed_file_paths?: string[];
}

export interface ToolOutputDeltaEvent {
  type: "tool_output_delta";
  id: string;
  output: string;
  stream?: "stdout" | "stderr" | string;
  turn_id?: string;
  iteration_id?: string;
  step_id?: string;
}

export interface CommandOutputChunkEvent {
  type: "command_output_chunk";
  conversation_id: string;
  message_id: string;
  content: string;
  stream: "stdout" | "stderr";
  turn_id?: string;
  id?: string;
  tool_call_id?: string;
}

export interface PermissionDecisionEvent {
  type: "permission.decision";
  tool_call_id: string;
  tool_name: string;
  decision: "allow" | "deny" | "ask" | string;
  source?: "hook" | "policy" | "user" | string;
  permission_level?: string;
  message?: string;
  capability?: { allowed?: boolean; reason?: string };
  approval_policy?: string;
  matched_rule?: { source?: string; rule?: string };
  risk?: "low" | "medium" | "high" | "critical" | string;
  scope?: Record<string, unknown>;
  expiry?: "call" | "session" | "policy" | string;
}

export interface AgentItemEvent {
  type: "agent.item";
  conversation_id: string;
  message_id: string;
  id: string;
  item_id?: string;
  loop_id?: string;
  iteration_id?: string;
  parent_id?: string;
  kind: "process_text" | "observation" | "status" | "plan" | "tool_group" | string;
  source?: "model" | "runtime" | "system" | "tool" | string;
  role?: "assistant" | "runtime" | string;
  status?: "running" | "completed" | "failed" | "info" | string;
  title?: string;
  content?: string;
  summary?: string;
  visibility?: "timeline" | "compact" | "debug" | string;
  created_at?: number;
  order?: number;
  default_collapsed?: boolean;
  group_id?: string;
  step_id?: string;
  tool_call_ids?: string[];
  skill_name?: string;
  trigger_mode?: "explicit" | "implicit" | "model" | string;
  source_level?: string;
  reason?: string;
  token_estimate?: number;
}

export interface AgentProgressEvent {
  type: "agent.progress";
  conversation_id: string;
  message_id: string;
  id: string;
  stage: AgentProgressStage;
  phase?: AgentProgressPhase;
  status: AgentProgressStatus;
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
  iteration_id?: string;
  ephemeral?: boolean;
}

export interface RuntimeSpanEvent {
  type: "runtime.span";
  conversation_id: string;
  event: string;
  span_id: string;
  parent_span_id?: string;
  run_id?: string;
  turn_id?: string;
  message_id: string;
  iteration_id?: string;
  phase?: "context" | "provider" | "model" | "tool" | "approval" | "final" | "recovery" | "recover" | string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted" | "superseded" | "partial" | "info";
  label?: string;
  summary?: string;
  started_at?: number;
  ended_at?: number;
  duration_ms?: number;
  tool_call_id?: string;
  tool_name?: string;
  agent_id?: string;
  waiting_on?: string;
  blocking_reason?: string;
  ui_visible: boolean;
  debug_only: boolean;
  data?: Record<string, unknown>;
}

export interface AgentRunRecordPayload {
  run_id: string;
  conversation_id?: string;
  parent_run_id?: string;
  turn_id?: string;
  role?: string;
  phase?: "plan" | "execute" | "verify" | "recover" | "final" | string;
  status?: "running" | "completed" | "partial" | "failed" | "cancelled" | "interrupted" | string;
  budget?: Record<string, unknown>;
  started_at?: number;
  completed_at?: number | null;
  task_id?: string;
  session_id?: string;
  summary?: string;
  error?: string;
  terminal_reason?: string;
}

export interface AgentRunStartedEvent extends AgentRunRecordPayload {
  type: "agent.run.started";
}

export interface AgentRunCompletedEvent extends AgentRunRecordPayload {
  type: "agent.run.completed";
}

export interface ApprovalRequestEvent {
  type: "approval_request";
  tool_call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  conversation_id: string;
  turn_id?: string;
  message_id?: string;
  workspace_root?: string;
  permission_mode?: string;
  workspace_scope?: string;
  source_agent?: string;
  source_thread?: string;
  source_tool?: string;
  diff?: unknown;
  timeout_seconds?: number;
  expires_at?: number;
}

export interface ApprovalFileDiffEvent {
  type: "approval.file_diff";
  conversation_id: string;
  tool_call_id: string;
  path: string;
  patch: string;
  is_large: boolean;
  is_truncated: boolean;
  turn_id?: string;
  workspace_root?: string;
}

export interface ApprovalCancelledEvent {
  type: "approval.cancelled";
  request_ids: string[];
  reason?: string;
}

export interface AskUserEvent {
  type: "ask_user";
  conversation_id: string;
  tool_call_id: string;
  question: string;
  options?: string[];
  turn_id?: string;
  message_id?: string;
}

export interface DoneEvent {
  type: "done";
  conversation_id: string;
  message_id: string;
  status: "completed" | "partial" | "failed" | "cancelled" | "interrupted";
  reason?: string;
  duration_ms?: number;
  failure_recoverable?: boolean;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
    input_includes_cache_read: boolean;
    input_includes_cache_write?: boolean;
    ordinary_input_tokens?: number;
    cache_deleted_input_tokens?: number;
    prompt_cache_total_tokens?: number;
    prompt_cache_hit_rate?: number;
    reasoning_output_tokens?: number;
    cost_usd?: number;
  };
  provider_raw?: ProviderRawMetadata;
}

export interface ErrorEvent {
  type: "error";
  message: string;
  recoverable: boolean;
  error_type: "api" | "tool" | "budget" | "stagnant" | string;
  conversation_id?: string;
  message_id?: string;
  tool_call_id?: string;
  request_id?: string;
  error_code?: string;
  provider?: string;
  provider_error_type?: "busy" | "rate_limit" | "auth" | "network" | "billing" | "blocked" | "unknown" | string;
  attachments?: Array<Record<string, unknown>>;
}

export interface StreamResumeEvent {
  type: "stream_resume";
  conversation_id: string;
  message_id: string | null;
  turn_id?: string;
  /** Ordered authoritative projection for an in-flight assistant turn. */
  content_blocks?: Array<Record<string, unknown>>;
  phase?: string;
  stream_status?: string;
  event_seq?: number;
  last_event_type?: string;
  tool_calls_pending: Array<{
    id: string;
    name: string;
    args: Record<string, unknown>;
    status?: "pending" | "running" | string;
    started_at?: number;
    startedAt?: number;
    display_hint?: string;
    displayHint?: string;
    input_summary?: string;
    inputSummary?: string;
    iteration_id?: string;
    iterationId?: string;
    phase?: string;
  }>;
  tool_states?: Array<{
    id: string;
    name: string;
    args?: Record<string, unknown>;
    status?: string;
    transition?: string;
    started_at?: number;
    startedAt?: number;
    finished_at?: number;
    finishedAt?: number;
    duration_ms?: number;
    durationMs?: number;
    waiting_on?: string;
    waitingOn?: string;
    blocking_reason?: string;
    blockingReason?: string;
    outputPreview?: string;
    stdoutPreview?: string;
    stderrPreview?: string;
  }>;
}

// ── SDK / provider transparency events ─────────────────────────

export interface StreamEventEvent {
  type: "stream_event";
  provider: string;
  event_type: string;
  data: Record<string, unknown>;
  sdk_only?: boolean;
}

export interface RateLimitEvent {
  type: "rate_limit";
  provider?: string;
  error_type: "rate_limit" | "quota_exceeded" | "concurrency_limit" | string;
  retry_after_seconds?: number;
  retry_at?: number;
  message?: string;
  recoverable?: boolean;
}

export interface SessionStateEvent {
  type: "session.state_changed";
  state: "idle" | "working";
  conversation_id?: string;
  run_id?: string;
  reason?: string;
}

// ── Subagent events ─────────────────────────────────────────────

export interface SubagentTranscriptSnapshot {
  seq: number;
  messages?: Record<string, unknown>[];
}

export interface SubagentStartEvent {
  type: "subagent.start";
  subagent_id: string;
  agent_path?: string;
  mailbox_epoch?: number;
  parent_id: string;
  role: string;
  prompt?: string;
  parent_run_id?: string;
  turn_id?: string;
  node_id?: string;
  task_id?: string;
  objective?: string;
  depends_on?: string[];
  blocked_by?: string[];
  background?: boolean;
  read_only?: boolean;
  write_scope?: string[];
  current_activity?: string;
  waiting_on?: string;
  last_progress_at?: number;
  record?: Record<string, unknown>;
  transcript_snapshot?: SubagentTranscriptSnapshot;
}

export interface SubagentEventEvent {
  type: "subagent.event";
  subagent_id: string;
  event: { type: string; [key: string]: unknown };
}

export interface SubagentProgressEvent {
  type: "subagent.progress";
  subagent_id: string;
  /** A refreshed live state; terminal states are always sent as subagent.done. */
  status?: "pending" | "running" | "blocked";
  agent_path?: string;
  mailbox_epoch?: number;
  iteration?: number;
  max_iterations?: number;
  tool_name?: string;
  tool_call_id?: string;
  source_event_type?: string;
  detail?: string;
  current_activity?: string;
  waiting_on?: string;
  last_progress_at?: number;
  activity_kind?: string;
  activity_summary?: string;
  user_visible?: boolean;
  transcript_snapshot?: SubagentTranscriptSnapshot;
}

export interface SubagentDoneEvent {
  type: "subagent.done";
  subagent_id: string;
  agent_path?: string;
  mailbox_epoch?: number;
  summary?: string;
  error?: string;
  duration_ms?: number;
  iterations?: number;
  tool_call_count?: number;
  timed_out?: boolean;
  status?: "completed" | "partial" | "failed" | "cancelled";
  termination_reason?: string;
  initiator?: string;
  result?: Record<string, unknown>;
  snapshot?: { result?: Record<string, unknown> };
  record?: Record<string, unknown>;
  prompt_cache_fork?: Record<string, unknown>;
  cancel_requested?: boolean;
  cancelled?: boolean;
  transcript_snapshot?: SubagentTranscriptSnapshot;
}

/** A teammate submitted a plan and is blocked until the leader decides.
 *
 * The runtime only auto-approves when the permission mode pre-authorizes broad
 * execution; otherwise it surfaces the request and waits for the user, so this
 * event must become a blocking prompt (answered with subagent.plan_review). */
export interface SubagentPlanApprovalRequestedEvent {
  type: "subagent.plan_approval_requested";
  conversation_id: string;
  subagent_id: string;
  request_id: string;
  teammate_name?: string;
  team_name?: string;
  plan_file_path?: string;
  plan_content?: string;
}

export interface ParentNotificationsEvent {
  type: "parent.notifications";
  count: number;
  parent_run_id: string;
  conversation_id: string;
}

// ── Citations + inspector ───────────────────────────────────────

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
  conversation_id: string;
  message_id: string;
  artifact_id: string;
  kind: "file" | "diff" | "image" | "json" | "code" | "text";
  summary: string;
  bytes?: number;
  media_type?: string;
  url?: string;
  text_offset?: number;
}

export interface InspectorUpdateEvent {
  type: "inspector.update";
  conversation_id: string;
  request_id?: string;
  target_kind: InspectorTargetKind;
  target_id: string;
  payload: Record<string, unknown>;
}

// ── Task / plan updates ─────────────────────────────────────────

export interface TodoTaskUpdateEvent {
  type: "task.update";
  todo_id: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
  content: string;
  activeForm?: string;
}

export interface TodoTaskSnapshotEvent {
  type: "task.update";
  todos: Array<Omit<TodoTaskUpdateEvent, "type"> & { id?: string }>;
}

export interface RuntimePendingApprovalSnapshot {
  request_id: string;
  type: string;
  conversation_id?: string;
  subtype?: string;
  tool_name?: string;
}

export interface SubagentMailboxEvent {
  type: "subagent.mailbox";
  subagent_id: string;
  count: number;
  high_water?: number;
  mailbox_epoch?: number;
  stale_sealed?: number;
}

export interface RuntimeQueuedUserMessageSnapshot {
  conversation_id: string;
  message_id: string;
  user_message_id?: string;
  content?: string;
  position?: number;
}

export interface RuntimePendingTurnInputSnapshot {
  conversation_id: string;
  mode: "steer";
  message_id: string;
  user_message_id?: string;
  target_message_id?: string;
  content?: string;
  attachments?: Record<string, unknown>[];
  position?: number;
  queued_at_ms?: number;
}

export interface RuntimeForkSnapshot {
  fork_id: string;
  parent_conversation_id?: string;
  branch_conversation_id?: string;
  message_index?: number;
  history_length?: number;
  estimated_tokens?: number;
  created_at?: string;
  status?: string;
}

export interface RuntimeSessionSnapshot {
  session_id?: string;
  parent_session_id?: string | null;
  active_conversation_id?: string | null;
  active_conversation?: unknown;
  active_task_id?: string | null;
  active_stream_conversation_ids?: string[];
  workspace_root?: string | null;
  selected_model?: string | null;
  provider_capabilities?: AgentCapabilitiesPayload["provider_capabilities"];
  invoked_skill_names?: string[];
  permission_mode?: string;
  permission_profile?: "confirm" | "plan" | "auto" | "bypass" | string;
  permission_source?: string;
  workspace_scope?: "computer" | "project" | "worktree" | string;
  sandbox_status?: {
    os?: "enforced" | "app_layer" | "disabled" | string;
    network?: "restricted" | "approval_required" | "enabled" | string;
    policy_configured?: boolean;
    probe_status?: string;
    enforcement?: string;
    requested?: {
      filesystem?: boolean;
      network?: boolean;
      deny_read?: boolean;
      protected_paths?: boolean;
    };
    backend_available?: boolean | null;
    backend?: string;
    filesystem_isolated?: boolean | null;
    network_isolated?: boolean | null;
    deny_read_isolated?: boolean | null;
    protected_paths_isolated?: boolean | null;
    fail_closed?: boolean;
    unavailable_action?: string;
    reason?: string;
    policy_limitations?: string[];
    probed_at?: number;
  };
  mcp?: {
    connected?: number;
    failed?: number;
    auth_required?: number;
    servers?: { name?: string; status?: string; phase?: string }[];
  };
  pending_approval_count?: number;
  pending_approvals?: RuntimePendingApprovalSnapshot[];
  queued_user_messages?: RuntimeQueuedUserMessageSnapshot[];
  pending_turn_inputs?: RuntimePendingTurnInputSnapshot[];
  forks?: RuntimeForkSnapshot[];
  running_tasks?: unknown[];
  task_summary?: Record<string, unknown>;
  capabilities?: AgentCapabilitiesPayload;
}

export interface SessionTaskUpdateEvent {
  type: "task.update";
  session: RuntimeSessionSnapshot;
}

export type TaskUpdateEvent = TodoTaskUpdateEvent | TodoTaskSnapshotEvent | SessionTaskUpdateEvent;

export interface TurnPlanStep {
  step: string;
  status: "pending" | "in_progress" | "completed";
}

export interface TurnPlanUpdatedEvent {
  type: "turn.plan.updated";
  thread_id: string;
  conversation_id: string;
  turn_id: string;
  explanation?: string | null;
  plan: TurnPlanStep[];
  message_id?: string;
}

/** MiniCode app-server ``turn/diff/updated`` notification. */
export interface TurnDiffUpdatedEvent {
  type: "turn.diff.updated";
  thread_id: string;
  conversation_id: string;
  turn_id: string;
  message_id?: string;
  task_id?: string;
  diff: string;
  revision?: number;
  tool_call_id?: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (streaming domain)
// ──────────────────────────────────────────────────────────────────

interface ConversationOwnedCommand {
  conversation_id: string;
  workspace_root?: string;
}

export interface AgentResumeCommand extends ConversationOwnedCommand {
  type: "agent.resume";
}

export interface SubagentCancelCommand extends ConversationOwnedCommand {
  type: "subagent.cancel";
  subagent_id: string;
}

export interface SubagentStatusCommand extends ConversationOwnedCommand {
  type: "subagent.status";
  subagent_id: string;
  include_result?: boolean;
  include_messages?: boolean;
}

export interface SubagentTranscriptCommand extends ConversationOwnedCommand {
  type: "subagent.transcript";
  subagent_id: string;
}

/** The leader's decision on a teammate plan approval request. */
export interface SubagentPlanReviewCommand {
  type: "subagent.plan_review";
  subagent_id: string;
  request_id: string;
  approved: boolean;
  /** Explicit owner when known; the runtime rejects a stale conversation. */
  conversation_id?: string;
}

export interface SendMessageCommand {
  type: "send_message";
  recipient: string;
  message: string;
  sender?: string;
  message_id?: string;
  conversation_id?: string;
  task_id?: string;
  team_name?: string;
}

export interface InspectorFocusCommand extends ConversationOwnedCommand {
  type: "inspector.focus";
  target_kind: InspectorTargetKind;
  target_id: string;
}
