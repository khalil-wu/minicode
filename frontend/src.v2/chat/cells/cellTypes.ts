/**
 * Cell type system for MiniCode chat rendering.
 * Maps to MiniCode_AgentLoop_UI_Detailed_V3.md Section 4.
 */

import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import type { TurnActivityKind } from "../../lib/turn-projection";
import type { ArtifactPreview, Citation, MessageUsage, ProgressContentBlock } from "../../stores/types";

// ── Diff File Change ────────────────────────────────────────────────

export interface DiffFileChange {
  path: string;
  oldPath?: string;
  patch?: string;
  additions: number;
  deletions: number;
  changeType?: "created" | "updated" | "deleted" | "renamed";
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
  steeredIntoMessageId?: string;
}

export interface StatusNoticeCellState {
  kind: "status_notice";
  id: string;
  tone: "info" | "warning" | "success" | "danger";
  title: string;
  message?: string;
  createdAt: number;
}

export interface ActivityCellState {
  kind: "activity";
  id: string;
  activityKind: TurnActivityKind;
  title: string;
  subtitle?: string;
  status: "running" | "done" | "partial" | "failed" | "interrupted";
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
  segment?: number;
  segmentClosed?: boolean;
}

export interface SkillProcessMetadata {
  name?: string;
  triggerMode?: string;
  sourceLevel?: string;
  reason?: string;
  tokenEstimate?: number;
  content?: string;
}

export interface ExecCellState {
  kind: "exec";
  id: string;
  command: string;
  cwd?: string;
  background?: boolean;
  status: "pending_approval" | "running" | "success" | "partial" | "failed" | "cancelled";
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
  segment?: number;
  segmentClosed?: boolean;
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
  /** Number of authoritative file-mutating tool calls folded into this cell. */
  toolCallCount?: number;
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
  source?: "reply" | "stream" | "partial";
  /** Attachments carried by a BriefTool (send_message) reply. Rendered as a
   * compact chip list below the answer; image attachments are previewable. */
  attachments?: AssistantReplyAttachment[];
  /** Validated artifacts attached to this assistant message. */
  artifacts?: ArtifactPreview[];
  /** Dedicated image-generation lifecycle rendered in the answer position. */
  imageProgress?: ProgressContentBlock[];
  /** When image generation is interleaved with answer text, preserve the
   * provider's before/after ordering around the generated artifact. */
  markdownBeforeArtifacts?: string;
  markdownAfterArtifacts?: string;
  /** Terminal image-generation failure metadata owned by this assistant message. */
  failureMessage?: string;
  failureRecoverable?: boolean;
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

export interface ThinkingCellState {
  kind: "thinking";
  id: string;
  content: string;
  source: "commentary" | "model_preamble" | "post_tool" | "provider" | "reasoning" | "runtime";
  phase?: string; // Optional phase indicator (e.g., "analyzing", "planning")
  isStreaming?: boolean;
  createdAt: number;
  segment?: number;
  segmentClosed?: boolean;
}

export interface CollaborationCellEntry {
  agentId: string;
  agentLabel: string;
  content?: string;
}

/** MiniCode-style transcript projection for parent/child control actions. The
 * runtime remains the source of truth; this is only a collapsible view over
 * existing subagent start, mailbox message, and terminal state. */
export interface CollaborationCellState {
  kind: "collaboration";
  id: string;
  action: "sent_message" | "closed";
  status: "running" | "success" | "failed";
  entries: CollaborationCellEntry[];
  collapsed: boolean;
  createdAt: number;
  segment?: number;
  segmentClosed?: boolean;
}

// ── Cell Union ──────────────────────────────────────────────────────

export type HistoryCellState =
  | UserMessageCellState
  | StatusNoticeCellState
  | ActivityCellState
  | ExecCellState
  | DiffCellState
  | ErrorCellState
  | AssistantMarkdownCellState
  | StreamingAssistantTailCellState
  | ThinkingCellState
  | CollaborationCellState;

// ── Turn State ──────────────────────────────────────────────────────

export interface ChatTurnState {
  id: string;
  turnId?: string;
  userCell: UserMessageCellState | null;
  committedCells: Exclude<
    HistoryCellState,
    UserMessageCellState | StreamingAssistantTailCellState
  >[];
  activeCell: HistoryCellState | null;
  finalAnswerCell: AssistantMarkdownCellState | null;
  status: "streaming" | "completed" | "partial" | "failed" | "interrupted";
  startedAt: number;
  completedAt?: number;
  durationMs?: number;
  usage?: MessageUsage;
}

// ── Surface State ───────────────────────────────────────────────────

export interface ChatSurfaceState {
  turns: ChatTurnState[];
  isStreaming: boolean;
  currentTurnId: string | null;
}
