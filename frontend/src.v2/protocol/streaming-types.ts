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

// ──────────────────────────────────────────────────────────────────
// Server event type strings (streaming domain)
// ──────────────────────────────────────────────────────────────────

export type StreamingServerEventType =
  // Streaming text + tool execution
  | "text_chunk"
  | "final_answer_started"
  | "final_answer_delta"
  | "final_answer_retracted"
  | "final_answer_committed"
  | "image_chunk"
  | "thinking_delta"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "tool_output_delta"
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
  | "subagent.cancel"
  | "inspector.focus";

// ──────────────────────────────────────────────────────────────────
// Event payload types
// ──────────────────────────────────────────────────────────────────

export interface TextChunkEvent {
  type: "text_chunk";
  content: string;
}

export interface FinalAnswerStartedEvent {
  type: "final_answer_started";
  message_id?: string;
}

export interface FinalAnswerDeltaEvent {
  type: "final_answer_delta";
  content: string;
  message_id?: string;
}

export interface FinalAnswerRetractedEvent {
  type: "final_answer_retracted";
  reason?: string;
  message_id?: string;
}

export interface FinalAnswerCommittedEvent {
  type: "final_answer_committed";
  message_id?: string;
}

export interface ThinkingDeltaEvent {
  type: "thinking_delta" | "thinking";
  content: string;
  source?: "provider" | "model_preamble" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  is_raw_provider_reasoning?: boolean;
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
  group_id?: string;
  step_id?: string;
  iteration_id?: string;
  phase?: "tool" | "approval" | "model" | "final" | "recover" | string;
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
}

export interface ToolOutputDeltaEvent {
  type: "tool_output_delta";
  id: string;
  output: string;
  stream?: "stdout" | "stderr" | string;
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
}

export interface SubagentDoneEvent {
  type: "subagent.done";
  subagent_id: string;
  summary?: string;
  error?: string;
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
  target_kind: "message" | "tool_call" | "artifact" | "subagent" | "budget";
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
}

export interface SessionTaskUpdateEvent {
  type: "task.update";
  session: RuntimeSessionSnapshot;
}

export type TaskUpdateEvent = TodoTaskUpdateEvent | SessionTaskUpdateEvent;

/**
 * PlanStep is still used by PlanStepUpdatedEvent (plan_step_updated is live).
 * PlanUpdateEvent and PlanEditCommand were removed (plan.update / plan.edit
 * are dead code with no backend emitter).
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

export interface SubagentCancelCommand {
  type: "subagent.cancel";
  subagent_id: string;
}

export interface InspectorFocusCommand {
  type: "inspector.focus";
  target_kind: "message" | "tool_call" | "artifact" | "subagent" | "budget";
  target_id: string;
}
