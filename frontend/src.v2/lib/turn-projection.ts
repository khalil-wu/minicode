import type { ContentBlock } from "../stores/types";
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
  | "progress";

export type TurnActivityStatus = "running" | "completed" | "failed" | "blocked" | "pending" | "partial" | "info";

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
}

const isRunningTool = (record: ToolCallRecord): boolean =>
  record.status === "running" || record.status === "pending";

const isUsableTool = (record: ToolCallRecord): boolean =>
  record.status === "success" || record.status === "partial";

const isFailedTool = (record: ToolCallRecord): boolean =>
  (record.status === "failed" || record.status === "blocked") && !isNonFatalToolIssue(record);

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
  /^(?:grep|grep_files|glob|glob_files|list_files|fuzzy_search)$/i.test(name);

const isFileReadTool = (name: string): boolean =>
  /^(?:read_file|read_artifact)$/i.test(name);

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
    "progress",
  ].includes(value);

const kindForTool = (record: ToolCallRecord): TurnActivityKind | null => {
  if (record.name === "ask_user") return null;
  if (record.name === "todo_write") return "genericTool";
  if (record.activityKind && isTurnActivityKind(record.activityKind)) return record.activityKind;
  const resultKind = resultKindForRecord(record);
  if (isWebTool(record)) return "webSearch";
  if (resultKind === "command" || isCommandTool(record.name)) return "commandExecution";
  if (resultKind === "edit" || isFileChangeTool(record.name)) {
    return hasFileChangeTarget(record) ? "fileChange" : "genericTool";
  }
  if (record.name.startsWith("mcp__") || resultKind === "mcp") return "mcpToolCall";
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

const isProgressMirroredByTool = (
  block: Extract<ContentBlock, { type: "progress" }>,
  items: TurnActivityItem[],
): boolean => {
  if (block.stage !== "tool" || !block.toolName) return false;
  return items.some((item) =>
    item.records?.some((record) =>
      record.id === block.toolCallId || record.name === block.toolName,
    ),
  );
};

const itemStatusFromRecords = (records: ToolCallRecord[]): TurnActivityStatus => {
  const hasSuccess = records.some((record) => record.status === "success");
  const hasPartial = records.some((record) => record.status === "partial");
  const isHardFailure = (record: ToolCallRecord): boolean =>
    !isNonFatalToolIssue(record) &&
    !(hasSuccess && isRecoverableLocalExplorationFailure(record));

  // 优先检查失败和阻塞状态
  if (records.some((record) => record.status === "failed" && isHardFailure(record))) return "failed";
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

const canJoinToolItem = (item: TurnActivityItem, record: ToolCallRecord, kind: TurnActivityKind): boolean => {
  if (!canAggregate(kind)) return false;
  if (item.kind !== kind) return false;
  const lastRecord = item.records?.at(-1);
  if (!lastRecord) return true;
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
  if (kind === "mcpToolCall" && mcpServerName(lastRecord.name) !== mcpServerName(record.name)) return false;
  return true;
};

const pushToolBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "tool_call" }>,
): boolean => {
  const kind = kindForTool(block.record);
  if (!kind) return false;
  const last = items.at(-1);
  if (last && canJoinToolItem(last, block.record, kind)) {
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
) => {
  const kind: TurnActivityKind = block.stage === "planning" ? "planning" : "progress";
  const last = items.at(-1);
  if (last?.kind === kind) {
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
  if (block.source === "model_preamble" || block.source === "runtime") return "processNote";
  return "reasoning";
};

const pushThinkingBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "thinking" }>,
  index: number,
  isThinkingStreaming?: boolean,
) => {
  const kind = kindForThinking(block);
  const last = items.at(-1);
  if (last?.kind === kind) {
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
    status: isThinkingStreaming ? "running" : "completed",
    hasFailure: false,
    hasPendingUserAction: false,
  });
};

const pushProcessBlock = (
  items: TurnActivityItem[],
  block: Extract<ContentBlock, { type: "process" }>,
  index: number,
) => {
  if (!block.content.trim() || block.visibility === "debug") return;
  const kind: TurnActivityKind = "processNote";
  const status: TurnActivityStatus =
    block.status === "running"
      ? "running"
      : block.status === "failed"
        ? "failed"
        : block.status === "info"
          ? "info"
          : "completed";
  const last = items.at(-1);
  if (last?.kind === kind && last.source === block.source && last.itemKind === block.itemKind) {
    last.blocks.push(block);
    last.content = `${last.content ?? ""}${block.content}`;
    last.status = status;
    last.hasFailure = last.hasFailure || status === "failed";
    return;
  }
  items.push({
    id: block.id || `process-${index}`,
    kind,
    blocks: [block],
    content: block.content,
    source: block.source,
    itemKind: block.itemKind,
    status,
    startedAt: block.timestamp,
    finishedAt: block.status && block.status !== "running" ? block.timestamp : undefined,
    hasFailure: status === "failed",
    hasPendingUserAction: false,
  });
};

