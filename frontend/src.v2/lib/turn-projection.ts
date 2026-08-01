import type { ContentBlock } from "../stores/types";
import { getAnswerTextFromBlocks, isCompletedAgentMessageBlock, isFinalAnswerBlock } from "./content-blocks";
import type { ToolCallRecord } from "./tool-call-reducer";

export type TurnActivityKind =
  | "reasoning"
  | "planning"
  | "processNote"
  | "providerReasoning"
  | "agentMessage"
  | "webSearch"
  | "workspaceSearch"
  | "fileRead"
  | "commandExecution"
  | "fileChange"
  | "mcpToolCall"
  | "genericTool"
  | "skill"
  | "progress";

export type TurnActivityStatus =
  | "running"
  | "completed"
  | "failed"
  | "blocked"
  | "pending"
  | "partial"
  | "timeout"
  | "cancelled"
  | "info";

export interface TurnActivityItem {
  id: string;
  kind: TurnActivityKind;
  blocks: ContentBlock[];
  status: TurnActivityStatus;
  content?: string;
  source?: string;
  isRawProviderReasoning?: boolean;
  providerReasoningType?: string;
  phase?: string;
  itemKind?: string;
  records?: ToolCallRecord[];
  progress?: Extract<ContentBlock, { type: "progress" }>[];
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  skillName?: string;
  triggerMode?: string;
  sourceLevel?: string;
  reason?: string;
  tokenEstimate?: number;
  title?: string;
  summary?: string;
  hasFailure: boolean;
  hasPendingUserAction: boolean;
}

export interface TurnProjection {
  activityItems: TurnActivityItem[];
  finalAnswer: string;
  finalAnswerSource?: string;
  status: "streaming" | "completed" | "partial" | "failed" | "interrupted" | "empty";
  durationMs: number;
  hasFailure: boolean;
  hasPendingUserAction: boolean;
}

export interface ProjectTurnOptions {
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
  terminalStatus?: "completed" | "partial" | "failed" | "interrupted";
  includeHiddenActivity?: boolean;
}

const ACTIVITY_KINDS = new Set<TurnActivityKind>([
  "reasoning",
  "planning",
  "processNote",
  "providerReasoning",
  "agentMessage",
  "webSearch",
  "workspaceSearch",
  "fileRead",
  "commandExecution",
  "fileChange",
  "mcpToolCall",
  "genericTool",
  "skill",
  "progress",
]);

const toolStatus = (record: ToolCallRecord): TurnActivityStatus => {
  if (record.status === "success") return "completed";
  return record.status;
};

export const activityStatusFromToolRecords = (records: ToolCallRecord[]): TurnActivityStatus => {
  if (records.some((record) => record.status === "running")) return "running";
  if (records.some((record) => record.status === "pending")) return "pending";
  if (records.some((record) => record.status === "failed")) return "failed";
  if (records.some((record) => record.status === "timeout")) return "timeout";
  if (records.some((record) => record.status === "blocked")) return "blocked";
  if (records.some((record) => record.status === "cancelled")) return "cancelled";
  if (records.some((record) => record.status === "partial")) return "partial";
  return "completed";
};

const toolKind = (record: ToolCallRecord): TurnActivityKind => {
  const value = String(record.activityKind || "");
  return ACTIVITY_KINDS.has(value as TurnActivityKind)
    ? value as TurnActivityKind
    : "genericTool";
};

const toolItem = (
  block: Extract<ContentBlock, { type: "tool_call" }>,
): TurnActivityItem => {
  const record = block.record;
  const kind = toolKind(record);
  return {
    id: record.id,
    kind,
    blocks: [block],
    records: [record],
    status: toolStatus(record),
    title: record.status === "running" || record.status === "pending"
      ? record.displayHint
      : record.displaySummary || record.displayHint,
    // The result summary is model-facing and may contain safety envelopes.
    // Keep the compact timeline on typed, user-facing metadata only.
    summary: record.inputSummary || record.userSummary || record.sourceUrl || "",
    startedAt: record.startedAt,
    finishedAt: record.finishedAt,
    durationMs: record.durationMs,
    hasFailure: ["failed", "blocked", "timeout"].includes(record.status),
    hasPendingUserAction: false,
  };
};

const progressItem = (
  block: Extract<ContentBlock, { type: "progress" }>,
): TurnActivityItem => ({
  id: block.id,
  kind: block.stage === "planning" ? "planning" : "progress",
  blocks: [block],
  progress: [block],
  status: block.status === "running"
    ? "running"
    : block.status === "failed"
      ? "failed"
      : block.status === "partial"
        ? "partial"
        : block.status === "completed"
          ? "completed"
          : "info",
  title: block.label || block.message,
  summary: block.summary,
  hasFailure: block.status === "failed",
  hasPendingUserAction: block.stage === "approval" && block.status === "running",
});

const thinkingKind = (
  block: Extract<ContentBlock, { type: "thinking" }>,
): Extract<TurnActivityKind, "reasoning" | "processNote" | "providerReasoning"> => {
  if (block.source === "provider") return "providerReasoning";
  if (["model_preamble", "post_tool", "runtime"].includes(String(block.source || ""))) {
    return "processNote";
  }
  return "reasoning";
};

