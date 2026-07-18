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

export const AGENT_PROGRESS_PHASES = [
  "orienting",
  "planning",
  "model",
  "tool",
  "approval",
  "verify",
  "final",
  "recover",
  "status",
  "iteration",
  "subagent",
  "workflow",
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
  | "text_chunk"
  | "text_replace"
  | "image_chunk"
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
  | "tool_use_summary"
  // Subagents
  | "subagent.start"
  | "subagent.event"
  | "subagent.progress"
  | "subagent.done"
  | "verification.started"
  | "verification.result"
  // Citations + inspector
  | "citation.add"
  | "inspector.update"
  // Plan step tracking (plan.update is dead code, removed)
  | "plan_step_updated"
  | "plan_updated";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (streaming domain)
// ──────────────────────────────────────────────────────────────────

export type StreamingClientCommandType =
  | "task.edit"
  | "plan.edit"
  | "agent.resume"
  | "workflow.resume"
  | "verification.run"
  | "subagent.cancel"
  | "subagent.status"
  | "subagent.resume"
  | "send_message"
  | "inspector.focus";

// ──────────────────────────────────────────────────────────────────
// Event payload types
// ──────────────────────────────────────────────────────────────────

export interface TextChunkEvent {
  type: "text_chunk";
  content: string;
  source?: string;
  visibility?: "final" | "timeline" | "unsealed" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
  finalize?: boolean;
  metadata?: TextStreamMetadata;
  segmentId?: string;
  segment_id?: string;
  iterationIndex?: number;
  iteration_index?: number;
  streamAttempt?: number;
  stream_attempt?: number;
  sealReason?: string;
  seal_reason?: string;
  message_id?: string;
}

export interface TextReplaceEvent {
  type: "text_replace";
  content: string;
  source?: string;
  visibility?: "final" | "timeline" | "unsealed" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
}

export interface TextStreamMetadata {
  visibility?: "final" | "timeline" | "unsealed" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
  segmentId?: string;
  segment_id?: string;
  iterationIndex?: number;
  iteration_index?: number;
  streamAttempt?: number;
  stream_attempt?: number;
  sealReason?: string;
  seal_reason?: string;
  sealed?: boolean;
  promoteAllUnsealedNarration?: boolean;
  promote_all_unsealed_narration?: boolean;
}

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
  content: string;
  source?: "provider" | "model_preamble" | "post_tool" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  is_raw_provider_reasoning?: boolean;
  provider_reasoning_type?: string;
}

export type DisplayScope = "chat" | "activity" | "notice" | "agents" | "inspector" | "silent" | string;
export type PanelHint = "plan" | "subagents" | "diff" | "inspector" | "tasks" | "terminal" | "preview" | string;

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
  group_id?: string;
  step_id?: string;
  turn_id?: string;
  task_id?: string;
  seq?: number;
  iteration_id?: string;
  phase?: "tool" | "approval" | "model" | "final" | "recover" | string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
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
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
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

export interface AgentLoopEvent {
  type: "agent.loop.started" | "agent.loop.completed";
  item_id?: string;
  loop_id: string;
  iteration_id?: string;
  status?: "running" | "completed" | "failed" | "interrupted" | string;
  title?: string;
  summary?: string;
  started_at?: number;
  completed_at?: number;
  duration_ms?: number;
  item_count?: number;
  tool_call_count?: number;
  default_collapsed?: boolean;
  transition_reason?: string;
  transition_details?: Record<string, unknown>;
}

export interface AgentItemEvent {
  type: "agent.item";
  id: string;
  item_id?: string;
  loop_id?: string;
  iteration_id?: string;
  parent_id?: string;
  kind: "process_text" | "action_summary" | "observation" | "status" | "plan" | "tool_group" | string;
  source?: "model" | "runtime" | "system" | "tool" | string;
  role?: "assistant" | "runtime" | string;
  status?: "running" | "completed" | "failed" | "info" | string;
  title?: string;
  content?: string;
  summary?: string;
  visibility?: "timeline" | "compact" | "debug" | string;
  created_at?: number;
  order?: number;
  seq?: number;
  default_collapsed?: boolean;
  group_id?: string;
  step_id?: string;
  tool_call_ids?: string[];
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
  skill_name?: string;
  trigger_mode?: "explicit" | "implicit" | "model" | string;
  source_level?: string;
  reason?: string;
  token_estimate?: number;
}

