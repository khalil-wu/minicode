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
  | "task.update"
  | "approval_request"
  | "approval.cancelled"
  | "approval.file_diff"
  | "ask_user"
  | "done"
  | "error"
  | "stream_resume"
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
  | "verification.run"
  | "subagent.cancel"
  | "inspector.focus"
  | "pause_streaming"
  | "resume_streaming";

// ──────────────────────────────────────────────────────────────────
// Event payload types
// ──────────────────────────────────────────────────────────────────

export interface TextChunkEvent {
  type: "text_chunk";
  content: string;
  source?: string;
  visibility?: "final" | "timeline" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
  message_id?: string;
}

export interface TextReplaceEvent {
  type: "text_replace";
  content: string;
  source?: string;
  visibility?: "final" | "timeline" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
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
  status?: "success" | "failed" | "blocked" | string;
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
}

export interface AgentProgressEvent {
  type: "agent.progress";
  id: string;
  stage: "status" | "planning" | "tool" | "approval" | "verification" | "final";
  phase?: "orienting" | "planning" | "model" | "tool" | "approval" | "verify" | "final" | "recover" | "status" | "iteration";
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
}

export interface AgentRunRecordPayload {
  run_id: string;
  conversation_id?: string;
  parent_run_id?: string;
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
  provider_error_type?: "busy" | "rate_limit" | "auth" | "network" | "billing" | "blocked" | "unknown" | string;
}

export interface StreamResumeEvent {
  type: "stream_resume";
  conversation_id: string;
  message_id: string | null;
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

// ── Subagent events ─────────────────────────────────────────────

export interface SubagentStartEvent {
  type: "subagent.start";
  subagent_id: string;
  parent_id: string;
  role: string;
  prompt?: string;
  parent_run_id?: string;
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
  detail?: string;
  display_scope?: DisplayScope;
  panel_hint?: PanelHint;
  requires_attention?: boolean;
}

export interface SubagentDoneEvent {
  type: "subagent.done";
  subagent_id: string;
  summary?: string;
  error?: string;
  record?: Record<string, unknown>;
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
  target_kind: "message" | "tool_call" | "artifact" | "file" | "diff" | "subagent" | "budget";
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
  active_conversation_id?: string | null;
  active_task_id?: string | null;
  selected_model?: string | null;
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

export interface VerificationRunCommand {
  type: "verification.run";
  command?: string;
  timeout_seconds?: number;
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