const isVisibleActivity = (block: ContentBlock, includeHidden: boolean): boolean => {
  if (includeHidden || !("visibility" in block) || block.visibility !== "debug") return true;
  // Older transcripts tagged provider-authored thinking as debug. Keep
  // internal diagnostics hidden while restoring the model's thinking blocks.
  return block.type === "thinking"
    && (block.source === "provider" || block.is_raw_provider_reasoning === true);
};

const finalTextIndex = (blocks: ContentBlock[]): number | undefined => {
  const explicit = blocks.flatMap((block, index) => isFinalAnswerBlock(block) ? [index] : []);
  return explicit.at(-1);
};

export function projectTurn(
  blocks: ContentBlock[],
  options: ProjectTurnOptions = {},
): TurnProjection {
  const selectedFinalIndex = finalTextIndex(blocks);
  const selectedFinal = selectedFinalIndex == null
    ? undefined
    : blocks[selectedFinalIndex];
  // CC keeps each max-output continuation as another assistant segment. Join
  // all completed/partial agent-message items so a recovered answer does not
  // visually lose the portion emitted before the output boundary.
  const finalAnswer = getAnswerTextFromBlocks(blocks);
  const finalAnswerSource = selectedFinal?.type === "text" ? selectedFinal.source : undefined;
  const typedToolIds = new Set(
    blocks.flatMap((block) => block.type === "tool_call" ? [block.record.id] : []),
  );
  let activeThinkingIndex = -1;
  if (options.isThinkingStreaming) {
    for (let index = blocks.length - 1; index >= 0; index -= 1) {
      const block = blocks[index];
      if (block?.type === "thinking" && block.content.trim()) {
        activeThinkingIndex = index;
        break;
      }
    }
  }
  const activityItems: TurnActivityItem[] = [];
  blocks.forEach((block, index) => {
    if (block.type === "tool_call") {
      if (!isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      if (!block.record.temporaryRemoved) activityItems.push(toolItem(block));
      return;
    }
    if (block.type === "progress") {
      // Older persisted turns can contain an agent.progress mirror for a
      // typed tool call. The tool lifecycle is authoritative.
      if (block.toolCallId && typedToolIds.has(block.toolCallId)) return;
      if (!isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      activityItems.push(progressItem(block));
      return;
    }
    if (block.type === "thinking") {
      if (!block.content.trim() || !isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      activityItems.push({
        id: `thinking-${index}`,
        kind: thinkingKind(block),
        blocks: [block],
        content: block.content,
        source: block.source,
        isRawProviderReasoning: block.is_raw_provider_reasoning,
        providerReasoningType: block.provider_reasoning_type,
        phase: block.phase,
        status: index === activeThinkingIndex ? "running" : "completed",
        hasFailure: false,
        hasPendingUserAction: false,
      });
      return;
    }
    if (block.type === "process") {
      if (!block.content.trim() || !isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      const status: TurnActivityStatus = block.status === "running"
        ? "running"
        : block.status === "failed"
          ? "failed"
          : block.status === "partial"
            ? "partial"
            : block.status === "info"
              ? "info"
              : "completed";
      activityItems.push({
        id: block.id || `process-${index}`,
        kind: block.itemKind === "skill" ? "skill" : "processNote",
        blocks: [block],
        content: block.content,
        source: block.source,
        itemKind: block.itemKind,
        title: block.title,
        summary: block.summary,
        skillName: block.skillName,
        triggerMode: block.triggerMode,
        sourceLevel: block.sourceLevel,
        reason: block.reason,
        tokenEstimate: block.tokenEstimate,
        status,
        startedAt: block.timestamp,
        finishedAt: status === "running" ? undefined : block.timestamp,
        hasFailure: status === "failed",
        hasPendingUserAction: false,
      });
      return;
    }
    if (block.type === "text" && isCompletedAgentMessageBlock(block) && !isFinalAnswerBlock(block)) {
      if (!block.content.trim() || block.source === "cancelled") return;
      activityItems.push({
        id: block.itemId || `agent-message-${index}`,
        kind: "processNote",
        blocks: [block],
        content: block.content,
        source: block.source,
        status: block.status === "partial" ? "partial" : "completed",
        hasFailure: false,
        hasPendingUserAction: false,
      });
    }
  });

  const terminalFailed = options.terminalStatus === "failed";
  const terminalPartial = options.terminalStatus === "partial";
  const terminalInterrupted = options.terminalStatus === "interrupted";
  const hasActivityFailure = activityItems.some((item) => item.hasFailure);
  const durationMs = activityItems.reduce((max, item) => Math.max(max, item.durationMs ?? 0), 0);
  const status: TurnProjection["status"] = options.isStreaming
    ? "streaming"
    : terminalFailed
      ? "failed"
      : terminalInterrupted
        ? "interrupted"
        : terminalPartial
          ? "partial"
          : finalAnswer.trim() || activityItems.length
            ? "completed"
            : "empty";

  return {
    activityItems,
    finalAnswer,
    finalAnswerSource,
    status,
    durationMs,
    hasFailure: terminalFailed || hasActivityFailure,
    hasPendingUserAction: activityItems.some((item) => item.hasPendingUserAction),
  };
}