export interface AgentProgressEvent {
  type: "agent.progress";
  id: string;
  stage: "status" | "planning" | "tool" | "approval" | "verification" | "final";
  phase?: AgentProgressPhase;
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
  iteration_id?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
  ephemeral?: boolean;
}

export interface RuntimeSpanEvent {
  type: "runtime.span";
  event: string;
  span_id: string;
  parent_span_id?: string;
  run_id?: string;
  turn_id?: string;
  message_id?: string;
  iteration_id?: string;
  phase?: "context" | "provider" | "model" | "tool" | "approval" | "verification" | "verify" | "final" | "recovery" | "recover" | string;
  status?: "running" | "completed" | "failed" | "info" | string;
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
  ui_visible?: boolean;
  debug_only?: boolean;
  requires_attention?: boolean;
  data?: Record<string, unknown>;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
}

export interface AgentRunRecordPayload {
  run_id: string;
  conversation_id?: string;
  parent_run_id?: string;
  turn_id?: string;
  role?: string;
  phase?: "plan" | "execute" | "verify" | "recover" | "final" | string;
  status?: "running" | "completed" | "failed" | "cancelled" | string;
  budget?: Record<string, unknown>;
  started_at?: number;
  completed_at?: number | null;
  task_id?: string;
  session_id?: string;
  summary?: string;
  error?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface AgentRunStartedEvent extends AgentRunRecordPayload {
  type: "agent.run.started";
}

export interface AgentRunUpdatedEvent extends AgentRunRecordPayload {
  type: "agent.run.updated";
}

export interface AgentRunCompletedEvent extends AgentRunRecordPayload {
  type: "agent.run.completed";
}

export interface AgentPhaseUpdatedEvent {
  type: "agent.phase.updated";
  run_id: string;
  phase: "plan" | "execute" | "verify" | "recover" | "final" | string;
  status?: "running" | "completed" | "failed" | string;
  summary?: string;
  role?: string;
  conversation_id?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface VerificationStartedEvent {
  type: "verification.started";
  run_id: string;
  command?: string;
  conversation_id?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface VerificationResultEvent {
  type: "verification.result";
  run_id: string;
  passed: boolean;
  output?: string;
  command?: string;
  conversation_id?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface ApprovalRequestEvent {
  type: "approval_request";
  tool_call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  source_agent?: string;
  source_thread?: string;
  source_tool?: string;
  diff?: unknown;
}

export interface ApprovalFileDiffEvent {
  type: "approval.file_diff";
  tool_call_id?: string;
  path?: string;
  patch?: string;
  is_large?: boolean;
  is_truncated?: boolean;
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
  status?: "completed" | "partial" | "failed" | "cancelled";
  reason?: string;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
    prompt_cache_total_tokens?: number;
    prompt_cache_hit_rate?: number;
    reasoning_output_tokens?: number;
  };
  providerRaw?: ProviderRawMetadata;
  provider_raw?: ProviderRawMetadata;
}

export interface ErrorEvent {
  type: "error";
  message: string;
  recoverable: boolean;
  error_type: "api" | "tool" | "budget" | "stagnant" | string;
  error_code?: string;
  provider_error_type?: "busy" | "rate_limit" | "auth" | "network" | "billing" | "blocked" | "unknown" | string;
}

export interface StreamResumeEvent {
  type: "stream_resume";
  conversation_id: string;
  message_id: string | null;
  turn_id?: string;
  accumulated_text?: string;
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

export interface ToolUseSummaryEvent {
  type: "tool_use_summary";
  summary: string;
  iteration_id?: string;
  tool_call_ids?: string[];
  tool_count?: number;
  generated_by?: "heuristic" | "llm";
}

// ── Subagent events ─────────────────────────────────────────────

export interface SubagentStartEvent {
  type: "subagent.start";
  subagent_id: string;
  parent_id: string;
  role: string;
  prompt?: string;
  parent_run_id?: string;
  turn_id?: string;
  workflow_id?: string;
  workflow_name?: string;
  workflow_mode?: string;
  node_id?: string;
  task_id?: string;
  objective?: string;
  depends_on?: string[];
  blocked_by?: string[];
  required_for_final?: boolean;
  blocks_final_reply?: boolean;
  read_only?: boolean;
  write_scope?: string[];
  current_activity?: string;
  waiting_on?: string;
  last_progress_at?: number;
  record?: Record<string, unknown>;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface SubagentEventEvent {
  type: "subagent.event";
  subagent_id: string;
  event: { type: string; [key: string]: unknown };
}

export interface SubagentProgressEvent {
  type: "subagent.progress";
  subagent_id: string;
  iteration?: number;
  max_iterations?: number;
  tool_name?: string;
  tool_call_id?: string;
  source_event_type?: string;
  detail?: string;
  current_activity?: string;
  waiting_on?: string;
  blocks_final_reply?: boolean;
  last_progress_at?: number;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface SubagentDoneEvent {
  type: "subagent.done";
  subagent_id: string;
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
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
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
  artifact_id: string;
  kind: "file" | "diff" | "image" | "json" | "code" | "text";
  summary: string;
  bytes?: number;
  media_type?: string;
  url?: string;
}

export interface InspectorUpdateEvent {
  type: "inspector.update";
  target_kind: InspectorTargetKind;
  target_id: string;
  payload: Record<string, unknown>;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
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
  permission_profile?: "ask" | "auto" | "full_access" | string;
  permission_source?: string;
  workspace_scope?: "computer" | "project" | "worktree" | string;
  sandbox_status?: {
    os?: "enforced" | "app_layer" | "disabled" | string;
    network?: "restricted" | "approval_required" | "enabled" | string;
  };
  mcp?: {
    connected?: number;
    failed?: number;
    auth_required?: number;
    servers?: { name?: string; status?: string; phase?: string }[];
  };
  pending_approval_count?: number;
  pending_approvals?: RuntimePendingApprovalSnapshot[];
  running_tasks?: unknown[];
  task_summary?: Record<string, unknown>;
  capabilities?: AgentCapabilitiesPayload;
}

export interface SessionTaskUpdateEvent {
  type: "task.update";
  session: RuntimeSessionSnapshot;
}

export type TaskUpdateEvent = TodoTaskUpdateEvent | TodoTaskSnapshotEvent | SessionTaskUpdateEvent;

/**
 * PlanStep is used by plan_updated, plan_step_updated, and plan.edit.
 */
export interface PlanStep {
  id: string;
  title: string;
  detail?: string;
  status: "pending" | "running" | "done" | "skipped" | "failed";
  tool_hint?: string;
}

export interface PlanStepUpdatedEvent {
  type: "plan_step_updated";
  plan_id: string;
  step_id?: string;
  step_index?: number;
  status: PlanStep["status"];
  title?: string;
  detail?: string;
  current_step?: number;
}

export interface PlanUpdatedEvent {
  type: "plan_updated";
  plan_id: string;
  status: string;
  current_step?: number;
  steps: PlanStep[];
  explanation?: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (streaming domain)
// ──────────────────────────────────────────────────────────────────

export interface TaskEditCommand {
  type: "task.edit";
  todo_id: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
}

export interface PlanEditCommand {
  type: "plan.edit";
  plan_id: string;
  action: "accept" | "reject";
  steps?: PlanStep[];
  current_step?: number;
}

export interface AgentResumeCommand {
  type: "agent.resume";
  conversation_id?: string;
}

export interface WorkflowResumeCommand {
  type: "workflow.resume";
  workflow_id: string;
  conversation_id?: string;
  timeout_seconds?: number;
}

export interface VerificationRunCommand {
  type: "verification.run";
  command?: string;
  timeout_seconds?: number;
}

export interface SubagentCancelCommand {
  type: "subagent.cancel";
  subagent_id: string;
}

export interface SubagentStatusCommand {
  type: "subagent.status";
  subagent_id: string;
  include_result?: boolean;
  include_messages?: boolean;
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

export interface SubagentResumeCommand {
  type: "subagent.resume";
  subagent_id: string;
}

export interface InspectorFocusCommand {
  type: "inspector.focus";
  target_kind: InspectorTargetKind;
  target_id: string;
}
