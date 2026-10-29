/**
 * Cell type system for MiniCode chat rendering.
 * Maps to MiniCode_AgentLoop_UI_Detailed_V3.md Section 4.
 */

import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import type { TurnActivityKind } from "../../lib/turn-projection";
import type { ArtifactPreview, ChatMessageSource, Citation, ComposerQuote, MessageContextRef, MessageUsage, ProgressContentBlock, SubagentState, ToolHistoryPage } from "../../stores/types";

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
  contextRefs?: MessageContextRef[];
  quotedMessage?: ComposerQuote | null;
  messageSource?: ChatMessageSource;
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
  processing?: boolean;
}

export interface GuardianReviewCellState {
  kind: "guardian_review";
  id: string;
  review: import("../../protocol/guardian-types").GuardianAssessment;
  status: "running" | "done";
  createdAt: number;
  segment?: number;
  segmentClosed?: boolean;
}
export interface GuardianWarningCellState { kind: "guardian_warning"; id: string; message: string; createdAt: number; }

export interface ContextCompactionCellState {
  kind: "context_compaction";
  id: string;
  status: "running" | "completed" | "interrupted";
  source?: string;
  createdAt: number;
  completedAt?: number;
}

export interface ActivityCellState {
  kind: "activity";
  id: string;
  activityKind: TurnActivityKind;
  title: string;
  subtitle?: string;
  status: "pending" | "pending_approval" | "running" | "done" | "partial" | "failed" | "interrupted";
  collapsed: boolean;
  automaticApprovalReviews?: import("../../protocol/guardian-types").GuardianAssessment[];
  guardianDeclined?: boolean;
  guardianPatchFiles?: string[];
  toolCallRecords?: ToolCallRecord[];
  outputText?: string;
  artifacts?: ArtifactPreview[];
  progress?: {
    current?: number;
    total?: number;
    text?: string;
    phase?: ProgressContentBlock["phase"];
    retryAttempt?: number;
    maxRetries?: number;
    retryAfterMs?: number;
    statusCode?: number;
    providerErrorCode?: string;
    providerState?: ProgressContentBlock["providerState"];
    errorMessage?: string;
    detail?: string;
    errorDetails?: string[];
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
  automaticApprovalReviews?: import("../../protocol/guardian-types").GuardianAssessment[];
  guardianDeclined?: boolean;
  command: string;
  commandActions?: ToolCallRecord["commandActions"];
  commandPresentation?: ToolCallRecord["commandPresentation"];
  executionStartedAt?: number;
  commandExecutionItemId?: string;
  originalCommand?: string;
  callSource?: ToolCallRecord["callSource"];
  artifacts?: ArtifactPreview[];
  cwd?: string;
  outputArtifact?: ArtifactPreview;
  background?: boolean;
  status: "pending" | "pending_approval" | "running" | "success" | "partial" | "failed" | "cancelled";
  exitCode?: number;
  stdoutPreview: string[];
  stderrPreview: string[];
  stdoutFull?: string;
  stderrFull?: string;
  diagnostic?: string;
  durationMs?: number;
  cleanupReceipt?: ToolCallRecord["cleanupReceipt"];
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
  historical?: boolean;
  source?: string;
  truncated?: boolean;
  deferredDiff?: { conversationId: string; messageId: string; turnId: string; revision?: number };
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
  providerReasoningType?: string;
  /** Legacy/untyped thinking is shown behind the timeline's “思考” control. */
  collapsible?: boolean;
  isStreaming?: boolean;
  createdAt: number;
  segment?: number;
  segmentClosed?: boolean;
}

export interface CollaborationCellEntry {
  agentId: string;
  agentLabel: string;
  agentIdentity?: string;
  agentStartedAt?: number;
  agentStatus?: SubagentState["status"];
  content?: string;
}

/** MiniCode-style transcript projection for parent/child control actions. The
 * runtime remains the source of truth; this is only a collapsible view over
 * existing subagent start, mailbox message, and terminal state. */
export interface CollaborationCellState {
  kind: "collaboration";
  id: string;
  action: "sent_message" | "closed" | "delegated" | "completed";
  status: "running" | "success" | "failed" | "partial" | "cancelled";
  background?: boolean;
  entries: CollaborationCellEntry[];
  error?: string;
  collapsed: boolean;
  createdAt: number;
  segment?: number;
  segmentClosed?: boolean;
}

// ── Cell Union ──────────────────────────────────────────────────────

export type HistoryCellState =
  | UserMessageCellState
  | StatusNoticeCellState
  | GuardianReviewCellState
  | GuardianWarningCellState
  | ContextCompactionCellState
  | ActivityCellState
  | ExecCellState
  | DiffCellState
  | ErrorCellState
  | AssistantMarkdownCellState
  | StreamingAssistantTailCellState
  | ThinkingCellState
  | CollaborationCellState;

// ── Turn State ──────────────────────────────────────────────────────

export type TurnWorkItem =
  | { kind: "process"; id: string; cells: Exclude<HistoryCellState, UserMessageCellState | StreamingAssistantTailCellState>[] }
  | { kind: "user"; id: string; cell: UserMessageCellState };

export interface ChatTurnState {
  asyncMessages?: import("../../stores/types").TextContentBlock[];
  id: string;
  /** Pure streaming receipts preserve the file evidence owned by this projection. */
  resourceKey?: object;
  toolPage?: ToolHistoryPage;
  turnId?: string;
  userCell: UserMessageCellState | null;
  /** Source-ordered activity and admitted input within this physical turn. */
  workItems?: TurnWorkItem[];
  committedCells: Exclude<
    HistoryCellState,
    UserMessageCellState | StreamingAssistantTailCellState
  >[];
  activeCell: HistoryCellState | null;
  finalAnswerCell: AssistantMarkdownCellState | null;
  status: "streaming" | "completed" | "partial" | "failed" | "interrupted";
  startedAt?: number;
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
