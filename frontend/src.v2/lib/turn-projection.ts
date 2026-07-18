import type { ContentBlock } from "../stores/types";
import { isAgentControlToolName, type ToolCallRecord } from "./tool-call-reducer";
import { displayScopeOf, requiresAttention, shouldShowInMainChat, type DisplayRoutable } from "./display-intent";

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

export type TurnActivityStatus = "running" | "completed" | "failed" | "blocked" | "pending" | "partial" | "timeout" | "info";

export interface TurnActivityItem {
  id: string;
  kind: TurnActivityKind;
  blocks: ContentBlock[];
  status: TurnActivityStatus;
  content?: string;
  source?: string;
  itemKind?: string;
  records?: ToolCallRecord[];
  progress?: Extract<ContentBlock, { type: "progress" }>[];
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  queryCount?: number;
  pageCount?: number;
  sourceCount?: number;
  provider?: string;
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

export interface TurnHeadline {
  title: string;
  items: string[];
  cta: "review" | "sources" | "open" | null;
  diffStats: { plus: number; minus: number; files: number };
  sourceCount: number;
  artifactCount: number;
  hasReviewableChanges: boolean;
}

export interface TurnProjection {
  activityItems: TurnActivityItem[];
  finalAnswer: string;
  /** Origin of the final answer text. "reply" = explicit BriefTool reply;
   * "stream" (or undefined) = final-answer text streamed after tool work. */
  finalAnswerSource?: string;
  /** True when the turn contains a `reply` (BriefTool) call. Used to
   * collapse preamble text since the BriefTool reply is the visible answer. */
  hasSendMessage: boolean;
  status: "streaming" | "completed" | "failed" | "empty";
  durationMs: number;
  hasFailure: boolean;
  hasPendingUserAction: boolean;
  headline: TurnHeadline;
}

export interface ProjectTurnOptions {
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
  terminalStatus?: "completed" | "failed";
  sourceCount?: number;
  artifactCount?: number;
  includeHiddenActivity?: boolean;
}

const isRunningTool = (record: ToolCallRecord): boolean =>
  record.status === "running" || record.status === "pending";

const isUsableTool = (record: ToolCallRecord): boolean =>
  record.status === "success" || record.status === "partial";

const isFailedTool = (record: ToolCallRecord): boolean =>
  (record.status === "failed" || record.status === "blocked" || record.status === "timeout") && !isNonFatalToolIssue(record);

const isNonFatalToolIssue = (record: ToolCallRecord): boolean => {
  if (record.projection === "silent" || record.projection === "status" || record.projection === "warning") {
    return true;
  }
  if (
    record.errorKind === "missing_generated_content" ||
    record.errorKind === "routing_error" ||
    record.errorKind === "stale_evidence" ||
    record.errorKind === "repeat_guard" ||
    record.errorKind === "tool_disabled"
  ) {
    return true;
  }
  const text = `${record.summary || ""} ${record.displaySummary || ""}`;
  if (/Search budget reached|Skipped another similar web search|Use the available results|answer with uncertainty/i.test(text)) {
    return true;
  }
  if (record.name === "preview_server" && /No preview launch configuration found|no preview launch configuration/i.test(text)) {
    return true;
  }
  if (isWebTool(record)) {
    return true;
  }
  return false;
};

const resultKindForRecord = (record: ToolCallRecord): string =>
  String(record.resultKind || "").toLowerCase();

const isWebTool = (record: ToolCallRecord): boolean =>
  ["search", "web"].includes(resultKindForRecord(record)) ||
  /^(?:web_search|web_fetch|search_web)$/i.test(record.name);

const isWebSearchAction = (record: ToolCallRecord): boolean =>
  resultKindForRecord(record) === "search" || /^(?:web_search|search_web)$/i.test(record.name);

const isWebFetchAction = (record: ToolCallRecord): boolean =>
  resultKindForRecord(record) === "web" || /^web_fetch$/i.test(record.name);

const isWorkspaceSearchTool = (name: string): boolean =>
  /^(?:grep|grep_files|glob|glob_files|fuzzy_search)$/i.test(name);

const isFileReadTool = (name: string): boolean =>
  /^(?:read_file|read_artifact|list_files)$/i.test(name);

const isLocalExplorationTool = (name: string): boolean =>
  isFileReadTool(name) || isWorkspaceSearchTool(name);

const isRecoverableLocalExplorationFailure = (record: ToolCallRecord): boolean => {
  if (record.status !== "failed" && record.status !== "blocked") return false;
  if (!isLocalExplorationTool(record.name)) return false;
  const text = `${record.summary || ""} ${record.displaySummary || ""} ${record.userSummary || ""}`;
  return !/blocked by policy|always deny|permission denied|unauthorized/i.test(text);
};

const isCommandTool = (name: string): boolean =>
  /(?:run_command|terminal|shell|bash|powershell|cmd)/i.test(name);

const isFileChangeTool = (name: string): boolean =>
  name !== "todo_write" && /(?:write|edit|patch|delete|remove|create|move|rename|save)/i.test(name);

const fileChangeTarget = (record: ToolCallRecord): string => {
  const value = record.args.file_path ?? record.args.path ?? record.args.target ?? record.args.filename;
  return typeof value === "string" ? value : "";
};

const hasFileChangeTarget = (record: ToolCallRecord): boolean => Boolean(fileChangeTarget(record).trim());

const isTurnActivityKind = (value: string): value is TurnActivityKind =>
  [
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
  ].includes(value);

/**
 * Determine the activity kind for a tool record.
 *
 * Preference order: backend activityKind → resultKind → name-based fallback.
 * Name-based patterns are only for backward compatibility with old transcripts
 * that may lack structured projection fields, and for disambiguating tools
 * that share the same resultKind (e.g. file_read vs workspace_search both
 * have resultKind="file").
 */
const kindForTool = (record: ToolCallRecord): TurnActivityKind | null => {
  // Tools that are never shown as activity items
  if (record.name === "ask_user") return null;
  if (record.name === "todo_write" || record.name === "todo_read") return null;

  // 1. Prefer backend-provided activityKind (set after tool_result event)
  if (record.activityKind && isTurnActivityKind(record.activityKind)) return record.activityKind;

  // 2. Use structured resultKind (always set, even during prepare phase)
  const resultKind = resultKindForRecord(record);
  if (resultKind === "search" || resultKind === "web") return "webSearch";
  if (resultKind === "command") return "commandExecution";
  if (resultKind === "edit") {
    return hasFileChangeTarget(record) || record.diff ? "fileChange" : "genericTool";
  }
  if (resultKind === "mcp") return "mcpToolCall";
  if (resultKind === "file") {
    // Backend uses resultKind="file" for both file-read and workspace-search
    // tools; disambiguate using the tool name until the backend provides a
    // more specific resultKind.
    if (isFileReadTool(record.name)) return "fileRead";
    return "workspaceSearch";
  }

  // 3. Last-resort name-based fallback for tools with resultKind="generic"
  //    or missing structured fields (old transcripts).
  if (isWebTool(record)) return "webSearch";
  if (isCommandTool(record.name)) return "commandExecution";
  if (isFileChangeTool(record.name) || record.diff) {
    return hasFileChangeTarget(record) || record.diff ? "fileChange" : "genericTool";
  }
  if (record.name.startsWith("mcp__")) return "mcpToolCall";
  if (isWorkspaceSearchTool(record.name)) return "workspaceSearch";
  if (isFileReadTool(record.name)) return "fileRead";
  return "genericTool";
};

const progressStatus = (record: Extract<ContentBlock, { type: "progress" }>): TurnActivityStatus => {
  if (record.status === "running") return "running";
  if (record.status === "failed") return "failed";
  if (record.status === "completed") return "completed";
  return "info";
};

const isGenericProgressPlaceholder = (block: Extract<ContentBlock, { type: "progress" }>): boolean => {
  const visibleParts = [
    block.label,
    block.message,
    block.summary,
    block.detail,
  ].map((part) => part?.trim()).filter(Boolean) as string[];
  if (visibleParts.length === 0) return true;
  return visibleParts.every(isGenericProcessPlaceholder);
};

const isProgressMirroredByTool = (
  block: Extract<ContentBlock, { type: "progress" }>,
  items: TurnActivityItem[],
): boolean => {
  const toolName = block.toolName || block.label;
  if (block.stage !== "tool" || !toolName) return false;
  return items.some((item) =>
    item.records?.some((record) =>
      record.id === block.toolCallId || record.name === toolName,
    ),
  );
};

const isProviderLifecycleProgress = (
  block: Extract<ContentBlock, { type: "progress" }>,
): boolean =>
  block.stage === "planning"
  && (
    block.phase === "model"
    ||
    /^provider$/i.test(block.label || "")
    || /^provider responded$/i.test(block.message || "")
    || /^first provider event after\b/i.test(block.summary || "")
  );

const itemStatusFromRecords = (records: ToolCallRecord[]): TurnActivityStatus => {
  const hasSuccess = records.some((record) => record.status === "success");
  const hasPartial = records.some((record) => record.status === "partial");
  const isHardFailure = (record: ToolCallRecord): boolean =>
    !isNonFatalToolIssue(record) &&
    !(hasSuccess && isRecoverableLocalExplorationFailure(record));

  // 优先检查失败和阻塞状态
  if (records.some((record) => (record.status === "failed" || record.status === "timeout") && isHardFailure(record))) return "failed";
  if (records.some((record) => record.status === "blocked" && isHardFailure(record))) return "blocked";

  // 检查运行中和待处理
  if (records.some((record) => record.status === "running")) return "running";
  if (records.some((record) => record.status === "pending")) return "pending";

  if (hasPartial) return "partial";

  // 最后才检查成功
  if (records.some((record) => record.status === "success")) return "completed";

  return "completed";
};

const itemTimingFromRecords = (records: ToolCallRecord[]) => {
  const starts = records.map((record) => record.startedAt).filter((value): value is number => Number.isFinite(value));
  const finishes = records.map((record) => record.finishedAt).filter((value): value is number => Number.isFinite(value));
  const explicitDuration = records.reduce((sum, record) => sum + (record.durationMs ?? 0), 0);
  const startedAt = starts.length ? Math.min(...starts) : undefined;
  const finishedAt = finishes.length ? Math.max(...finishes) : undefined;
  const durationMs = explicitDuration || (startedAt != null && finishedAt != null ? Math.max(0, finishedAt - startedAt) : 0);
  return { startedAt, finishedAt, durationMs };
};

const webRecordStats = (records: ToolCallRecord[]) => {
  const queries = new Set<string>();
  const pages = new Set<string>();
  const sources = new Set<string>();
  const providers = new Set<string>();
  records.forEach((record) => {
    if (isWebSearchAction(record)) {
      const query = typeof record.args.query === "string" && record.args.query.trim()
        ? record.args.query.trim()
        : record.id;
      queries.add(query);
      const candidateMatch = String(record.summary || "").match(/returned\s+(\d+)\s+candidate sources|返回\s*(\d+)\s*条候选/i);
      const candidateCount = Number(candidateMatch?.[1] ?? candidateMatch?.[2] ?? 0);
      if (candidateCount > 0) {
        for (let index = 0; index < candidateCount; index += 1) sources.add(`${record.id}:candidate:${index}`);
      }
    }
    if (isWebFetchAction(record)) {
      const url = typeof record.args.url === "string" && record.args.url.trim()
        ? record.args.url.trim()
        : record.sourceUrl || record.id;
      pages.add(url);
      if (record.extractionStatus !== "failed") sources.add(url);
    }
    if (record.sourceUrl && record.extractionStatus !== "failed") sources.add(record.sourceUrl);
    if (record.provider) providers.add(record.provider);
  });
  return {
    queryCount: queries.size,
    pageCount: pages.size,
    sourceCount: sources.size,
    provider: Array.from(providers).join(", "),
  };
};

const updateWebItemStats = (item: TurnActivityItem) => {
  if (item.kind !== "webSearch") return;
  const stats = webRecordStats(item.records ?? []);
  item.queryCount = stats.queryCount;
  item.pageCount = stats.pageCount;
  item.sourceCount = stats.sourceCount;
  item.provider = stats.provider;
};

const canAggregate = (kind: TurnActivityKind): boolean =>
  kind === "webSearch" ||
  kind === "workspaceSearch" ||
  kind === "fileRead" ||
  kind === "commandExecution" ||
  kind === "fileChange" ||
  kind === "genericTool" ||
  kind === "mcpToolCall";

const hasConflictingIterations = (previous: ToolCallRecord, next: ToolCallRecord): boolean =>
  Boolean(previous.iterationId && next.iterationId && previous.iterationId !== next.iterationId);

const mcpServerName = (name: string): string => {
  const match = name.match(/^mcp__([^_]+)__/i);
  return match?.[1] ?? name;
};

const isListFilesTool = (name: string): boolean => /^list_files$/i.test(name);

const projectionVisibilityKey = (item: DisplayRoutable): "main" | "activity" | "developer" => {
  if (requiresAttention(item)) return "main";
  const scope = displayScopeOf(item);
  if (scope === "activity") return "activity";
  if (scope === "silent" || scope === "inspector") return "developer";
  return "main";
};

const canJoinToolItem = (
  item: TurnActivityItem,
  record: ToolCallRecord,
  kind: TurnActivityKind,
  includeHiddenActivity: boolean,
): boolean => {
  if (!canAggregate(kind)) return false;
  if (item.kind !== kind) return false;
  const lastRecord = item.records?.at(-1);
  if (!lastRecord) return true;
  if (includeHiddenActivity && projectionVisibilityKey(lastRecord) !== projectionVisibilityKey(record)) return false;
  // Allow cross-iteration grouping for read-only tools (fileRead, workspaceSearch)
  if (kind !== "webSearch" && kind !== "fileRead" && kind !== "workspaceSearch" && hasConflictingIterations(lastRecord, record)) return false;
  if (kind === "webSearch") {
    return (
      (isWebSearchAction(lastRecord) && isWebSearchAction(record)) ||
      (isWebFetchAction(lastRecord) && isWebFetchAction(record))
    );
  }
  if (kind === "genericTool") {
    return lastRecord.name === "todo_write" && record.name === "todo_write";
  }
  if (kind === "fileRead" && isListFilesTool(lastRecord.name) !== isListFilesTool(record.name)) return false;
  if (kind === "mcpToolCall" && mcpServerName(lastRecord.name) !== mcpServerName(record.name)) return false;
  return true;
};

const pushToolBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "tool_call" }>,
  includeHiddenActivity = false,
): boolean => {
  if (
    !includeHiddenActivity &&
    !shouldShowInMainChat(block.record) &&
    !isAgentControlToolName(block.record.name)
  ) return false;
  const kind = kindForTool(block.record);
  if (!kind) return false;
  const last = items.at(-1);
  if (last && canJoinToolItem(last, block.record, kind, includeHiddenActivity)) {
    const records = [...(last.records ?? []), block.record];
    const timing = itemTimingFromRecords(records);
    last.blocks.push(block);
    last.records = records;
    last.status = itemStatusFromRecords(records);
    last.startedAt = timing.startedAt;
    last.finishedAt = timing.finishedAt;
    last.durationMs = timing.durationMs;
    last.hasFailure = records.some(isFailedTool);
    last.hasPendingUserAction = records.some(isRunningTool);
    updateWebItemStats(last);
    return true;
  }
  const timing = itemTimingFromRecords([block.record]);
  const nextItem: TurnActivityItem = {
    id: block.record.id || `tool-${items.length}`,
    kind,
    blocks: [block],
    records: [block.record],
    status: itemStatusFromRecords([block.record]),
    startedAt: timing.startedAt,
    finishedAt: timing.finishedAt,
    durationMs: timing.durationMs,
    hasFailure: isFailedTool(block.record),
    hasPendingUserAction: isRunningTool(block.record),
  };
  updateWebItemStats(nextItem);
  items.push(nextItem);
  return true;
};

const pushProgressBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "progress" }>,
  includeHiddenActivity = false,
) => {
  if (!includeHiddenActivity && !shouldShowInMainChat(block)) return;
  if (isProviderLifecycleProgress(block)) return;
  if (block.stage === "tool" && isBareInternalIdentifier(block.label) && !requiresAttention(block)) return;
  if (block.stage === "status" && isGenericProgressPlaceholder(block)) return;
  const kind: TurnActivityKind = block.stage === "planning" ? "planning" : "progress";
  const last = items.at(-1);
  const lastProgress = last?.progress?.at(-1);
  if (
    last?.kind === kind &&
    (!includeHiddenActivity || !lastProgress || projectionVisibilityKey(lastProgress) === projectionVisibilityKey(block))
  ) {
    const progress = [...(last.progress ?? []), block];
    last.blocks.push(block);
    last.progress = progress;
    last.status = progress.some((item) => item.status === "failed")
      ? "failed"
      : progress.some((item) => item.status === "running")
        ? "running"
        : "completed";
    last.hasFailure = progress.some((item) => item.status === "failed");
    return;
  }
  items.push({
    id: block.id || `progress-${items.length}`,
    kind,
    blocks: [block],
    progress: [block],
    status: progressStatus(block),
    hasFailure: block.status === "failed",
    hasPendingUserAction: block.stage === "approval" && block.status === "running",
  });
};

