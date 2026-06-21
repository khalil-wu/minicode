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
  | "budget_update"
  | "budget.warning"
  // Conversation runtime
  | "conversation.hydration.updated"
  | "conversation.compaction.updated"
  | "conversation.summary.updated"
  | "goal.updated"
  | "conversation.list"
  | "conversation.switched"
  // LLM settings
  | "llm.model.updated";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (conversation domain)
// ──────────────────────────────────────────────────────────────────

export type ConversationClientCommandType =
  // Chat
  | "user_message"
  | "answer"
  | "interrupt"
  | "ping"
  // Approval flow
  | "approval"
  | "approval.respond"
  | "approval.file_diff"
  | "read_artifact"
  // Conversation lifecycle
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
  | "conversation.goal.set"
  | "conversation.worktree.cleanup"
  | "conversation.permission.rules.list"
  | "conversation.permission.rules.add"
  | "conversation.permission.rules.remove"
  | "permissions.content_rule.add"
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
  available_models?: string[];
  working_directory?: string | null;
}

export interface UserMessageCommand {
  type: "user_message";
  content: string;
  conversation_id?: string;
  workspace_root?: string;
  primaryFile?: string;
  activeTabPath?: string;
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
