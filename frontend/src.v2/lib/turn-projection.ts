import type { ContentBlock } from "../stores/types";
import {
  isCompletedAgentMessageBlock,
  isFinalAnswerBlock,
} from "./content-blocks";
import type { ToolCallRecord } from "./tool-call-reducer";
import { isBrowserScreenshotRecord } from "./artifact-projection";
import { isProviderRequestProgress, providerProgressLabel } from "./provider-progress";

export type TurnActivityKind =
  | "reasoning"
  | "planning"
  | "processNote"
  | "providerReasoning"
  | "agentMessage"
  | "webSearch"
  | "workspaceSearch"
  | "workspaceList"
  | "fileRead"
  | "commandExecution"
  | "fileChange"
  | "mcpToolCall"
  | "browser"
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
  /** Semantic work segment. Invisible commentary/reasoning/final-answer
   * boundaries advance this value without becoming transcript cells. */
  segment?: number;
  /** True once a later semantic boundary has closed this segment. */
  segmentClosed?: boolean;
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
  "workspaceList",
  "fileRead",
  "commandExecution",
  "fileChange",
  "mcpToolCall",
  "browser",
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

/**
 * Canonical MiniCode tool-to-timeline classification.
 *
 * Every projection path, including persisted multi-record envelopes, must use
 * this function.  Rendering code must never reinterpret resultKind or stale
 * activityKind metadata independently.
 */
export const activityKindFromToolRecord = (record: ToolCallRecord): TurnActivityKind => {
  const declared = String(record.activityKind || "").trim();
  const name = String(record.name || "").trim().toLowerCase();
  // A screenshot is a first-class browser result.  Persisted records from
  // before the browser lane was introduced can retain an unrelated activity
  // declaration (for example `fileRead`) even though the result carries
  // browser screenshot evidence.  Let the evidence repair that stale label
  // before honoring the old declaration; otherwise the image may be visible
  // while the timeline still presents it as the wrong operation.
  if (isBrowserScreenshotRecord(record)) return "browser";
  // list_files historically arrived with the broad workspaceSearch metadata.
  // The operation name is the canonical discriminator for this one built-in,
  // so it must win before the broad declaration is accepted.
  if (name === "list_files") return "workspaceList";
  if (declared && declared !== "genericTool" && ACTIVITY_KINDS.has(declared as TurnActivityKind)) {
    return declared as TurnActivityKind;
  }

  // Result metadata is the authoritative completion-side classification when
  // an older stream omitted activity_kind on tool_call and supplied it only on
  // tool_result. Normalize that one canonical shape here so the UI does not
  // fall back to a generic disclosure row after the result commits.
  const resultKind = String(record.resultKind || "").trim().toLowerCase();
  if (resultKind === "file") {
    if (name === "list_files") return "workspaceList";
    if (["grep_files", "glob_files", "search_files"].includes(name)) return "workspaceSearch";
    return "fileRead";
  }
  if (resultKind === "edit") return "fileChange";
  if (resultKind === "command") return "commandExecution";
  if (resultKind === "web" || resultKind === "search") return "webSearch";
  if (resultKind === "browser" || resultKind === "preview") return "browser";

  // These are MiniCode's canonical built-in tool names. Older event streams
  // may omit both projection fields, but the exact name still identifies the
  // same built-in lifecycle and must not degrade into a generic row.
  if (["read_file", "read_artifact"].includes(name)) return "fileRead";
  if (["grep_files", "glob_files", "search_files"].includes(name)) return "workspaceSearch";
  if (["web_fetch", "webfetch", "web_search", "websearch"].includes(name)) return "webSearch";
  if (["browser_control", "browser", "computer"].includes(name)) return "browser";
  if (["run_command", "shell_command", "bash"].includes(name)) return "commandExecution";
  if (["write_file", "edit_file", "apply_patch"].includes(name)) return "fileChange";
  return "genericTool";
};

const toolItem = (
  block: Extract<ContentBlock, { type: "tool_call" }>,
  segment: number,
): TurnActivityItem => {
  const record = block.record;
  const kind = activityKindFromToolRecord(record);
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
    segment,
    segmentClosed: false,
  };
};

const progressItem = (
  block: Extract<ContentBlock, { type: "progress" }>,
  segment: number,
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
  title: providerProgressLabel(block) || block.label || block.message,
  summary: block.summary,
  hasFailure: block.status === "failed",
  hasPendingUserAction: block.stage === "approval" && block.status === "running",
  segment,
  segmentClosed: false,
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
  if (includeHidden) return true;
  const visibility = block.type === "tool_call"
    ? block.record.visibility
    : "visibility" in block
      ? block.visibility
      : undefined;
  if (visibility == null || visibility === "") {
    return true;
  }
  // Only the public activity projections are renderable. Unknown values from
  // extensions or stale persisted data fail closed instead of becoming UI.
  return visibility === "timeline" || visibility === "compact";
};

