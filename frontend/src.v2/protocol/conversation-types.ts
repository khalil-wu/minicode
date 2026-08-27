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
  | "interrupt"
  | "ping"
  // Approval flow
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
  | "memory.reset"
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
  conversation_id: string;
  used: number;
  limit: number;
  ledger?: Omit<ContextLedgerEvent, "type" | "conversation_id">;
}

export interface ContextCompactedEvent {
  type: "context_compacted";
  conversation_id: string;
  summary: string;
  before_tokens?: number;
  after_tokens?: number;
  retained_categories?: string[];
  ledger?: Omit<ContextLedgerEvent, "type" | "conversation_id">;
}

export interface ContextForkedEvent {
  type: "context_forked";
  conversation_id: string;
  fork_id: string;
  message_index: number;
  context_history_index: number;
  history_length: number;
  estimated_tokens: number;
  parent_conversation_id: string;
  branch_created: boolean;
  branch_activated: boolean;
  message_id?: string;
  created_at?: string;
  status?: string;
  branch_conversation_id?: string;
}

export type ContextLedgerCategoryPayload =
  | "system_runtime"
  | "guidelines"
  | "skills"
  | "files_attachments"
  | "history"
  | "tool_results"
  | "memory"
  | "compaction_summaries";

export interface ContextLedgerEntryPayload {
  category: ContextLedgerCategoryPayload;
  label: string;
  estimated_tokens: number;
  item_count: number;
  source_count: number;
  sources: string[];
}

export interface ContextLedgerEvent {
  type: "context_ledger";
  conversation_id: string;
  schema_version: 1;
  estimated_tokens: number;
  actual_tokens: number;
  compaction_count: number;
  native_attachment_tokens: number;
  native_attachment_count: number;
  entries: ContextLedgerEntryPayload[];
}

export interface ContextSideQueryResultEvent {
  type: "context_side_query_result";
  conversation_id: string;
  query: string;
  result: string;
  focus: string;
}

export interface BudgetUpdateEvent {
  type: "budget_update";
  conversation_id: string;
  used: number;
  total: number;
  breakdown: Record<string, number>;
}

export interface BudgetWarningEvent {
  type: "budget.warning";
  conversation_id: string;
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
  updated_at?: string;
  revision?: number;
}

export interface ConversationHydrationUpdatedEvent {
  type: "conversation.hydration.updated";
  conversation_id: string;
  is_hydrating: boolean;
}

export interface ConversationCompactionUpdatedEvent {
  type: "conversation.compaction.updated";
  conversation_id: string;
  state: "compacted";
  summary: string;
}

export interface ConversationSummaryUpdatedEvent {
  type: "conversation.summary.updated";
  conversation_id: string;
  summary: string;
  title: string;
  updated_at: string;
  memory_mode: "enabled" | "disabled" | "polluted";
  memory_polluted: boolean;
  memory_pollution_sources: string[];
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
  revision?: number;
  conversation_type?: "main" | "side_chat";
  title?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  memory_mode?: "enabled" | "disabled" | "polluted" | string | null;
  memory_polluted?: boolean;
  memory_pollution_sources?: string[];
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
  transcript?: ConversationTranscriptMessage[];
  messages?: ConversationTranscriptMessage[];
  context_snapshot?: Record<string, unknown>;
}

export interface ConversationListEvent {
  type: "conversation.list";
  inventory_instance_id?: string;
  inventory_revision?: number;
  conversation_id?: string | null;
  active_conversation_id?: string | null;
  conversations?: ConversationSummaryPayload[];
  active_conversation?: ConversationRecordPayload | null;
  session?: RuntimeSessionSnapshot | null;
  snapshot_at?: string;
}

export interface ConversationSwitchedEvent {
  type: "conversation.switched";
  conversation_id?: string | null;
  conversation?: ConversationRecordPayload | null;
  is_hydrating?: boolean;
  session?: RuntimeSessionSnapshot | null;
  snapshot_at?: string;
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
  reasoning_effort?: string | null;
  configured_reasoning_effort?: string | null;
  effective_reasoning_effort?: string | null;
  reasoning_effort_supported?: boolean;
  reasoning_effort_levels?: string[];
  context_window?: number;
  context_window_source?: string;
  context_window_verified?: boolean;
  max_context_window?: number;
  max_context_window_source?: string;
  max_context_window_verified?: boolean;
  max_output_tokens?: number;
  max_output_tokens_source?: string;
  max_output_tokens_verified?: boolean;
  default_reasoning_effort?: string;
  default_reasoning_summary?: string;
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
  permission_mode?: "plan" | "confirm" | "bypass" | "auto";
  agent_mode?: "build" | "plan" | "review" | "explore" | "subagent" | string;
  attachments?: Record<string, unknown>[];
  skills?: { name: string; path: string }[];
  plugins?: { config_name: string; path: string }[];
  assistant_message_id?: string;
  user_message_id?: string;
  retry_from_message_id?: string;
  queue_if_busy?: boolean;
  streaming_behavior?: "follow_up" | "steer";
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

export interface InterruptCommand {
  type: "interrupt";
  conversation_id?: string;
  turn_id?: string;
  message_id?: string;
  task_id?: string;
}

export interface PingCommand {
  type: "ping";
}

export interface ReadArtifactCommand {
  type: "read_artifact";
  artifact_id: string;
  conversation_id: string;
  request_id: string;
  purpose?: "preview" | "attachment";
}

export interface ConversationCreateCommand {
  type: "conversation.create";
  conversation_id?: string;
  title?: string;
  conversation_type?: "main" | "side_chat";
  /** @deprecated Use conversation_type. */
  side_chat?: boolean;
  git_isolated?: boolean;
  workspace_root?: string;
  permission_mode?: "plan" | "confirm" | "bypass" | "auto";
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

export interface ConversationMemoryModeSetCommand {
  type: "conversation.memory_mode.set";
  conversation_id?: string;
  memory_mode: "enabled" | "disabled";
}

export interface MemoryResetCommand {
  type: "memory.reset";
  confirmed: true;
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
