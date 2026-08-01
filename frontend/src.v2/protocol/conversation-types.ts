/**
 * Conversation lifecycle event types.
 *
 * Domain subset of the WebSocket protocol for events related to conversation
 * management: context window tracking, budget monitoring, conversation
 * CRUD operations, permission management, goal tracking, and LLM model
 * configuration.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

// ──────────────────────────────────────────────────────────────────
// Server event type strings (conversation domain)
// ──────────────────────────────────────────────────────────────────

import type { RuntimeSessionSnapshot } from "./streaming-types";

export type ConversationServerEventType =
  // Context lifecycle
  | "context_usage"
  | "context_compacted"
  | "context_forked"
  | "context_ledger"
  | "context_side_query_result"
  | "budget_update"
  | "budget.warning"
  // Conversation runtime
  | "conversation.hydration.updated"
  | "conversation.compaction.updated"
  | "conversation.summary.updated"
  | "goal.updated"
  | "conversation.list"
  | "conversation.switched"
  | "user_message.queue.updated"
  // LLM settings
  | "llm.model.updated";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (conversation domain)
// ──────────────────────────────────────────────────────────────────

export type ConversationClientCommandType =
  // Chat
  | "user_message"
  | "user_message.queue.cancel"
  | "user_message.queue.steer"
  | "answer"
  | "interrupt"
  | "ping"
  // Approval flow
  | "approval"
  | "approval.file_diff"
  | "read_artifact"
  // Conversation lifecycle
  | "conversation.create"
  | "conversation.clone"
  | "conversation.merge"
  | "conversation.export"
  | "conversation.switch"
  | "conversation.list"
  | "conversation.clear"
  | "conversation.truncate"
  | "conversation.delete"
  | "conversation.archive"
  | "conversation.unarchive"
  | "conversation.rename"
  | "conversation.memory_mode.set"
  | "conversation.permission_mode.set"
  | "conversation.goal.set"
  | "conversation.worktree.cleanup"
  | "conversation.worktree.handoff.preflight"
  | "conversation.worktree.handoff.execute"
  | "conversation.permission.rules.list"
  | "conversation.permission.rules.add"
  | "conversation.permission.rules.remove"
  | "permissions.content_rule.add"
  | "context.compact"
  | "context.fork"
  | "context.ledger"
  | "context.side_query"
  // LLM
  | "llm.model.set"
  | "llm.config.set";

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

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

export interface GoalInfo {
  id?: string;
  text?: string;
  status?: "active" | "paused" | string;
  created_at?: string;
  updated_at?: string;
  source?: string;
}

export interface GoalUpdatedEvent {
  type: "goal.updated";
  conversation_id?: string;
  goal: GoalInfo;
  source?: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (conversation domain)
// ──────────────────────────────────────────────────────────────────

export interface ConversationTranscriptMessage {
  id?: unknown;
  role?: unknown;
  content?: unknown;
  thinking?: unknown;
  blocks?: unknown;
  tool_calls?: unknown;
  toolCalls?: unknown;
  artifacts?: unknown;
  attachments?: unknown;
  attachmentRefs?: unknown;
  citations?: unknown;
  usage?: unknown;
  timestamp?: unknown;
  [key: string]: unknown;
}

export interface ConversationSummaryPayload {
  id: string;
  title?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  memory_mode?: string | null;
  permission_mode?: string | null;
  summary?: string | null;
  compaction_state?: string | null;
  message_count?: number;
  archived?: boolean;
  archived_at?: string | null;
  workspace_root?: string | null;
  git_branch?: string | null;
  worktree_path?: string | null;
  git_isolated?: boolean;
  goal?: GoalInfo | null;
  parent_conversation_id?: string | null;
  parent_message_index?: number | null;
  fork_id?: string | null;
  branch_kind?: string | null;
  merged_into_conversation_id?: string | null;
  merged_at?: string | null;
}

export interface ConversationRecordPayload extends ConversationSummaryPayload {
  permission_deny_rules?: string[];
  permission_overrides?: Record<string, string>;
  compaction_summary?: string | null;
  inherited_facts?: Record<string, unknown>[];
  local_facts?: Record<string, unknown>[];
  transcript?: ConversationTranscriptMessage[];
  messages?: ConversationTranscriptMessage[];
  context_snapshot?: Record<string, unknown>;
}

export interface ConversationListEvent {
  type: "conversation.list";
  conversation_id?: string | null;
  active_conversation_id?: string | null;
  conversations?: ConversationSummaryPayload[];
  active_conversation?: ConversationRecordPayload | null;
  session?: RuntimeSessionSnapshot | null;
}

export interface ConversationSwitchedEvent {
  type: "conversation.switched";
  conversation_id?: string | null;
  conversation?: ConversationRecordPayload | null;
  is_hydrating?: boolean;
  session?: RuntimeSessionSnapshot | null;
}

export interface LlmModelUpdatedEvent {
  type: "llm.model.updated";
  model?: string | null;
  current_model?: string | null;
  provider?: string | null;
  provider_id?: string | null;
  base_url?: string | null;
  wire_api?: string | null;
  available_models?: string[];
  models_source?: string;
  working_directory?: string | null;
}

export interface UserMessageQueueUpdatedEvent {
  type: "user_message.queue.updated";
  status: "queued" | "dequeued" | "cancelled";
  conversation_id: string;
  message_id: string;
  user_message_id?: string;
  position?: number;
  reason?: string;
  target_message_id?: string;
  turn_mode?: "follow_up" | "steer";
}

export interface UserMessageCommand {
  type: "user_message";
  content: string;
  conversation_id?: string;
  workspace_root?: string;
  primaryFile?: string;
  activeTabPath?: string;
  permission_mode?: "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits";
  agent_mode?: "build" | "plan" | "review" | "explore" | "subagent" | string;
  attachments?: Record<string, unknown>[];
  skills?: { name: string; path: string }[];
  plugins?: { config_name: string; path: string }[];
  assistant_message_id?: string;
  user_message_id?: string;
  queue_if_busy?: boolean;
}

export interface UserMessageQueueCancelCommand {
  type: "user_message.queue.cancel";
  conversation_id: string;
  message_id: string;
  user_message_id?: string;
}

export interface UserMessageQueueSteerCommand {
  type: "user_message.queue.steer";
  conversation_id: string;
  message_id: string;
  user_message_id?: string;
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

export interface ReadArtifactCommand {
  type: "read_artifact";
  artifact_id: string;
  purpose?: "preview" | "image_preview" | "attachment";
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

export interface ConversationTruncateCommand {
  type: "conversation.truncate";
  conversation_id: string;
  truncate_before_message_id: string;
  retained_message_ids?: string[];
}

export interface ConversationCloneCommand {
  type: "conversation.clone";
  conversation_id: string;
  title?: string;
  activate?: boolean;
}

export interface ConversationMergeCommand {
  type: "conversation.merge";
  conversation_id: string;
  target_conversation_id?: string;
}

export interface ConversationExportCommand {
  type: "conversation.export";
  conversation_id: string;
  include_descendants?: boolean;
}

/**
 * Fork a conversation from a visible transcript message.
 *
 * `message_id` is the authoritative selector. `message_index` is retained
 * only as a compatibility hint for older clients and test fixtures; the
 * backend resolves the stable id against the persisted transcript before
 * choosing the model-context boundary.
 */
export interface ContextForkCommand {
  type: "context.fork";
  message_id?: string;
  message_index?: number;
  create_branch?: boolean;
  activate?: boolean;
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

export interface ConversationWorktreeHandoffPreflightCommand {
  type: "conversation.worktree.handoff.preflight";
  conversation_id: string;
  target: "local" | "worktree";
  dirty_action?: "block" | "stash";
}

export interface ConversationWorktreeHandoffExecuteCommand {
  type: "conversation.worktree.handoff.execute";
  conversation_id: string;
  target: "local" | "worktree";
  fingerprint: string;
  dirty_action?: "block" | "stash";
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

export interface ConversationGoalSetCommand {
  type: "conversation.goal.set";
  conversation_id?: string;
  action?: "set" | "show" | "status" | "inspect" | "pause" | "resume" | "clear" | "delete" | "reset" | string;
  text?: string;
  goal?: string;
  source?: string;
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