const compactToolName = (value: string): string =>
  value.toLowerCase().replace(/[^a-z0-9]+/g, "");

const toolNameAliases = (toolNames: Set<string>): Set<string> => {
  const aliases = new Set<string>();
  for (const name of toolNames) {
    const parts = name.split(/__|[./:\\]+/).filter(Boolean);
    aliases.add(compactToolName(name));
    for (let index = 0; index < parts.length; index += 1) {
      aliases.add(compactToolName(parts.slice(index).join("")));
    }
  }
  aliases.delete("");
  return aliases;
};

const isAliasSequence = (value: string, aliases: Set<string>): boolean => {
  const compact = compactToolName(value);
  if (!compact) return true;
  const reachable = new Array<boolean>(compact.length + 1).fill(false);
  reachable[0] = true;
  for (let index = 0; index < compact.length; index += 1) {
    if (!reachable[index]) continue;
    for (const alias of aliases) {
      if (compact.startsWith(alias, index)) {
        reachable[index + alias.length] = true;
      }
    }
  }
  return reachable[compact.length];
};

const isToolProtocolSummary = (content: string, toolNames: Set<string>): boolean => {
  let text = content.trim();
  const aliases = toolNameAliases(toolNames);
  if (!text || aliases.size === 0) return false;
  const fenced = text.match(/^```(?:[a-z0-9_-]+)?[ \t]*\r?\n([\s\S]*?)\r?\n?```$/i);
  if (fenced) text = fenced[1].trim();
  if (!text || !/^[A-Za-z0-9_./:\\,\-\s`]+$/.test(text)) return false;
  const tokens = (text.replace(/`/g, "").match(/[A-Za-z0-9_./:\\-]+/g) ?? [])
    .filter((token) => Boolean(compactToolName(token)));
  return tokens.length > 0 && tokens.every((token) => isAliasSequence(token, aliases));
};

/** True when process narration carries no word at all (e.g. a bare "..."). */
const isContentFreeNarration = (content: string): boolean =>
  !/[\p{L}\p{N}]/u.test(content);

/**
 * The turn plan owns a dedicated live surface (TurnPlanProgress), so a plan
 * write is not timeline work. cc's TodoWriteTool renders nothing for the same
 * reason (`renderToolUseMessage` returns null); projecting it here produced a
 * run of identical, content-free "Update plan" rows beside the real widget.
 */
const isPlanStateWrite = (record: ToolCallRecord): boolean =>
  String(record.resultKind || "").trim().toLowerCase() === "plan";

const VISIBLE_NARRATION_SOURCES = new Set([
  "pending",
  "commentary",
  "model_preamble",
  "post_tool",
  // Text the user interrupted mid-flight. It is not an answer, but it was on
  // screen while streaming, so dropping it would erase what the model had
  // already said.
  "cancelled",
]);

const narrationItem = (
  block: Extract<ContentBlock, { type: "text" }>,
  index: number,
  segment: number,
  status: TurnActivityStatus,
): TurnActivityItem => ({
  id: block.itemId || `commentary-${index}`,
  kind: "processNote",
  blocks: [block],
  content: block.content,
  source: block.source,
  status,
  hasFailure: status === "failed",
  hasPendingUserAction: false,
  segment,
  segmentClosed: false,
});

