/**
 * Cell type system for Codex-like chat rendering.
 * Maps to MiniCode_AgentLoop_UI_Detailed_V3.md Section 4.
 */

import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import type { TurnActivityKind } from "../../lib/turn-projection";
import type { ActivityItem } from "../../agent-loop/activity-item";
import type { Citation } from "../../stores/types";

// ── Plan Step ───────────────────────────────────────────────────────

export interface PlanStep {
  id: string;
  title: string;
  description?: string;
  status: "pending" | "in_progress" | "completed" | "blocked" | "cancelled";
}

// ── Diff File Change ────────────────────────────────────────────────

export interface DiffFileChange {
  path: string;
  patch?: string;
  additions: number;
  deletions: number;
  changeType?: "created" | "updated" | "deleted";
  isLarge?: boolean;
  isTruncated?: boolean;
}

// ── Cell States ─────────────────────────────────────────────────────

export interface UserMessageCellState {
  kind: "user_message";
  id: string;
  content: string;
  attachments?: {
    id?: string;
    artifactId?: string;
    docId?: string;
    name: string;
    type: string;
    size?: number;
    dataUrl?: string;
  }[];
  createdAt: number;
  queueState?: "queued" | "cancelled";
  queuePosition?: number;
  queueMessageId?: string;
}

export interface StatusNoticeCellState {
  kind: "status_notice";
  id: string;
  tone: "info" | "warning" | "success" | "danger";
  title: string;
  message?: string;
  createdAt: number;
}

export interface TurnSummaryCellState {
  kind: "turn_summary";
  id: string;
  status: "running" | "completed" | "failed" | "interrupted";
  items: {
    kind: "command" | "diff" | "activity" | "error" | "source";
    label: string;
    detail?: string;
    tone?: "neutral" | "success" | "warning" | "danger";
  }[];
  createdAt: number;
}

export interface ActivityCellState {
  kind: "activity";
  id: string;
  activityKind: TurnActivityKind;
  title: string;
  subtitle?: string;
  status: "running" | "done" | "failed" | "interrupted";
  collapsed: boolean;
  toolCallRecords?: ToolCallRecord[];
  progress?: {
    current?: number;
    total?: number;
    text?: string;
  };
  skill?: SkillProcessMetadata;
  startedAt: number;
  completedAt?: number;
  canonical?: ActivityItem;
}

export interface SkillProcessMetadata {
  name?: string;
  triggerMode?: string;
  sourceLevel?: string;
  reason?: string;
  tokenEstimate?: number;
  content?: string;
}

export interface ActivityGroupCellState {
  kind: "activity_group";
  id: string;
  cells: ActivityCellState[];
  status: "running" | "done" | "failed" | "interrupted";
  collapsed: boolean;
  startedAt: number;
  completedAt?: number;
}

export interface PlanCellState {
  kind: "plan";
  id: string;
  planId?: string;
  title: string;
  status: "proposed" | "approved" | "executing" | "completed" | "cancelled";
  requiresApproval: boolean;
  steps: PlanStep[];
  markdownSource?: string;
  createdAt: number;
  updatedAt: number;
}

export interface ExecCellState {
  kind: "exec";
  id: string;
  command: string;
  cwd?: string;
  status: "pending_approval" | "running" | "success" | "failed" | "cancelled";
  exitCode?: number;
  stdoutPreview: string[];
  stderrPreview: string[];
  stdoutFull?: string;
  stderrFull?: string;
  durationMs?: number;
  collapsed: boolean;
  needsApproval?: boolean;
  createdAt: number;
  completedAt?: number;
}

export interface ExecGroupCellState {
  kind: "exec_group";
  id: string;
  cells: ExecCellState[];
  status: "running" | "done" | "failed";
  collapsed: boolean;
  startedAt: number;
  completedAt?: number;
}

export interface DiffCellState {
  kind: "diff";
  id: string;
  status: "created" | "updated";
  files: DiffFileChange[];
  summary: {
    added: number;
    deleted: number;
    modifiedFiles: number;
  };
  collapsed: boolean;
  createdAt: number;
}

export interface ErrorCellState {
  kind: "error";
  id: string;
  title: string;
  message: string;
  source?: "agent" | "tool" | "command" | "permission" | "network";
  recoverable: boolean;
  suggestedAction?: string;
  rawError?: string;
  createdAt: number;
}

export interface AssistantMarkdownCellState {
  kind: "assistant_markdown";
  id: string;
  messageId?: string;
  markdownSource: string;
  citations?: Citation[];
  phase: "final";
  copyable: boolean;
  isStreaming?: boolean; // ✅ 添加流式状态标志
  /** Origin of the reply text. Used for data-source attribution, not for
   * visual divergence in this phase. */
  source?: "reply" | "stream" | "fallback" | "partial";
  /** Attachments carried by a BriefTool (send_message) reply. Rendered as a
   * compact chip list below the answer; image attachments are previewable. */
  attachments?: AssistantReplyAttachment[];
  createdAt: number;
}

export interface AssistantReplyAttachment {
  /** Absolute or workspace-relative file path. */
  path: string;
  /** File size in bytes. */
  size: number;
  /** True for previewable image formats. */
  isImage: boolean;
}

export interface StreamingAssistantTailCellState {
  kind: "streaming_assistant_tail";
  id: string;
  partialMarkdown: string;
  updatedAt: number;
}

export interface StreamingAssistantNarrationCellState {
  kind: "streaming_assistant_narration";
  id: string;
  partialMarkdown: string;
  isStreaming: boolean;
  updatedAt: number;
  eventIndex?: number;
}

export interface ThinkingCellState {
  kind: "thinking";
  id: string;
  content: string;
  source: "model_preamble" | "post_tool" | "provider" | "reasoning" | "runtime";
  isRawProviderReasoning?: boolean;
  providerReasoningType?: string;
  phase?: string; // Optional phase indicator (e.g., "analyzing", "planning")
  isStreaming?: boolean;
  createdAt: number;
}

// ── Cell Union ──────────────────────────────────────────────────────

export type HistoryCellState =
  | UserMessageCellState
  | StatusNoticeCellState
  | TurnSummaryCellState
  | ActivityCellState
  | ActivityGroupCellState
  | PlanCellState
  | ExecCellState
  | ExecGroupCellState
  | DiffCellState
  | ErrorCellState
  | AssistantMarkdownCellState
  | StreamingAssistantTailCellState
  | StreamingAssistantNarrationCellState
  | ThinkingCellState;

// ── Turn State ──────────────────────────────────────────────────────

export interface ChatTurnState {
  id: string;
  userCell: UserMessageCellState | null;
  committedCells: Exclude<
    HistoryCellState,
    UserMessageCellState | StreamingAssistantTailCellState
  >[];
  activeCell: HistoryCellState | null;
  finalAnswerCell: AssistantMarkdownCellState | null;
  status: "streaming" | "completed" | "failed" | "interrupted";
  startedAt: number;
  completedAt?: number;
}

// ── Surface State ───────────────────────────────────────────────────

export interface ChatSurfaceState {
  turns: ChatTurnState[];
  isStreaming: boolean;
  currentTurnId: string | null;
}