const nonEmptyTextIndexes = (blocks: ContentBlock[]): number[] =>
  blocks.flatMap((block, index) => block.type === "text" && block.content.trim() ? [index] : []);

const isActivityBlock = (block: ContentBlock): boolean =>
  block.type === "tool_call" || block.type === "progress" || block.type === "thinking" || block.type === "process";

const hasWorkActivity = (blocks: ContentBlock[]): boolean =>
  blocks.some(isActivityBlock);

const isRawProviderErrorText = (content: string): boolean =>
  /Claude API 调用失败|LLM API 调用失败|LLM API request failed|Concurrency limit exceeded|rate limit|too many requests|429/i.test(content);

const isInterimAssistantText = (content: string): boolean => {
  const text = content.replace(/\s+/g, " ").trim();
  if (!text) return false;
  if (/(?:收到，我继续|我继续|我现在|现在我|接下来|下一步|重新跑|跑测试|回归.*过了|测试.*过了|修掉了|我会|我来|我先)/.test(text)) {
    return true;
  }
  return (
    /^(?:let me|i['’]?ll|i will|i(?:'| a)?m going to|first[,，]?|next[,，]?)/i.test(text) ||
    /(?:我先|先看|先了解|先检查|先详细|接下来|继续看|再看|再检查|我看看|看一下|了解一下|先修|先改)/.test(text) ||
    /(?:look at|look into|inspect|check|read).{0,80}(?:first|next|then)/i.test(text)
  );
};

const selectFinalTextIndex = (
  blocks: ContentBlock[],
  options: ProjectTurnOptions,
): number | undefined => {
  const indexes = nonEmptyTextIndexes(blocks);
  const candidate = indexes.at(-1);
  if (candidate == null) return undefined;

  const hasActivityAfter = blocks.slice(candidate + 1).some(isActivityBlock);
  if (hasActivityAfter) return undefined;

  const content = (blocks[candidate] as Extract<ContentBlock, { type: "text" }>).content;
  if (isRawProviderErrorText(content)) return undefined;
  if (isInterimAssistantText(content) && (options.isStreaming || hasWorkActivity(blocks))) return undefined;

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
  if (/^running$/i.test(normalized)) return "正在处理";
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
    ? "正在处理"
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
  const finalAnswer = finalTextIndex == null || blocks[finalTextIndex]?.type !== "text"
    ? ""
    : (blocks[finalTextIndex] as Extract<ContentBlock, { type: "text" }>).content;

  const activityItems: TurnActivityItem[] = [];
  const legacyThinkingBlocks = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "thinking" }> =>
      block.type === "thinking" && !block.source,
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
      pushProcessBlock(activityItems, block, index);
      return;
    }
    if (block.type === "text") {
      if (index === finalTextIndex) return;
      if (!block.content.trim()) return;
      if (isRawProviderErrorText(block.content)) return;
      activityItems.push({
        id: `agent-message-${index}`,
        kind: "agentMessage",
        blocks: [block],
        content: block.content,
        status: "completed",
        hasFailure: false,
        hasPendingUserAction: false,
      });
      return;
    }
    if (block.type === "tool_call") {
      if (block.record.name === "ask_user") skippedAskUser = true;
      pushToolBlock(activityItems, block);
      return;
    }
    if (block.type === "progress") {
      if (isProgressMirroredByTool(block, activityItems)) return;
      pushProgressBlock(activityItems, block);
    }
  });

  const hasActivityFailure = activityItems.some((item) =>
    item.status === "failed" ||
    item.status === "blocked" ||
    (item.hasFailure && !hasSuccessfulToolRecord(item)) ||
    itemHasHardFailure(item)
  );
  const hasOnlyLocalSuccessfulWorkspaceActivity = activityItems.length > 0 && activityItems.every((item) => (
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
  const hasPendingUserAction = skippedAskUser || activityItems.some((item) => item.hasPendingUserAction);
  const durationMs = activityItems.reduce((max, item) => Math.max(max, item.durationMs ?? 0), 0);
  const status = options.isStreaming
    ? "streaming"
    : hasFailure
      ? "failed"
      : finalAnswer.trim() || activityItems.length
        ? "completed"
        : "empty";
  const headline = buildHeadline(activityItems, status, durationMs, options);

  return {
    activityItems,
    finalAnswer,
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