const kindForThinking = (
  block: Extract<ContentBlock, { type: "thinking" }>,
): Extract<TurnActivityKind, "reasoning" | "processNote" | "providerReasoning"> => {
  if (block.source === "provider") return "providerReasoning";
  if (block.source === "model_preamble" || block.source === "post_tool" || block.source === "runtime") return "processNote";
  return "reasoning";
};

const pushThinkingBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "thinking" }>,
  index: number,
  isThinkingStreaming?: boolean,
) => {
  if (isGenericProcessPlaceholder(block.content)) return;
  const kind = kindForThinking(block);
  const last = items.at(-1);
  if (last?.kind === kind && last.source === block.source) {
    last.blocks.push(block);
    last.content = `${last.content ?? ""}${block.content}`;
    last.status = isThinkingStreaming ? "running" : "completed";
    return;
  }
  items.push({
    id: `${kind}-${index}`,
    kind,
    blocks: [block],
    content: block.content,
    source: block.source,
    status: isThinkingStreaming ? "running" : "completed",
    hasFailure: false,
    hasPendingUserAction: false,
  });
};

const pushProcessBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "process" }>,
  index: number,
  includeHiddenActivity = false,
) => {
  if (!includeHiddenActivity && !shouldShowInMainChat(block)) return;
  if (!block.content.trim() || (!includeHiddenActivity && block.visibility === "debug")) return;
  const kind: TurnActivityKind = block.itemKind === "skill" ? "skill" : "processNote";
  if (kind !== "skill" && isGenericProcessPlaceholder(block.content)) return;
  const status: TurnActivityStatus =
    block.status === "running"
      ? "running"
      : block.status === "failed"
        ? "failed"
        : block.status === "info"
          ? "info"
          : "completed";
  items.push({
    id: block.id || `process-${index}`,
    kind,
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
    finishedAt: block.status && block.status !== "running" ? block.timestamp : undefined,
    hasFailure: status === "failed",
    hasPendingUserAction: false,
  });
};

const pushTextBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "text" }>,
  index: number,
  blocks: ContentBlock[],
) => {
  const content = block.content.trim();
  if (!content || isGenericProcessPlaceholder(content)) return;
  if (block.visibility === "debug" || isExplicitFinalTextBlock(block)) return;
  if (isRawProviderErrorText(content)) return;
  if (!hasToolActivityBefore(blocks, index) && !blocks.slice(index + 1).some(isActivityBlock)) return;
  if (duplicatesVisibleProcessText(blocks, index, content)) return;

  const source = block.source || (hasToolActivityBefore(blocks, index) ? "post_tool" : "model_preamble");
  items.push({
    id: `text-${index}`,
    kind: "agentMessage",
    blocks: [block],
    content,
    source,
    status: "completed",
    hasFailure: false,
    hasPendingUserAction: false,
  });
};

const nonEmptyTextIndexes = (blocks: ContentBlock[]): number[] =>
  blocks.flatMap((block, index) => block.type === "text" && block.content.trim() ? [index] : []);

function isFinalTextPhase(phase: string | undefined): boolean {
  return phase === "final" || phase === "final_answer";
}

function isCommentaryTextPhase(phase: string | undefined): boolean {
  return phase === "commentary";
}

function isExplicitFinalTextBlock(block: ContentBlock): boolean {
  return (
    block.type === "text" &&
    Boolean(block.content.trim()) &&
    (
      block.visibility === "final" ||
      isFinalTextPhase(block.phase) ||
      block.source === "reply" ||
      block.source === "model_final" ||
      block.source === "fallback" ||
      block.source === "partial"
    )
  );
}