export function projectTurn(
  blocks: ContentBlock[],
  options: ProjectTurnOptions = {},
): TurnProjection {
  const typedToolIds = new Set(
    blocks.flatMap((block) => block.type === "tool_call" ? [block.record.id] : []),
  );
  const typedToolNames = new Set(
    blocks.flatMap((block) => block.type === "tool_call"
      ? [String(block.record.name || "").trim().toLowerCase()].filter(Boolean)
      : []),
  );
  const finalBlocks = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "text" }> =>
      isFinalAnswerBlock(block)
      && block.type === "text"
  );
  const selectedFinal = finalBlocks.at(-1);
  // CC keeps each max-output continuation as another assistant segment. Join
  // all completed/partial agent-message items so a recovered answer does not
  // visually lose the portion emitted before the output boundary.
  const finalAnswer = finalBlocks.map((block) => block.content).join("");
  const finalAnswerSource = selectedFinal?.source;
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
  let segment = 0;
  blocks.forEach((block, index) => {
    if (block.type === "tool_call") {
      if (!isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      if (!options.includeHiddenActivity && isPlanStateWrite(block.record)) return;
      if (!block.record.temporaryRemoved) activityItems.push(toolItem(block, segment));
      return;
    }
    if (block.type === "progress") {
      // Older persisted turns can contain an agent.progress mirror for a
      // typed tool call. The tool lifecycle is authoritative.
      if (block.toolCallId && typedToolIds.has(block.toolCallId)) return;
      // Codex keeps provider request/retry lifecycle in its transient status
      // surface, never as a transcript item. Keep the same boundary here;
      // typed tool activity (MCP/image) is not classified as this lifecycle.
      if (isProviderRequestProgress(block)) return;
      if (!isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      activityItems.push(progressItem(block, segment));
      return;
    }
    if (block.type === "thinking") {
      if (!block.content.trim() || !isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      if (isToolProtocolSummary(block.content, typedToolNames)) return;
      const isLiveProviderReasoning = ["provider", "reasoning"].includes(String(block.source || ""))
        && index === activeThinkingIndex;
      segment += 1;
      if (isLiveProviderReasoning) {
        activityItems.push({
          id: `thinking-${index}`,
          kind: thinkingKind(block),
          blocks: [block],
          content: block.content,
          source: block.source,
          phase: block.phase,
          status: "running",
          hasFailure: false,
          hasPendingUserAction: false,
          segment,
          segmentClosed: false,
        });
      }
      segment += 1;
      return;
    }
    if (block.type === "process") {
      if (block.status === "retracted") return;
      if (!block.content.trim() || !isVisibleActivity(block, Boolean(options.includeHiddenActivity))) return;
      // Chat Completions models sometimes emit a bare "..." as a pre-tool
      // placeholder. It carries no user-facing progress and should not become
      // a visible timeline row between real tool groups.
      if (isContentFreeNarration(block.content)) return;
      if (isToolProtocolSummary(block.content, typedToolNames)) return;
      if (
        block.itemKind === "process_text"
        && ["model", "commentary", "model_preamble", "post_tool", "runtime"].includes(String(block.source || ""))
      ) {
        segment += 1;
      }
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
        segment,
        segmentClosed: false,
      });
      return;
    }
    if (block.type === "text" && block.isStreaming === true) {
      const source = String(block.source || "");
      if (VISIBLE_NARRATION_SOURCES.has(source) && block.content.trim()) {
        segment += 1;
        activityItems.push(narrationItem(block, index, segment, "running"));
      } else if (source === "runtime") {
        segment += 1;
      }
      return;
    }
    if (block.type === "text" && VISIBLE_NARRATION_SOURCES.has(String(block.source || ""))) {
      if (!block.content.trim()) return;
      if (isToolProtocolSummary(block.content, typedToolNames)) return;
      segment += 1;
      activityItems.push(narrationItem(
        block,
        index,
        segment,
        block.status === "partial"
          ? "partial"
          : block.status === "cancelled"
            ? "cancelled"
            : "completed",
      ));
      return;
    }
    if (block.type === "text" && isCompletedAgentMessageBlock(block) && !isFinalAnswerBlock(block)) {
      if (!block.content.trim() || block.source === "cancelled") return;
      if (isContentFreeNarration(block.content)) return;
      if (isToolProtocolSummary(block.content, typedToolNames)) return;
      segment += 1;
      return;
    }
    if (block.type === "text" && isFinalAnswerBlock(block)) segment += 1;
  });

  for (const item of activityItems) {
    item.segmentClosed = (item.segment ?? 0) < segment;
  }

  const terminalFailed = options.terminalStatus === "failed";
  const terminalPartial = options.terminalStatus === "partial";
  const terminalInterrupted = options.terminalStatus === "interrupted";
  const hasActivityFailure = activityItems.some((item) => item.hasFailure);
  const explicitDurations = activityItems
    .map((item) => Number(item.durationMs ?? 0))
    .filter((value) => Number.isFinite(value) && value >= 0);
  const starts = activityItems
    .map((item) => Number(item.startedAt))
    .filter((value) => Number.isFinite(value));
  const finishes = activityItems
    .map((item) => Number(item.finishedAt))
    .filter((value) => Number.isFinite(value));
  // A turn duration is the elapsed span of the turn, not the longest single
  // tool.  Keep the legacy item-duration fallback for older persisted blocks
  // that do not carry lifecycle timestamps.
  const durationMs = starts.length && finishes.length
    ? Math.max(0, Math.max(...finishes) - Math.min(...starts))
    : explicitDurations.reduce((max, value) => Math.max(max, value), 0);
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