const explicitFinalTextIndexes = (blocks: ContentBlock[]): number[] =>
  blocks.flatMap((block, index) => {
    return isExplicitFinalTextBlock(block) ? [index] : [];
  });

const firstExplicitFinalTextIndex = (blocks: ContentBlock[]): number | undefined =>
  explicitFinalTextIndexes(blocks).at(0);

const hasModernTextRouting = (blocks: ContentBlock[]): boolean =>
  blocks.some((block) => {
    if (block.type === "process" || block.type === "progress") return true;
    if (block.type !== "text") return false;
    if (block.visibility || block.phase || block.role) return true;
    return Boolean(block.source && block.source !== "stream");
  });

const isTimelineTextBlock = (block: ContentBlock): boolean =>
  block.type === "text" &&
  !isExplicitFinalTextBlock(block) &&
  (
    block.visibility === "timeline" ||
    block.visibility === "unsealed" ||
    block.visibility === "draft" ||
    block.visibility === "debug" ||
    block.role === "runtime" ||
    isCommentaryTextPhase(block.phase) ||
    block.source === "model_preamble" ||
    block.source === "post_tool" ||
    block.source === "runtime"
  );

const implicitFinalTextIndexes = (blocks: ContentBlock[], requireStreamSource = false): number[] =>
  blocks.flatMap((block, index) => {
    if (block.type !== "text" || !block.content.trim()) return [];
    if (isTimelineTextBlock(block)) return [];
    if (requireStreamSource && block.source !== "stream") return [];
    return [index];
  });

const isActivityBlock = (block: ContentBlock): boolean =>
  block.type === "tool_call" || block.type === "progress" || block.type === "thinking" || block.type === "process";

const hasWorkActivity = (blocks: ContentBlock[]): boolean =>
  blocks.some(isActivityBlock);

const hasRunningToolBefore = (blocks: ContentBlock[], index: number): boolean =>
  blocks.slice(0, index).some((block) =>
    block.type === "tool_call" &&
    (block.record.status === "running" || block.record.status === "pending"),
  );

const hasToolActivityBefore = (blocks: ContentBlock[], index: number): boolean =>
  blocks.slice(0, index).some((block) => block.type === "tool_call");

const isTimelineText = (block: ContentBlock): block is Extract<ContentBlock, { type: "text" }> =>
  block.type === "text" && block.visibility === "timeline";

const isRawProviderErrorText = (content: string): boolean => {
  // Keep this aligned with chatStreamEvents.ts: only full provider-error
  // messages are suppressed. Substring matches (rate limit / 429) would drop
  // legitimate answer text that mentions those tokens.
  const trimmed = content.trim().replace(/^Error:\s*/i, "");
  return /^(?:Claude API 调用失败|LLM API 调用失败|LLM API request failed|Concurrency limit exceeded)/i.test(trimmed);
};

const normalizePreambleText = (content: string): string =>
  content.replace(/\s+/g, " ").trim();

const isBareInternalIdentifier = (content: string | undefined | null): boolean =>
  /^(?:plan|provider|status|tool|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)$/i.test(String(content || "").trim());

const GENERIC_PROCESS_PLACEHOLDERS = new Set([
  "thinking",
  "reasoning",
  "processed",
  "processing",
  "working",
  "running",
  "agentisworking",
  "agentstepcompleted",
  "modelisthinking",
  "modelthinking",
  "modeldecidingnextaction",
  "decidingnextaction",
  "waitingformodel",
  "waitingformodelresponse",
  "waitingforthemodel",
  "waitingforthemodelresponse",
  "正在思考",
  "思考",
  "思考中",
  "正在处理",
  "处理中",
  "正在整理下一步",
  "等待模型回应",
  "等待模型回复",
  "模型正在思考",
  "模型正在确认下一步行动",
  "模型正在组织回复",
  "已发送请求模型正在组织回复",
  "正在生成回复",
  "正在准备",
  "思考过程",
  "正在搜索实时信息并核对来源",
  "正在打开相关来源提取可用信息",
]);

function normalizeGenericProcessPlaceholder(content: string): string {
  return content
    .replace(/^[\s•·*\-–—]+/, "")
    .replace(/[\s.。…]+$/g, "")
    .toLowerCase()
    .replace(/[`*_~#[\](){}<>|"'“”‘’]/g, "")
    .replace(/[，。！？、；：,.!?;:\s\-_/·•]+/g, "")
    .trim();
}

function isGenericPlaceholderSegment(content: string): boolean {
  const normalized = normalizeGenericProcessPlaceholder(content);
  return Boolean(normalized) && GENERIC_PROCESS_PLACEHOLDERS.has(normalized);
}

export function isGenericProcessPlaceholder(content: string | undefined | null): boolean {
  const text = normalizePreambleText(content ?? "");
  if (!text) return true;
  if (isBareInternalIdentifier(text)) return true;
  if (isGenericPlaceholderSegment(text)) return true;
  const segments = text
    .split(/\r?\n/)
    .map((segment) => segment.trim())
    .filter(Boolean);
  return segments.length > 1 && segments.every(isGenericPlaceholderSegment);
}

const INTERNAL_COLLABORATION_KEYWORD_RE = /(?:Coordinator\s*mode|Coordinator\s*模式|coordinator mode|workflow|工作流|subagent|sub-agent|subagents|子代理|子审查|探索子代理|多\s*agent|multi-?agent|swarm|parallel_tasks|read_artifact is blocked|direct tools? .*blocked|blocked in coordinator mode|产物被压缩|结果被压缩|取出.*报告|编排.*步骤|final report)/i;
const INTERNAL_COLLABORATION_NARRATION_RE = /(?:我(?:会|将|要|先|来|准备|需要|打算|用|直接|再|读取|取|尝试)|让我|现在我|接下来我|稍等|还在|已并行启动|已完成|Let me|I(?:'ll| will| need to| am going to)|poll|status|still running|running in the background|final report|The `[^`]+` .* blocked)/i;

export function isInternalCollaborationNarration(content: string | undefined | null): boolean {
  const text = normalizePreambleText(content ?? "")
    .replace(/^#{1,6}\s*/, "")
    .trim();
  if (!text) return false;
  if (/Coordinator\s*(?:mode|模式).*?(?:限制|blocks|blocked)|direct tools? .* coordinator mode/i.test(text)) {
    return true;
  }
  if (!INTERNAL_COLLABORATION_KEYWORD_RE.test(text)) return false;
  if (/^(?:#+\s*)?(?:Findings|结论|问题|建议|Summary|结果)\b/i.test(text)) return false;
  return INTERNAL_COLLABORATION_NARRATION_RE.test(text) ||
    /(?:已完成|完成了).*(?:让我|取出|collect|报告)|(?:用|启动|编排).*workflow|直接自己审查|产物被压缩|结果被压缩/i.test(text);
}

const isVisibleProcessThinking = (
  block: ContentBlock,
): block is Extract<ContentBlock, { type: "thinking" }> =>
  block.type === "thinking" &&
  (block.source === "model_preamble" || block.source === "post_tool" || block.source === "provider" || block.source === "reasoning") &&
  Boolean(normalizePreambleText(block.content)) &&
  !isGenericProcessPlaceholder(block.content);

const isVisibleProcessBlock = (
  block: ContentBlock,
): block is Extract<ContentBlock, { type: "process" }> =>
  block.type === "process" &&
  (block.itemKind === "process_text" || block.itemKind === "action_summary") &&
  block.visibility !== "debug" &&
  Boolean(normalizePreambleText(block.content)) &&
  !isGenericProcessPlaceholder(block.content);

const duplicatesVisibleProcessText = (
  blocks: ContentBlock[],
  textIndex: number,
  content: string,
): boolean => {
  const normalized = normalizePreambleText(content);
  if (!normalized) return false;
  return blocks.some((block, index) =>
    index !== textIndex &&
    (isVisibleProcessThinking(block) || isVisibleProcessBlock(block)) &&
    normalizePreambleText(block.content) === normalized,
  );
};

const selectFinalTextIndex = (
  blocks: ContentBlock[],
  options: ProjectTurnOptions,
): number | undefined => {
  const explicitIndexes = explicitFinalTextIndexes(blocks);
  const explicitCandidate = explicitIndexes.at(-1);
  if (explicitCandidate != null) {
    const block = blocks[explicitCandidate];
    if (block?.type !== "text") return undefined;
    if (isInternalCollaborationNarration(block.content)) return undefined;
    return isRawProviderErrorText(block.content) ? undefined : explicitCandidate;
  }

  const modernTextRouting = hasModernTextRouting(blocks);
  const indexes = modernTextRouting
    ? implicitFinalTextIndexes(blocks, true)
    : nonEmptyTextIndexes(blocks);
  const candidate = indexes.at(-1);
  if (candidate == null) return undefined;
  const candidateBlock = blocks[candidate];
  if (candidateBlock?.type !== "text") return undefined;
  if (isTimelineTextBlock(candidateBlock)) return undefined;

  const hasActivityAfter = blocks.slice(candidate + 1).some(isActivityBlock);
  if (hasActivityAfter) return undefined;
  if (options.isStreaming && hasRunningToolBefore(blocks, candidate)) return undefined;

  if (isRawProviderErrorText(candidateBlock.content)) return undefined;
  if (isInternalCollaborationNarration(candidateBlock.content)) return undefined;
  if (duplicatesVisibleProcessText(blocks, candidate, candidateBlock.content) && hasWorkActivity(blocks)) return undefined;

  return candidate;
};

const formatHeadlineDuration = (ms: number): string => {
  if (!Number.isFinite(ms) || ms < 100) return "";
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
};

const toolTargetPath = (record: ToolCallRecord): string => {
  const value = record.args.file_path ?? record.args.path ?? record.args.target ?? record.args.filename;
  return typeof value === "string" ? value.trim() : "";
};

const toolSourceKey = (record: ToolCallRecord): string => {
  const value = record.sourceUrl ?? record.args.url ?? record.args.source_url;
  if (typeof value === "string" && value.trim()) return value.trim();
  const query = record.args.query;
  if (typeof query === "string" && query.trim()) return `query:${query.trim()}`;
  return record.id;
};

const hasFetchedWebSource = (record: ToolCallRecord): boolean => {
  if (!isWebTool(record)) return false;
  if (String(record.extractionStatus || "").toLowerCase() === "failed") return false;
  const evidenceType = String(record.evidenceType || "").toLowerCase();
  if (evidenceType === "candidate") return false;
  if (evidenceType === "fetched") return true;
  if (/fetch/i.test(record.name)) return true;
  return Boolean(record.sourceUrl);
};

const hasSuccessfulToolRecord = (item: TurnActivityItem): boolean =>
  Boolean(item.records?.some((record) => record.status === "success"));

const itemHasHardFailure = (item: TurnActivityItem): boolean => {
  const records = item.records ?? [];
  const hasSuccess = records.some((record) => record.status === "success");
  return records.some((record) => isFailedTool(record) && !(hasSuccess && isRecoverableLocalExplorationFailure(record)));
};

const readableHeadlineTitle = (title: string): string => {
  const normalized = title.trim();
  if (/^running$/i.test(normalized)) return "正在思考";
  if (/^stopped$/i.test(normalized)) return "已停止";
  if (/^completed$/i.test(normalized)) return "已完成";
  if (/^processed$/i.test(normalized)) return "已处理";
  return normalized;
};

const readableHeadlineItem = (item: string): string => {
  const normalized = item.trim();
  const thinking = normalized.match(/^thinking\s*(.*)$/i);
  if (thinking) return `思考${thinking[1] ? ` ${thinking[1]}` : ""}`;
  const files = normalized.match(/^files\s+(\d+)$/i);
  if (files) return `查看 ${files[1]} 个文件`;
  const sources = normalized.match(/^sources\s+(\d+)$/i);
  if (sources) return `搜索 ${sources[1]} 个来源`;
  const edits = normalized.match(/^edits\s+(\d+)(\s+\+\d+\s+-\d+)?$/i);
  if (edits) return `修改 ${edits[1]} 个文件${edits[2] ?? ""}`;
  const artifacts = normalized.match(/^artifacts\s+(\d+)$/i);
  if (artifacts) return `生成 ${artifacts[1]} 个产物`;
  return normalized;
};

const buildHeadline = (
  items: TurnActivityItem[],
  status: TurnProjection["status"],
  durationMs: number,
  options: ProjectTurnOptions,
): TurnHeadline => {
  const allRecords = items.flatMap((item) => item.records ?? []);
  const fileTargets = new Set<string>();
  let fileExplorationFallback = 0;
  const changedTargets = new Set<string>();
  let changedFallback = 0;
  let plus = 0;
  let minus = 0;
  const webSources = new Set<string>();
  const artifacts = new Set<string>();
  const hasReasoning = items.some((item) => item.kind === "reasoning" || item.kind === "processNote");

  for (const record of allRecords) {
    if (record.artifactId) artifacts.add(record.artifactId);
    if (hasFetchedWebSource(record)) webSources.add(toolSourceKey(record));
    const path = toolTargetPath(record);
    if (kindForTool(record) === "fileChange") {
      if (path) changedTargets.add(path);
      else changedFallback += 1;
      plus += record.diff?.plus ?? 0;
      minus += record.diff?.minus ?? 0;
    } else if (
      (isUsableTool(record) || record.status === "running" || record.status === "pending") &&
      (kindForTool(record) === "fileRead" || kindForTool(record) === "workspaceSearch")
    ) {
      if (path) fileTargets.add(path);
      else fileExplorationFallback += 1;
    }
  }

  const changedFiles = changedTargets.size + changedFallback;
  const exploredFiles = fileTargets.size + fileExplorationFallback;
  const projectedSourceCount = items.reduce((max, item) => Math.max(max, item.kind === "webSearch" ? item.sourceCount ?? 0 : 0), 0);
  const sourceCount = Math.max(options.sourceCount ?? 0, webSources.size, projectedSourceCount);
  const isFailed = status === "failed";
  const artifactCount = isFailed ? 0 : Math.max(options.artifactCount ?? 0, artifacts.size);
  const headlineItems: string[] = [];
  const formattedDuration = formatHeadlineDuration(durationMs);

  if (hasReasoning) headlineItems.push(formattedDuration ? `思考 ${formattedDuration}` : "思考");
  if (exploredFiles > 0) headlineItems.push(`查看 ${exploredFiles} 个文件`);
  if (sourceCount > 0) headlineItems.push(`搜索 ${sourceCount} 个来源`);
  if (changedFiles > 0) {
    const diffSuffix = plus || minus ? ` +${plus} -${minus}` : "";
    headlineItems.push(`修改 ${changedFiles} 个文件${diffSuffix}`);
  }
  if (artifactCount > 0) headlineItems.push(`生成 ${artifactCount} 个产物`);

  const hasReviewableChanges = !isFailed && changedFiles > 0 && (plus > 0 || minus > 0 || allRecords.some((record) => Boolean(record.diff?.patch)));
  const cta = isFailed ? null : hasReviewableChanges ? "review" : artifactCount > 0 ? "open" : sourceCount > 0 ? "sources" : null;
  const title = status === "streaming"
    ? "正在思考"
    : status === "failed"
      ? "已停止"
      : headlineItems.length
        ? "已完成"
        : "已处理";

  return {
    title: readableHeadlineTitle(title),
    items: headlineItems.map(readableHeadlineItem),
    cta,
    diffStats: { plus, minus, files: changedFiles },
    sourceCount,
    artifactCount,
    hasReviewableChanges,
  };
};

export function projectTurn(
  blocks: ContentBlock[],
  options: ProjectTurnOptions = {},
): TurnProjection {
  const finalTextIndex = selectFinalTextIndex(blocks, options);
  const finalBoundaryIndex = firstExplicitFinalTextIndex(blocks);
  const finalAnswerBlock = finalTextIndex == null
    ? undefined
    : (blocks[finalTextIndex] as Extract<ContentBlock, { type: "text" }> | undefined);
  const finalAnswer = finalAnswerBlock?.type === "text" ? finalAnswerBlock.content : "";
  const finalAnswerSource = finalAnswerBlock?.type === "text"
    ? (finalAnswerBlock as { source?: string }).source
    : undefined;
  // A BriefTool (send_message) reply makes itself the visible answer of the
  // turn; the preamble text that precedes it is then collapsed by the caller.
  const hasSendMessage = blocks.some(
    (block) => block.type === "tool_call" && block.record.name === "reply",
  );

  const activityItems: TurnActivityItem[] = [];
  const legacyThinkingBlocks = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "thinking" }> =>
      block.type === "thinking" && !block.source && !isGenericProcessPlaceholder(block.content),
  );
  if (legacyThinkingBlocks.length) {
    activityItems.push({
      id: "reasoning",
      kind: "reasoning",
      blocks: legacyThinkingBlocks,
      content: legacyThinkingBlocks.map((block) => block.content).join(""),
      status: options.isThinkingStreaming ? "running" : "completed",
      hasFailure: false,
      hasPendingUserAction: false,
    });
  }

  let skippedAskUser = false;
  blocks.forEach((block, index) => {
    if (block.type === "thinking") {
      if (!block.source) return;
      pushThinkingBlock(activityItems, block, index, options.isThinkingStreaming);
      return;
    }
    if (block.type === "process") {
      pushProcessBlock(activityItems, block, index, options.includeHiddenActivity);
      return;
    }
    if (block.type === "text") {
      if (finalBoundaryIndex != null && index > finalBoundaryIndex && index !== finalTextIndex) return;
      if (index !== finalTextIndex && !hasSendMessage) {
        pushTextBlock(activityItems, block, index, blocks);
      }
      return;
    }
    if (block.type === "tool_call") {
      if (block.record.name === "ask_user") skippedAskUser = true;
      pushToolBlock(activityItems, block, options.includeHiddenActivity);
      return;
    }
    if (block.type === "progress") {
      if (isProgressMirroredByTool(block, activityItems)) return;
      pushProgressBlock(activityItems, block, options.includeHiddenActivity);
    }
  });

  const displayActivityItems = activityItems;

  const hasActivityFailure = displayActivityItems.some((item) =>
    item.status === "failed" ||
    item.status === "blocked" ||
    (item.hasFailure && !hasSuccessfulToolRecord(item)) ||
    itemHasHardFailure(item)
  );
  const hasOnlyLocalSuccessfulWorkspaceActivity = displayActivityItems.length > 0 && displayActivityItems.every((item) => (
    !item.hasFailure
    && item.status === "completed"
    && (item.kind === "workspaceSearch" || item.kind === "fileRead")
  ));
  const terminalFailedWithoutFailedActivity = (
    options.terminalStatus === "failed"
    && !finalAnswer.trim()
    && hasOnlyLocalSuccessfulWorkspaceActivity
  );
  const hasFailure = hasActivityFailure || (options.terminalStatus === "failed" && !terminalFailedWithoutFailedActivity);
  const hasPendingUserAction = skippedAskUser || displayActivityItems.some((item) => item.hasPendingUserAction);
  const durationMs = displayActivityItems.reduce((max, item) => Math.max(max, item.durationMs ?? 0), 0);
  const status = options.isStreaming
    ? "streaming"
    : hasFailure
      ? "failed"
      : finalAnswer.trim() || displayActivityItems.length
        ? "completed"
        : "empty";
  const headline = buildHeadline(displayActivityItems, status, durationMs, options);

  return {
    activityItems: displayActivityItems,
    finalAnswer,
    finalAnswerSource,
    hasSendMessage,
    status,
    durationMs,
    hasFailure,
    hasPendingUserAction,
    headline,
  };
}

export function sanitizeBlockedSummary(summary: string): string {
  const text = summary.replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (/Stopped web search after \d+ consecutive empty or failed searches|consecutive empty or failed searches|Do not keep changing keywords/i.test(text)) {
    return "网页搜索连续失败，已停止继续搜索";
  }
  if (/duplicate tool call|repeated.*tool call|same model step/i.test(text)) return "已跳过重复工具调用";
  if (/blocked by policy|always deny|not allowed|permission|access is denied|unauthorized/i.test(text)) return "已被策略阻止";
  if (/outside (?:the )?(?:allowed|trusted) workspace|outside allowed|forbidden path|不在允许范围|允许的路径|禁止的路径/i.test(text)) {
    return "超出允许的工作区";
  }
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}
