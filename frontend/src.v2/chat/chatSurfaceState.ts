/**
 * Chat surface state projection — converts flat ChatMessage[] into
 * cell-based ChatTurnState[] for Codex-like rendering.
 *
 * This is the bridge between the existing message-based store and the
 * new cell architecture. It wraps projectTurn to extract
 * ActivityCells, ExecCells, DiffCells, ErrorCells, and the final answer.
 */

import type { ChatMessage } from "../stores/types";
import { useAppStore } from "../stores";
import { getContentBlocks } from "../lib/content-blocks";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { mergeCitationsWithWebSearchFallback } from "./citationProjection";
import {
  projectTurn,
  type TurnActivityItem,
  type TurnActivityStatus,
} from "../lib/turn-projection";
import { getToolDiffStats, type ToolCallRecord } from "../lib/tool-call-reducer";
import type {
  ActivityCellState,
  AssistantMarkdownCellState,
  ChatSurfaceState,
  ChatTurnState,
  DiffCellState,
  DiffFileChange,
  ErrorCellState,
  ExecCellState,
  HistoryCellState,
  PlanCellState,
  StatusNoticeCellState,
  StreamingAssistantTailCellState,
  ThinkingCellState,
  TurnSummaryCellState,
  UserMessageCellState,
} from "./cells/cellTypes";

type CommittedCellState = Exclude<
  HistoryCellState,
  UserMessageCellState | StreamingAssistantTailCellState
>;

// ── Activity Label Language ─────────────────────────────────────────
// Activity titles/subtitles follow the user's language, mirroring the
// backend's per-turn CJK detection (see _prefers_chinese_fallback). The
// signal is the turn's user message; English turns get English labels
// instead of a fixed Chinese string next to an English answer.

type Lang = "zh" | "en";

const CJK_RE = /[㐀-䶿一-鿿豈-﫿]/;

function detectLang(text: string | undefined | null): Lang {
  return text && CJK_RE.test(text) ? "zh" : "en";
}

function t(lang: Lang, zh: string, en: string): string {
  return lang === "zh" ? zh : en;
}

// ── Status Mapping ──────────────────────────────────────────────────

function mapActivityStatus(
  status: TurnActivityStatus,
): ActivityCellState["status"] {
  switch (status) {
    case "running":
    case "pending":
      return "running";
    case "failed":
    case "blocked":
      return "failed";
    case "partial":
    default:
      return "done";
  }
}

// ── Activity Title Helpers ──────────────────────────────────────────

function getActivityTitle(item: TurnActivityItem, lang: Lang = "zh"): string {
  const records = item.records ?? [];
  const first = records[0];
  const isRunning = item.status === "running" || item.status === "pending";
  const hasFailure =
    item.status === "failed" || item.status === "blocked" || item.hasFailure;
  const hasPartial = item.status === "partial" || records.some((r) => r.status === "partial");
  const hasSuccess = records.some((r) => r.status === "success");
  const partialFailure = hasFailure && hasSuccess;
  const positiveRecords = partialFailure ? successfulRecords(records) : records;
  switch (item.kind) {
    case "reasoning":
    case "planning":
    case "processNote":
    case "providerReasoning":
      return isRunning ? t(lang, "正在思考", "Thinking") : t(lang, "思考过程", "Reasoning");
    case "agentMessage":
      return t(lang, "过程", "Working");
    case "webSearch": {
      const queries = item.queryCount ?? records.filter(isWebSearchRecord).length;
      const hasLimitedWebEvidence = records.some(isNonFatalWebFailureRecord);
      if (hasLimitedWebEvidence && !hasSuccess) {
        return queries
          ? t(lang, "搜索实时信息受限", "Limited web search results")
          : t(lang, "读取网页资料受限", "Limited web page results");
      }
      if (partialFailure) {
        return queries
          ? t(lang, "已搜索实时信息", "Searched the web")
          : t(lang, "已读取网页资料", "Read web pages");
      }
      if (hasFailure) {
        return queries
          ? t(lang, "搜索实时信息失败", "Web search failed")
          : t(lang, "读取网页资料失败", "Reading web pages failed");
      }
      if (isRunning) {
        return queries
          ? t(lang, "正在搜索实时信息", "Searching the web")
          : t(lang, "正在读取网页资料", "Reading web pages");
      }
      if (queries) return t(lang, "已搜索实时信息", "Searched the web");
      return t(lang, "已读取网页资料", "Read web pages");
    }
    case "workspaceSearch": {
      const count = positiveRecords.length || records.length;
      if (partialFailure) return t(lang, `已搜索代码 ${count} 次`, `Searched code ${count}×`);
      if (hasFailure) return t(lang, `搜索 ${count} 个文件失败`, `Search failed (${count} files)`);
      if (isRunning) return t(lang, "正在搜索代码", "Searching code");
      return t(lang, `已搜索代码 ${count} 次`, `Searched code ${count}×`);
    }
    case "fileRead": {
      const count = positiveRecords.length || records.length;
      if (hasPartial) return t(lang, "已读取部分文件内容", "Read partial file contents");
      if (partialFailure) return t(lang, `已读取 ${count} 个文件`, `Read ${count} files`);
      if (hasFailure) return t(lang, `读取 ${count} 个文件失败`, `Reading ${count} files failed`);
      if (isRunning) return t(lang, "正在读取相关文件", "Reading files");
      return t(lang, `已读取 ${count} 个文件`, `Read ${count} files`);
    }
    case "commandExecution": {
      if (isRunning) {
        const target = extractToolTarget(records.at(-1));
        return target ? t(lang, `正在运行 ${target}`, `Running ${target}`) : t(lang, "正在运行命令", "Running command");
      }
      if (records.length > 1) return t(lang, `已运行 ${records.length} 条命令`, `Ran ${records.length} commands`);
      const target = extractToolTarget(first);
      return target ? t(lang, `已运行 ${target}`, `Ran ${target}`) : t(lang, "已运行命令", "Ran command");
    }
    case "fileChange": {
      const count = uniqueFileCount(records);
      if (isRunning) return t(lang, "正在修改文件", "Editing files");
      if (count === 1) {
        const target = extractToolTarget(first);
        return target ? t(lang, `已修改 ${target}`, `Edited ${target}`) : t(lang, "已修改文件", "Edited file");
      }
      return t(lang, `已修改 ${count} 个文件`, `Edited ${count} files`);
    }
    case "mcpToolCall": {
      const label = first?.name.startsWith("mcp__")
        ? first.name.split("__").slice(1, 3).join("/")
        : first?.name;
      if (isRunning) return label ? t(lang, `正在调用 MCP ${label}`, `Calling MCP ${label}`) : t(lang, "正在调用 MCP", "Calling MCP");
      return label ? t(lang, `已调用 MCP ${label}`, `Called MCP ${label}`) : t(lang, "已调用 MCP", "Called MCP");
    }
    case "progress":
      return item.progress?.at(-1)?.label || t(lang, "进度", "Progress");
    default:
      if (records.length > 0 && records.every((record) => record.name === "todo_write")) {
        if (isRunning) return t(lang, "正在更新任务清单", "Updating task list");
        return t(lang, "已更新任务清单", "Updated task list");
      }
      if (isRunning) return first ? t(lang, `正在调用 ${first.name}`, `Calling ${first.name}`) : t(lang, "正在调用工具", "Calling tool");
      return first ? t(lang, `已调用 ${first.name}`, `Called ${first.name}`) : t(lang, "已调用工具", "Called tool");
  }
}

function getActivitySubtitle(item: TurnActivityItem, lang: Lang = "zh"): string {
  const records = item.records ?? [];
  const isRunning = item.status === "running" || item.status === "pending";
  if (item.kind === "fileRead" || item.kind === "workspaceSearch") {
    if (isRunning) {
      const doneCount = records.filter((r) => r.status === "success").length;
      return doneCount > 0 ? t(lang, `已读取 ${doneCount} 个`, `${doneCount} done`) : "";
    }
    const recordsForSubtitle = records.some((r) => r.status === "success") &&
      records.some((r) => r.status === "failed" || r.status === "blocked")
      ? successfulRecords(records)
      : records;
    const names = recordsForSubtitle
      .map((r) => extractToolTarget(r))
      .filter(Boolean);
    if (names.length === 0) return "";
    const preview = names.slice(0, 2).map(shortFileName).join(", ");
    return names.length > 2
      ? t(lang, `${preview} 等 ${names.length} 个文件`, `${preview} +${names.length - 2} more`)
      : preview;
  }
  if (item.kind === "webSearch") {
    const searches = item.queryCount ?? records.filter(isWebSearchRecord).length;
    const pages = item.pageCount ?? records.filter(isWebFetchRecord).length;
    if (isRunning) {
      if (searches) return t(lang, `已完成 ${searches} 次搜索`, `${searches} searches done`);
      if (pages) return t(lang, `已读取 ${pages} 个页面`, `${pages} pages read`);
      return records.length ? t(lang, `${records.length} 个来源`, `${records.length} sources`) : "";
    }
    if (searches) return t(lang, `${searches} 次搜索`, `${searches} searches`);
    if (pages) return t(lang, `${pages} 个页面`, `${pages} pages`);
    return records.length ? t(lang, `${records.length} 个来源`, `${records.length} sources`) : "";
  }
  return "";
}

function successfulRecords(records: ToolCallRecord[]): ToolCallRecord[] {
  return records.filter((record) => record.status === "success");
}

function shortFileName(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] ?? path;
}

function shortCommand(command: string): string {
  const text = command.replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > 72 ? `${text.slice(0, 69)}...` : text;
}

// ── Tool Helpers ────────────────────────────────────────────────────

function extractToolTarget(record?: ToolCallRecord): string {
  if (!record) return "";
  const args = record.args ?? {};
  const path = args.file_path ?? args.path ?? args.target ?? args.filename;
  if (typeof path === "string") return path;
  const command = args.command;
  if (typeof command === "string") {
    return command.length > 80 ? `${command.slice(0, 77)}...` : command;
  }
  return "";
}

function isWebSearchRecord(record: ToolCallRecord): boolean {
  return /(?:web_search|search)$/i.test(record.name);
}

function isWebFetchRecord(record: ToolCallRecord): boolean {
  return /(?:web_fetch|fetch_page|fetch)$/i.test(record.name);
}

function isLocalExplorationTool(record: ToolCallRecord): boolean {
  return /^(?:read_file|read_artifact|list_files|glob_files|grep|grep_files|fuzzy_search)$/i.test(record.name);
}

function isWebLookupRecord(record: ToolCallRecord): boolean {
  const resultKind = String(record.resultKind || "").toLowerCase();
  return (
    resultKind === "search" ||
    resultKind === "web" ||
    isWebSearchRecord(record) ||
    isWebFetchRecord(record)
  );
}

function isNetworkLikeToolFailure(record: ToolCallRecord): boolean {
  const text = `${record.summary ?? ""} ${record.displaySummary ?? ""} ${record.developerDetail ?? ""}`;
  if (record.providerErrorType === "network") return true;
  return /407|proxy authentication required|代理鉴权失败|代理认证失败|connection|timeout|timed out|network/i.test(text);
}

function isPermissionBlockedRecord(record: ToolCallRecord): boolean {
  const text = [
    record.errorKind,
    record.userSummary,
    record.errorInfo?.user_message,
    record.summary,
    record.developerDetail,
  ].filter(Boolean).join(" ");
  return /permission_required|权限策略|不在允许范围|允许的路径|禁止的路径|outside (?:the )?(?:allowed|trusted) workspace|outside allowed|forbidden path/i.test(text);
}

function isMissingRequiredArgumentRecord(record: ToolCallRecord): boolean {
  return /Invalid tool call|missing required argument/i.test(String(record.summary ?? ""));
}

function isRepeatGuardRecord(record: ToolCallRecord): boolean {
  return /Skipped repeated failed tool call|repeated failed tool call|反复尝试|重复/i.test(String(record.summary ?? ""));
}

function isRecoverableLocalProbeFailureRecord(record: ToolCallRecord): boolean {
  if (record.status !== "failed" && record.status !== "blocked") return false;
  if (!isLocalExplorationTool(record)) return false;
  if (isNetworkLikeToolFailure(record)) return false;
  if (isPermissionBlockedRecord(record)) return false;
  if (isMissingRequiredArgumentRecord(record)) return false;
  const text = [
    record.summary,
    record.displaySummary,
    record.userSummary,
    record.errorInfo?.user_message,
    record.developerDetail,
  ].filter(Boolean).join(" ");
  return /File does not exist|does not exist:|No such file or directory|ENOENT|Path not found|Not a file|Is a directory|EISDIR|Directory does not exist|cannot read binary|non-UTF-?8/i.test(text);
}

function isNonFatalWebFailureRecord(record: ToolCallRecord): boolean {
  return (
    isWebLookupRecord(record) &&
    (record.status === "failed" || record.status === "blocked") &&
    !isNetworkLikeToolFailure(record)
  );
}

function isNonFatalToolRecord(record: ToolCallRecord): boolean {
  const projection = String(record.projection || "");
  if (projection === "silent" || projection === "status" || projection === "warning") return true;
  const kind = String(record.errorKind || "");
  if (["missing_generated_content", "routing_error", "stale_evidence", "repeat_guard", "tool_disabled"].includes(kind)) {
    return true;
  }
  if (isRecoverableLocalProbeFailureRecord(record)) return true;
  if (isNonFatalWebFailureRecord(record)) return true;
  return isWebGuardGuidanceRecord(record);
}

function isFailedToolRecord(record: ToolCallRecord): boolean {
  return (record.status === "failed" || record.status === "blocked") && !isNonFatalToolRecord(record);
}

function timingFromRecords(records: ToolCallRecord[]) {
  const starts = records.map((record) => record.startedAt).filter((value): value is number => Number.isFinite(value));
  const finishes = records.map((record) => record.finishedAt).filter((value): value is number => Number.isFinite(value));
  const startedAt = starts.length ? Math.min(...starts) : undefined;
  const finishedAt = finishes.length ? Math.max(...finishes) : undefined;
  return { startedAt, finishedAt };
}

function statusFromRecords(records: ToolCallRecord[]): TurnActivityStatus {
  // If any record succeeded, the operation produced useful results —
  // treat as completed even if some sibling calls failed (hermes-style:
  // partial success is still success from the user's perspective).
  const hasAnySuccess = records.some((r) => r.status === "success");
  const hasAnyPartial = records.some((r) => r.status === "partial");
  if (hasAnySuccess) {
    if (records.some((r) => r.status === "running")) return "running";
    if (records.some((r) => r.status === "pending")) return "pending";
    return "completed";
  }
  if (records.some((record) => record.status === "failed" && isFailedToolRecord(record))) return "failed";
  if (records.some((record) => record.status === "blocked" && isFailedToolRecord(record))) return "blocked";
  if (records.some((record) => record.status === "running")) return "running";
  if (records.some((record) => record.status === "pending")) return "pending";
  if (hasAnyPartial) return "partial";
  return "completed";
}

function aggregateWebActivityItems(items: TurnActivityItem[]): TurnActivityItem[] {
  const result: TurnActivityItem[] = [];
  let webRun: TurnActivityItem[] = [];

  const flushWebRun = () => {
    if (webRun.length === 0) return;
    result.push(...aggregateContiguousWebRun(webRun));
    webRun = [];
  };

  for (const item of items) {
    if (item.kind === "webSearch") {
      webRun.push(item);
      continue;
    }
    flushWebRun();
    result.push(item);
  }
  flushWebRun();

  return result;
}

function aggregateContiguousWebRun(webRun: TurnActivityItem[]): TurnActivityItem[] {
  const searchRecords: ToolCallRecord[] = [];
  const fetchRecords: ToolCallRecord[] = [];
  for (const item of webRun) {
    for (const record of item.records ?? []) {
      if (isWebFetchRecord(record)) fetchRecords.push(record);
      else searchRecords.push(record);
    }
  }

  if (searchRecords.length + fetchRecords.length <= 1) return webRun;

  const makeWebItem = (kind: "search" | "fetch", groupRecords: ToolCallRecord[]): TurnActivityItem => {
    const timing = timingFromRecords(groupRecords);
    const status = statusFromRecords(groupRecords);
    return {
      id: `${kind === "fetch" ? "web-fetch" : "web-search"}-${groupRecords[0]?.id ?? "turn"}`,
      kind: "webSearch",
      blocks: [],
      records: groupRecords,
      status,
      startedAt: timing.startedAt,
      finishedAt: timing.finishedAt,
      durationMs: groupRecords.reduce((sum, record) => sum + (record.durationMs ?? 0), 0),
      hasFailure: groupRecords.some(isFailedToolRecord),
      hasPendingUserAction: groupRecords.some((record) => record.status === "running" || record.status === "pending"),
      queryCount: kind === "search" ? groupRecords.length : 0,
      pageCount: kind === "fetch" ? groupRecords.length : 0,
    };
  };

  const result: TurnActivityItem[] = [];
  if (searchRecords.length) result.push(makeWebItem("search", searchRecords));
  if (fetchRecords.length) result.push(makeWebItem("fetch", fetchRecords));
  return result;
}

function isHiddenProcessActivity(item: TurnActivityItem): boolean {
  if (item.kind === "progress") {
    const text = (item.progress ?? [])
      .map((progress) => [
        progress.id,
        progress.stage,
        progress.phase,
        progress.label,
        progress.summary,
        progress.message,
      ].filter(Boolean).join(" "))
      .join(" ");
    if (/agent:recover|recover|Recovered|Used available tool results|Model .*failed/i.test(text)) {
      return true;
    }
  }
  return (
    item.kind === "agentMessage" ||
    item.kind === "processNote" ||
    item.kind === "providerReasoning" ||
    item.kind === "reasoning"
  );
}

function isInterleavedAgentMessageActivity(
  item: TurnActivityItem,
  index: number,
  items: TurnActivityItem[],
): boolean {
  if (item.kind !== "agentMessage" || !item.content?.trim()) return false;
  const previousTool = [...items.slice(0, index)].reverse().find((candidate) => Boolean(candidate.records?.length));
  const nextTool = items.slice(index + 1).find((candidate) => Boolean(candidate.records?.length));
  return Boolean(previousTool && nextTool && previousTool.kind !== nextTool.kind);
}

function isVisibleThinkingActivity(item: TurnActivityItem): boolean {
  const isThinkingKind =
    item.kind === "processNote" ||
    item.kind === "providerReasoning" ||
    item.kind === "reasoning";
  if (!isThinkingKind || !item.content?.trim()) return false;

  const thinkingBlocks = item.blocks.filter((block) => block.type === "thinking");
  if (thinkingBlocks.length === 0) return item.kind !== "providerReasoning";

  return (
    thinkingBlocks.some((block) =>
      Boolean(block.content?.trim()) &&
      block.visibility !== "debug" &&
      !block.is_raw_provider_reasoning,
    )
  );
}

function thinkingSourceForItem(item: TurnActivityItem): ThinkingCellState["source"] {
  if (item.kind === "processNote" && item.source === "runtime") return "runtime";
  if (item.kind === "processNote") return "model_preamble";
  if (item.kind === "providerReasoning") return "provider";
  return "reasoning";
}

function isGenericIterationProgress(item: TurnActivityItem): boolean {
  if (item.kind !== "progress") return false;
  const latest = item.progress?.at(-1);
  const text = (item.progress ?? [])
    .map((progress) => [
      progress.id,
      progress.stage,
      progress.phase,
      progress.label,
      progress.summary,
      progress.message,
    ].filter(Boolean).join(" "))
    .join(" ");
  return (
    String(latest?.phase ?? "") === "iteration" ||
    /Agent working|Iteration\s+\d+\s*\/\s*\d+|agent:iter/i.test(text)
  );
}

function isSchemaOrRepeatGuardRecord(record: ToolCallRecord): boolean {
  const raw = String(record.summary ?? "");
  if (["silent", "status", "warning"].includes(String(record.projection || ""))) return true;
  if (["missing_generated_content", "routing_error", "stale_evidence", "repeat_guard", "tool_disabled"].includes(String(record.errorKind || ""))) return true;
  return /Invalid tool call|missing required argument|Skipped repeated failed tool call|repeated failed tool call/i.test(raw);
}

function isWebGuardGuidanceRecord(record: ToolCallRecord): boolean {
  const raw = `${record.summary ?? ""} ${record.displaySummary ?? ""}`;
  return /You already have both search results|do not search or fetch more|Search budget reached|Web budget reached|Enough candidate web evidence|Skipped another similar web search|Use the available results|answer with uncertainty|Web budget exhausted|Search guard guidance|already searched with these exact keywords|already have enough results|returned the same result.*times|already gathered|many_web_operations|Guardrail:.*repeated_call|Guardrail:.*no_progress|搜索策略调整|相同关键词|高度相似|相似网页搜索未返回结果|停止重复搜索/i.test(raw);
}

function isHiddenSchemaFailureActivity(item: TurnActivityItem): boolean {
  const records = item.records ?? [];
  return records.length > 0 && records.every((record) =>
    isFailedToolRecord(record) &&
    isSchemaOrRepeatGuardRecord(record),
  );
}

function removeInternalGuardRecords(
  item: TurnActivityItem,
  developerMode: boolean,
): TurnActivityItem | null {
  if (developerMode) return item;
  const records = item.records ?? [];
  if (records.length === 0) return item;
  const visibleRecords = records.filter((record) =>
    !isWebGuardGuidanceRecord(record) &&
    !["silent", "status", "warning"].includes(String(record.projection || "")),
  );
  if (visibleRecords.length === records.length) return item;
  if (visibleRecords.length === 0) return null;
  const timing = timingFromRecords(visibleRecords);
  return {
    ...item,
    records: visibleRecords,
    status: statusFromRecords(visibleRecords),
    startedAt: timing.startedAt,
    finishedAt: timing.finishedAt,
    durationMs: visibleRecords.reduce((sum, record) => sum + (record.durationMs ?? 0), 0),
    hasFailure: visibleRecords.some(isFailedToolRecord),
    hasPendingUserAction: visibleRecords.some((record) => record.status === "running" || record.status === "pending"),
  };
}

function isTodoActivityItem(item: TurnActivityItem): boolean {
  const records = item.records ?? [];
  return records.length > 0 && records.every((record) => record.name === "todo_write");
}

function mergeActivityItemRecords(
  existing: TurnActivityItem,
  item: TurnActivityItem,
): void {
  const records = [...(existing.records ?? []), ...(item.records ?? [])];
  const timing = timingFromRecords(records);
  existing.blocks.push(...item.blocks);
  existing.records = records;
  existing.status = statusFromRecords(records);
  existing.startedAt = timing.startedAt;
  existing.finishedAt = timing.finishedAt;
  existing.durationMs = records.reduce((sum, record) => sum + (record.durationMs ?? 0), 0);
  existing.hasFailure = records.some(isFailedToolRecord);
  existing.hasPendingUserAction = records.some((record) => record.status === "running" || record.status === "pending");
}

function aggregateTodoActivityItems(items: TurnActivityItem[]): TurnActivityItem[] {
  let firstTodo: TurnActivityItem | null = null;
  const result: TurnActivityItem[] = [];

  for (const item of items) {
    if (!isTodoActivityItem(item)) {
      result.push(item);
      continue;
    }
    if (!firstTodo) {
      firstTodo = item;
      result.push(item);
      continue;
    }
    mergeActivityItemRecords(firstTodo, item);
  }

  return result;
}

function shouldHideRecoverableLocalProbeActivity(
  item: TurnActivityItem,
  options: {
    hasFinalAnswer?: boolean;
    isStreaming?: boolean;
    terminalFailed?: boolean;
  },
): boolean {
  if (options.terminalFailed) return false;
  if (!options.hasFinalAnswer && !options.isStreaming) return false;
  const records = item.records ?? [];
  return records.length > 0 && records.every(isRecoverableLocalProbeFailureRecord);
}

function aggregateActivityItems(
  items: TurnActivityItem[],
  options: {
    hasFinalAnswer?: boolean;
    isStreaming?: boolean;
    terminalFailed?: boolean;
  } = {},
): TurnActivityItem[] {
  const developerMode = useAppStore.getState().viewMode === "verbose";
  const visibleItems = items.flatMap((item, index) => {
    const withoutGuard = removeInternalGuardRecords(item, developerMode);
    if (!withoutGuard) return [];
    if (!developerMode && shouldHideRecoverableLocalProbeActivity(withoutGuard, options)) return [];
    const isFinalAnswerDraftProgress = isFinalAnswerDraftIterationProgress(withoutGuard, {
      hasFinalAnswer: options.hasFinalAnswer,
      isStreaming: options.isStreaming,
    });
    const visibleThinkingActivity = isVisibleThinkingActivity(withoutGuard);
    const visibleInterleavedAgentMessage = isInterleavedAgentMessageActivity(withoutGuard, index, items);
    const hiddenGenericIteration = !developerMode &&
      isGenericIterationProgress(withoutGuard) &&
      !isFinalAnswerDraftProgress;
    return !hiddenGenericIteration &&
      (!isHiddenProcessActivity(withoutGuard) || visibleThinkingActivity || visibleInterleavedAgentMessage) &&
      (developerMode || !isHiddenSchemaFailureActivity(withoutGuard))
      ? [withoutGuard]
      : [];
  });
  const todoAggregated = aggregateTodoActivityItems(visibleItems);
  const webAggregated = aggregateWebActivityItems(todoAggregated);
  const aggregateKinds = new Set<TurnActivityItem["kind"]>(["workspaceSearch", "fileRead"]);
  const grouped = new Map<TurnActivityItem["kind"], TurnActivityItem>();
  const result: TurnActivityItem[] = [];

  for (const item of webAggregated) {
    if (!aggregateKinds.has(item.kind)) {
      result.push(item);
      continue;
    }
    const existing = grouped.get(item.kind);
    if (!existing) {
      grouped.set(item.kind, item);
      result.push(item);
      continue;
    }
    mergeActivityItemRecords(existing, item);
  }

  return result;
}

function isFinalAnswerDraftIterationProgress(
  item: TurnActivityItem,
  options: {
    hasFinalAnswer?: boolean;
    isStreaming?: boolean;
  },
): boolean {
  if (!options.hasFinalAnswer || !options.isStreaming || item.kind !== "progress") return false;
  const latest = item.progress?.at(-1);
  return latest?.status === "running" && String(latest.phase ?? "") === "iteration";
}

function uniqueFileCount(records: ToolCallRecord[]): number {
  const paths = new Set<string>();
  let fallback = 0;
  for (const r of records) {
    const path = extractToolTarget(r);
    if (path) paths.add(path);
    else fallback += 1;
  }
  return paths.size + fallback;
}

function diffTotals(records: ToolCallRecord[]) {
  return records.reduce(
    (acc, r) => {
      const stats = r.diff ? getToolDiffStats(r.diff) : { plus: 0, minus: 0 };
      return {
        added: acc.added + stats.plus,
        deleted: acc.deleted + stats.minus,
      };
    },
    { added: 0, deleted: 0 },
  );
}

// ── Cell Extractors ─────────────────────────────────────────────────

function isCommandTool(name: string): boolean {
  return /(?:run_command|terminal|shell|bash|powershell|cmd)/i.test(name);
}

function isFileChangeTool(name: string): boolean {
  return (
    name !== "todo_write" &&
    /(?:write|edit|patch|delete|remove|create|move|rename|save)/i.test(name)
  );
}

function nonEmptyOutputLines(text: string): string[] {
  return text
    .split("\n")
    .filter((line) => line.trim());
}

function previewOutputLines(text: string, isRunning: boolean): string[] {
  const lines = nonEmptyOutputLines(text);
  return isRunning ? lines.slice(-12) : lines.slice(0, 8);
}

function extractCommandExitCode(record: ToolCallRecord): number | undefined {
  const text = [record.summary, record.outputPreview, record.stdoutPreview, record.stderrPreview]
    .filter((value): value is string => typeof value === "string" && value.length > 0)
    .join("\n");
  const match = /\bExit code:\s*(-?\d+)\b/i.exec(text);
  if (!match) return record.status === "failed" || record.status === "blocked" ? 1 : 0;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function extractExecCells(
  items: TurnActivityItem[],
): ExecCellState[] {
  const cells: ExecCellState[] = [];
  for (const item of items) {
    if (item.kind !== "commandExecution") continue;
    const records = item.records ?? [];
    for (const record of records) {
      const command =
        extractToolTarget(record) ||
        (typeof record.args?.command === "string"
          ? record.args.command
          : record.name);
      const isRunning = record.status === "running" || record.status === "pending";
      const hasSplitOutput =
        typeof record.stdoutPreview === "string" ||
        typeof record.stderrPreview === "string";
      const legacyOutputText = (record.outputPreview || record.summary || "")
        .trimEnd();
      const stdoutText = (
        record.stdoutPreview ??
        (hasSplitOutput ? "" : legacyOutputText)
      ).trimEnd();
      const stderrText = (record.stderrPreview ?? "").trimEnd();
      cells.push({
        kind: "exec",
        id: record.id,
        command,
        status:
          isRunning
            ? "running"
            : record.status === "failed" || record.status === "blocked"
              ? "failed"
              : "success",
        exitCode: extractCommandExitCode(record),
        stdoutPreview: previewOutputLines(stdoutText, isRunning),
        stderrPreview: previewOutputLines(stderrText, isRunning),
        stdoutFull: stdoutText || undefined,
        stderrFull: stderrText || undefined,
        durationMs: record.durationMs,
        collapsed: !(isRunning || record.status === "failed"),
        createdAt: record.startedAt,
        completedAt: record.finishedAt,
      });
    }
  }
  return cells;
}

function buildExecCellsForItem(item: TurnActivityItem): ExecCellState[] {
  return extractExecCells([item]);
}

function buildDiffCellForFileChangeItems(
  fileChangeItems: TurnActivityItem[],
): DiffCellState | null {
  if (fileChangeItems.length === 0) return null;

  const allRecords = fileChangeItems.flatMap((i) => i.records ?? []).filter((record) => (
    record.name !== "todo_write" &&
    (isFileChangeTool(record.name) || Boolean(record.diff)) &&
    (Boolean(extractToolTarget(record)) || Boolean(record.diff?.patch))
  ));
  if (allRecords.length === 0) return null;
  const totals = diffTotals(allRecords);

  const files: DiffFileChange[] = [];
  const seen = new Set<string>();
  for (const record of allRecords) {
    const path = extractToolTarget(record) || `${record.name} result`;
    if (seen.has(path)) continue;
    seen.add(path);
    const stats = record.diff ? getToolDiffStats(record.diff) : { plus: 0, minus: 0 };
    files.push({
      path,
      patch: record.diff?.patch,
      additions: stats.plus,
      deletions: stats.minus,
      isLarge: stats.plus + stats.minus > 200,
    });
  }

  return {
    kind: "diff",
    id: `diff-${fileChangeItems[0]?.id ?? "turn"}`,
    status: "updated",
    files,
    summary: {
      added: totals.added,
      deleted: totals.deleted,
      modifiedFiles: files.length,
    },
    collapsed: files.length > 3,
    createdAt: fileChangeItems[0]?.startedAt ?? Date.now(),
  };
}

function buildThinkingCell(
  item: TurnActivityItem,
  assistantMsg: ChatMessage,
): ThinkingCellState {
  return {
    kind: "thinking",
    id: `${assistantMsg.id}-thinking-${item.id}`,
    content: item.content ?? "",
    source: thinkingSourceForItem(item),
    createdAt: item.startedAt ?? assistantMsg.timestamp,
  };
}

function buildProcessNoteCell(
  item: TurnActivityItem,
  assistantMsg: ChatMessage,
): ThinkingCellState {
  return {
    kind: "thinking",
    id: `${assistantMsg.id}-process-${item.id}`,
    content: item.content ?? "",
    source: thinkingSourceForItem(item),
    createdAt: item.startedAt ?? assistantMsg.timestamp,
  };
}

function buildActivityCell(
  item: TurnActivityItem,
  options: {
    lang: Lang;
    assistantMsg: ChatMessage;
    hasFinalAnswer: boolean;
    isStreaming: boolean;
  },
): ActivityCellState {
  const { lang, assistantMsg, hasFinalAnswer, isStreaming } = options;
  const records = item.records ?? [];
  const isRunning = item.status === "running" || item.status === "pending";
  const isFinalAnswerDraftIteration = isFinalAnswerDraftIterationProgress(item, {
    hasFinalAnswer,
    isStreaming,
  });
  // Populate progress with real-time count for aggregatable activities.
  let progress: ActivityCellState["progress"];
  if (item.progress?.length) {
    progress = { text: isFinalAnswerDraftIteration ? undefined : item.progress.at(-1)?.summary };
  } else if (isRunning && records.length > 0) {
    const doneCount = records.filter((r) => r.status === "success").length;
    progress = { current: doneCount, total: undefined, text: t(lang, `已完成 ${doneCount} 个`, `${doneCount} done`) };
  }
  const hasItemFailure = item.hasFailure || item.status === "failed" || item.status === "blocked";
  const hasItemSuccess = records.some((r) => r.status === "success");
  const hasPartial = item.status === "partial" || records.some((r) => r.status === "partial");
  const isPartialFailure = hasItemFailure && hasItemSuccess;
  const visibleToolRecords = isPartialFailure ? successfulRecords(records) : item.records;
  return {
    kind: "activity",
    id: item.id,
    activityKind: item.kind,
    title: isFinalAnswerDraftIteration
      ? t(lang, "正在输出最终回答", "Writing final answer")
      : getActivityTitle(item, lang),
    subtitle: getActivitySubtitle(item, lang),
    status: isPartialFailure ? "done" : mapActivityStatus(item.status),
    collapsed:
      !hasPartial &&
      (!item.hasFailure || isPartialFailure) &&
      item.status !== "running" &&
      item.status !== "pending",
    toolCallRecords: visibleToolRecords,
    progress,
    startedAt: item.startedAt ?? assistantMsg.timestamp,
    completedAt: item.finishedAt,
  };
}

function buildOrderedProcessCells(
  items: TurnActivityItem[],
  options: {
    lang: Lang;
    assistantMsg: ChatMessage;
    hasFinalAnswer: boolean;
    isStreaming: boolean;
    fallbackErrorCells?: ErrorCellState[];
  },
): {
  orderedCells: CommittedCellState[];
  activityCells: ActivityCellState[];
  execCells: ExecCellState[];
  diffCells: DiffCellState[];
} {
  const orderedCells: CommittedCellState[] = [];
  const activityCells: ActivityCellState[] = [];
  const execCells: ExecCellState[] = [];
  const diffCells: DiffCellState[] = [];
  let emittedAnyErrors = false;

  for (const [index, item] of items.entries()) {
    if (isInterleavedAgentMessageActivity(item, index, items)) {
      const cell = buildProcessNoteCell(item, options.assistantMsg);
      orderedCells.push(cell);
      const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
      if (errorCells.length > 0) {
        orderedCells.push(...errorCells);
        emittedAnyErrors = true;
      }
      continue;
    }

    if (isVisibleThinkingActivity(item)) {
      const cell = buildThinkingCell(item, options.assistantMsg);
      orderedCells.push(cell);
      const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
      if (errorCells.length > 0) {
        orderedCells.push(...errorCells);
        emittedAnyErrors = true;
      }
      continue;
    }

    if (item.kind === "commandExecution") {
      const cells = buildExecCellsForItem(item);
      orderedCells.push(...cells);
      execCells.push(...cells);
      const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
      if (errorCells.length > 0) {
        orderedCells.push(...errorCells);
        emittedAnyErrors = true;
      }
      continue;
    }

    if (item.kind === "fileChange") {
      const diffCell = buildDiffCellForFileChangeItems([item]);
      if (diffCell) {
        orderedCells.push(diffCell);
        diffCells.push(diffCell);
        const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
        if (errorCells.length > 0) {
          orderedCells.push(...errorCells);
          emittedAnyErrors = true;
        }
        continue;
      }
    }

    const activityCell = buildActivityCell(item, options);
    orderedCells.push(activityCell);
    activityCells.push(activityCell);
    const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
    if (errorCells.length > 0) {
      orderedCells.push(...errorCells);
      emittedAnyErrors = true;
    }
  }

  if (!emittedAnyErrors && options.fallbackErrorCells?.length) {
    orderedCells.push(...options.fallbackErrorCells);
    emittedAnyErrors = true;
  }

  if (!emittedAnyErrors && options.assistantMsg.terminalStatus === "failed") {
    orderedCells.push({
      kind: "error",
      id: `err-${options.assistantMsg.id}`,
      title: "处理失败",
      message: options.assistantMsg.content || "Agent 处理过程中发生错误",
      source: "agent",
      recoverable: true,
      createdAt: options.assistantMsg.timestamp,
    });
  }

  return {
    orderedCells,
    activityCells,
    execCells,
    diffCells,
  };
}

function extractErrorCells(
  items: TurnActivityItem[],
  assistantMsg: ChatMessage,
  hasFinalAnswer = false,
): ErrorCellState[] {
  const cells = items.flatMap((item) => buildErrorCellsForItem(item, assistantMsg, hasFinalAnswer));
  if (cells.length === 0 && assistantMsg.terminalStatus === "failed") {
    cells.push({
      kind: "error",
      id: `err-${assistantMsg.id}`,
      title: "处理失败",
      message: assistantMsg.content || "Agent 处理过程中发生错误",
      source: "agent",
      recoverable: true,
      createdAt: assistantMsg.timestamp,
    });
  }
  return cells;
}

function buildErrorCellsForItem(
  item: TurnActivityItem,
  assistantMsg: ChatMessage,
  hasFinalAnswer = false,
): ErrorCellState[] {
  const cells: ErrorCellState[] = [];
  const developerMode = useAppStore.getState().viewMode === "verbose";
  const userFacingToolError = (record: ToolCallRecord): string => {
    const raw = String(record.summary ?? "");
    if (!developerMode && record.userSummary) {
      return record.userSummary.slice(0, 300);
    }
    if (!developerMode && record.errorInfo?.user_message) {
      return record.errorInfo.user_message.slice(0, 300);
    }
    if (developerMode) return raw.slice(0, 300);
    if (record.name === "read_artifact" && /missing required (?:argument\(s\): )?(?:\['artifact_id'\]|artifact_id)/i.test(raw)) {
      return "读取文件内容失败：缺少产物 ID。";
    }
    if (record.name === "read_file" && /missing required (?:argument\(s\): )?(?:\['file_path'\]|file_path|path)/i.test(raw)) {
      return "读取文件失败：缺少文件路径。";
    }
    if (/missing required (?:argument\(s\): )?(?:\['url'\]|url)/i.test(raw)) {
      return "读取网页失败：缺少 URL。";
    }
    if (/missing required (?:argument\(s\): )?(?:\['query'\]|query)/i.test(raw)) {
      return "搜索失败：缺少搜索关键词。";
    }
    if (/Skipped repeated failed tool call|repeated failed tool call|反复尝试|重复/i.test(raw)) {
      return "工具调用已停止：模型连续尝试了相同的无效调用。";
    }
    if (!developerMode && isPermissionBlockedRecord(record)) {
      return record.name === "read_file"
        ? "读取文件被阻止：目标路径不在允许范围。"
        : "工具调用被权限策略阻止。";
    }
    return normalizeAgentErrorMessage(raw).slice(0, 300);
  };
  const userFacingToolTitle = (record: ToolCallRecord): string => {
    if (!developerMode && isPermissionBlockedRecord(record)) {
      return record.name === "read_file" ? "读取文件被权限策略阻止" : "工具调用被权限策略阻止";
    }
    if (!developerMode && record.name === "read_artifact") return "读取文件内容失败";
    if (!developerMode && record.name === "read_file") return "读取文件失败";
    return `工具 ${record.name} 执行失败`;
  };
  const isUserFacingToolFailure = (record: ToolCallRecord): boolean =>
    Boolean(record.userSummary || record.errorInfo?.user_message) ||
    isPermissionBlockedRecord(record) ||
    isMissingRequiredArgumentRecord(record);
  const isProxyOrNetworkFailure = (record: ToolCallRecord): boolean => {
    const text = `${record.summary ?? ""} ${record.displaySummary ?? ""}`;
    if (record.providerErrorType === "network") return true;
    return /407|proxy authentication required|代理鉴权失败|代理认证失败/i.test(text);
  };

  // Tool failures — hermes-style: individual tool errors are internal
  // (the model handles them by adapting strategy). Only surface to user when:
  //   1. Network/proxy failure (user-actionable — may need to fix connectivity)
  //   2. Developer mode (show everything for debugging)
  //   3. Entire turn failed terminally (fallback: show what went wrong)
  const turnFailedTerminally = assistantMsg.terminalStatus === "failed";
  const hasNetworkFailure = (item.records ?? []).some((record) =>
    isFailedToolRecord(record) && isProxyOrNetworkFailure(record),
  );
  if (!item.hasFailure && item.status !== "failed" && item.status !== "blocked" && !hasNetworkFailure)
    return cells;
  for (const record of item.records ?? []) {
    const rawFailure = record.status === "failed" || record.status === "blocked";
    if (developerMode ? !rawFailure : !isFailedToolRecord(record))
      continue;
    if (!developerMode && isRepeatGuardRecord(record)) continue;
    const networkFailure = isProxyOrNetworkFailure(record);
    const permissionFailure = isPermissionBlockedRecord(record);
    const missingRequiredArgument = isMissingRequiredArgumentRecord(record);
    const handledLocalProbeFailure = (
      (hasFinalAnswer || Boolean(assistantMsg.isStreaming)) &&
      isLocalExplorationTool(record) &&
      !networkFailure &&
      !permissionFailure &&
      !missingRequiredArgument &&
      !turnFailedTerminally
    );
    if (!developerMode && handledLocalProbeFailure) continue;
    const userFacingFailure = isUserFacingToolFailure(record);
    // Suppress internal tool errors unless actionable, developer-visible, or terminal.
    if (!developerMode && !networkFailure && !turnFailedTerminally && !userFacingFailure) continue;
    cells.push({
      kind: "error",
      id: `err-${record.id}`,
      title: networkFailure ? "实时信息获取失败" : userFacingToolTitle(record),
      message: networkFailure
        ? normalizeAgentErrorMessage(record.summary ?? "")
        : userFacingToolError(record),
      source: networkFailure ? "network" : permissionFailure ? "permission" : "tool",
      recoverable: true,
      rawError: developerMode ? record.developerDetail ?? record.errorInfo?.developer_detail ?? record.summary : undefined,
      createdAt: record.finishedAt ?? Date.now(),
    });
  }

  return cells;
}

function extractPlanCell(
  items: TurnActivityItem[],
  assistantMsg: ChatMessage,
): Exclude<HistoryCellState, UserMessageCellState | StreamingAssistantTailCellState>[] {
  const hasPlanningActivity = items.some((i) => i.kind === "planning");
  if (!hasPlanningActivity) return [];

  const globalPlan = useAppStore.getState().plan;
  if (!globalPlan) return [];

  // Map backend status ("draft" | "accepted" | "executing" | "completed" | "cancelled")
  // to cell status ("proposed" | "approved" | "executing" | "completed" | "cancelled")
  let cellStatus: PlanCellState["status"] = "proposed";
  if (globalPlan.status === "accepted") {
    cellStatus = "approved";
  } else if (globalPlan.status === "executing") {
    cellStatus = "executing";
  } else if (globalPlan.status === "completed") {
    cellStatus = "completed";
  } else if (globalPlan.status === "cancelled") {
    cellStatus = "cancelled";
  }

  return [
    {
      kind: "plan",
      id: `plan-${globalPlan.planId}-${assistantMsg.id}`,
      planId: globalPlan.planId,
      title: "执行计划",
      status: cellStatus,
      requiresApproval: globalPlan.status === "draft",
      steps: globalPlan.steps.map((s, idx) => ({
        id: s.id || `step-${idx}`,
        title: s.title,
        description: s.detail,
        status:
          s.status === "done"
            ? "completed"
            : s.status === "running"
              ? "in_progress"
              : s.status === "failed"
                ? "blocked"
                : s.status === "skipped"
                  ? "cancelled"
                  : "pending",
      })),
      createdAt: assistantMsg.timestamp,
      updatedAt: Date.now(),
    },
  ];
}

function buildTurnSummaryCell(
  assistantMsg: ChatMessage,
  cells: {
    activityCells: ActivityCellState[];
    execCells: ExecCellState[];
    diffCells: DiffCellState[];
    errorCells: ErrorCellState[];
  },
): TurnSummaryCellState | null {
  const items: TurnSummaryCellState["items"] = [];
  const categories = new Set<string>();
  const runningExec = cells.execCells.find((cell) => cell.status === "running");
  if (runningExec) {
    categories.add("command");
    items.push({
      kind: "command",
      label: "Running",
      detail: shortCommand(runningExec.command),
      tone: "neutral",
    });
  } else if (cells.execCells.length > 0) {
    categories.add("command");
    const failed = cells.execCells.filter((cell) => cell.status === "failed").length;
    items.push({
      kind: "command",
      label: failed ? `${failed}/${cells.execCells.length} commands failed` : `${cells.execCells.length} command${cells.execCells.length === 1 ? "" : "s"}`,
      tone: failed ? "danger" : "success",
    });
  }

  const diff = cells.diffCells[0];
  if (diff) {
    categories.add("diff");
    items.push({
      kind: "diff",
      label: `${diff.summary.modifiedFiles} file${diff.summary.modifiedFiles === 1 ? "" : "s"}`,
      detail: `+${diff.summary.added} -${diff.summary.deleted}`,
      tone: "neutral",
    });
  }

  const sourceCount = cells.activityCells
    .filter((cell) => cell.activityKind === "webSearch" || cell.activityKind === "fileRead" || cell.activityKind === "workspaceSearch")
    .reduce((total, cell) => total + Math.max(1, cell.toolCallRecords?.length ?? 0), 0);
  if (sourceCount) {
    categories.add("source");
    items.push({
      kind: "source",
      label: `${sourceCount} source${sourceCount === 1 ? "" : "s"}`,
      tone: "neutral",
    });
  }

  const runningActivity = cells.activityCells.find((cell) => cell.status === "running");
  if (runningActivity && !runningExec && !isSourceSummaryActivity(runningActivity)) {
    categories.add("activity");
    items.push({
      kind: "activity",
      label: "Working",
      detail: runningActivity.title,
      tone: "neutral",
    });
  }

  if (cells.errorCells.length > 0) {
    categories.add("error");
    items.push({
      kind: "error",
      label: `${cells.errorCells.length} issue${cells.errorCells.length === 1 ? "" : "s"}`,
      detail: cells.errorCells[0]?.title,
      tone: "danger",
    });
  }

  if (!runningExec && cells.errorCells.length === 0 && categories.size < 2) return null;
  const status: TurnSummaryCellState["status"] = assistantMsg.isStreaming
    ? "running"
    : assistantMsg.terminalStatus === "failed" || cells.errorCells.length > 0
      ? "failed"
      : "completed";
  return {
    kind: "turn_summary",
    id: `${assistantMsg.id}-summary`,
    status,
    items: items.slice(0, 4),
    createdAt: assistantMsg.timestamp,
  };
}

function isSourceSummaryActivity(cell: ActivityCellState): boolean {
  return cell.activityKind === "webSearch" || cell.activityKind === "fileRead" || cell.activityKind === "workspaceSearch";
}

function buildStatusNoticeCell(
  msg: ChatMessage,
): StatusNoticeCellState {
  const content = msg.content;
  let tone: StatusNoticeCellState["tone"] = "info";
  let title = content;
  let message: string | undefined;

  // Detect compaction by the stable message id, not by substring matching the
  // content: a command result like /help lists "/compact" as an available
  // command, which would otherwise be misclassified as a compaction notice and
  // hide the real text.
  const isCompaction = msg.id === "system-compact-status" || msg.id.startsWith("system-compact");
  if (isCompaction) {
    tone = "info";
    title = "上下文已压缩";
  } else if (content.startsWith("Error:") || content.startsWith("LLM API")) {
    tone = "danger";
    title = content;
  } else {
    // Split multi-line notices (command results: "Command result: /x\n<body>")
    // into a bold title line + body so they render legibly.
    const newlineIdx = content.indexOf("\n");
    if (newlineIdx > 0) {
      title = content.slice(0, newlineIdx).trim();
      message = content.slice(newlineIdx + 1).trim() || undefined;
    }
  }
  return {
    kind: "status_notice",
    id: msg.id,
    tone,
    title,
    message,
    createdAt: msg.timestamp,
  };
}

// ── Turn Builder ────────────────────────────────────────────────────

function buildTurn(
  userMsg: ChatMessage | null,
  assistantMsg: ChatMessage | null,
): ChatTurnState {
  const userCell: UserMessageCellState | null = userMsg
    ? {
        kind: "user_message",
        id: userMsg.id,
        content: userMsg.content,
        attachments: userMsg.attachmentRefs?.map((a) => ({
          name: a.name,
          type: a.mediaType,
          size: a.sizeBytes,
        })),
        createdAt: userMsg.timestamp,
      }
    : null;

  if (!assistantMsg) {
    return {
      id: userMsg?.id ?? `turn-empty-${Date.now()}`,
      userCell,
      committedCells: [],
      activeCell: null,
      finalAnswerCell: null,
      status: "completed",
      startedAt: userMsg?.timestamp ?? Date.now(),
    };
  }

  // Activity labels follow the conversation language: detect from the user
  // message (falling back to the assistant's own text for hydrated
  // assistant-only turns) so an English chat doesn't show Chinese labels.
  const lang = detectLang(userMsg?.content || assistantMsg.content);
  const blocks = getContentBlocks(assistantMsg);
  const projection = projectTurn(blocks, {
    isStreaming: assistantMsg.isStreaming,
    isThinkingStreaming: assistantMsg.isThinkingStreaming,
    terminalStatus:
      assistantMsg.terminalStatus === "failed" ? "failed" : undefined,
    artifactCount: assistantMsg.artifacts?.length,
  });
  const effectiveCitations = mergeCitationsWithWebSearchFallback(
    assistantMsg.citations,
    blocks,
    projection.finalAnswer,
  );

  const hasFinalAnswer = Boolean(projection.finalAnswer.trim());
  const errorCells = extractErrorCells(
    projection.activityItems,
    assistantMsg,
    hasFinalAnswer,
  );
  const projectedActivityItems = aggregateActivityItems(projection.activityItems, {
    hasFinalAnswer,
    isStreaming: Boolean(assistantMsg.isStreaming),
    terminalFailed: assistantMsg.terminalStatus === "failed",
  });
  const {
    orderedCells: processCells,
    activityCells,
    execCells,
    diffCells,
  } = buildOrderedProcessCells(projectedActivityItems, {
    lang,
    assistantMsg,
    hasFinalAnswer,
    isStreaming: Boolean(assistantMsg.isStreaming),
    fallbackErrorCells: errorCells,
  });

  const planCells = extractPlanCell(projectedActivityItems, assistantMsg);
  const summaryCell = buildTurnSummaryCell(assistantMsg, {
    activityCells,
    execCells,
    diffCells,
    errorCells,
  });

  // Final answer
  const finalAnswerCell: AssistantMarkdownCellState | null = projection
    .finalAnswer
      ? {
        kind: "assistant_markdown",
        id: `${assistantMsg.id}-final`,
        messageId: assistantMsg.id,
        markdownSource: projection.finalAnswer,
        citations: effectiveCitations,
        phase: "final",
        copyable: true,
        createdAt: assistantMsg.timestamp,
        isStreaming: Boolean(assistantMsg.isStreaming),
      }
    : null;

  // Determine active cell during streaming
  let activeCell: HistoryCellState | null = null;
  const committedCells: CommittedCellState[] = [];

  if (assistantMsg.isStreaming) {
    const hasActivityRunning = projectedActivityItems.some(
      (i) => i.status === "running" || i.status === "pending",
    );

    // Check if text is being streamed as final answer (no tool activity running)
    const streamingText =
      assistantMsg.content.trim() ||
      blocks.some((b) => b.type === "text" && b.content.trim());

    if (streamingText && !hasActivityRunning && !projection.finalAnswer) {
      // Pure text streaming — show StreamingAssistantTailCell as active
      activeCell = {
        kind: "streaming_assistant_tail",
        id: `${assistantMsg.id}-tail`,
        partialMarkdown: assistantMsg.content,
        updatedAt: Date.now(),
      };
      if (summaryCell) committedCells.push(summaryCell);
      committedCells.push(...processCells, ...planCells);
    } else {
      if (summaryCell) committedCells.push(summaryCell);
      committedCells.push(...processCells);
      committedCells.push(...planCells);
    }
  } else {
    // Not streaming — all cells are committed
    if (summaryCell) committedCells.push(summaryCell);
    committedCells.push(...processCells);
    committedCells.push(...planCells);
  }

  return {
    id: assistantMsg.id,
    userCell,
    committedCells,
    activeCell,
    finalAnswerCell,
    status: assistantMsg.isStreaming
      ? "streaming"
      : assistantMsg.terminalStatus === "failed"
        ? "failed"
        : "completed",
    startedAt: userMsg?.timestamp ?? assistantMsg.timestamp,
    completedAt: assistantMsg.isStreaming
      ? undefined
      : assistantMsg.timestamp,
  };
}

// ── Public API ──────────────────────────────────────────────────────

export function projectMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
): ChatTurnState[] {
  const turns: ChatTurnState[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];

    if (msg.role === "user") {
      const userMsg = msg;
      const assistantMsg =
        i + 1 < messages.length && messages[i + 1]?.role === "assistant"
          ? messages[i + 1]
          : null;
      const turn = buildTurn(userMsg, assistantMsg);
      // Override streaming status with global flag
      if (assistantMsg && turn.status === "streaming" && !isStreaming) {
        turn.status = "completed";
      }
      turns.push(turn);
      i += assistantMsg ? 2 : 1;
    } else if (msg.role === "system") {
      const notice = buildStatusNoticeCell(msg);
      // Always a standalone notice turn. Conversation-level notices (command
      // results, compaction status) must render chronologically after the
      // previous turn's final answer — absorbing them into the previous
      // turn's committedCells paints them above the answer, out of view.
      turns.push({
        id: `notice-${msg.id}`,
        userCell: null,
        committedCells: [notice],
        activeCell: null,
        finalAnswerCell: null,
        status: "completed",
        startedAt: msg.timestamp,
      });
      i += 1;
    } else if (msg.role === "assistant") {
      turns.push(buildTurn(null, msg));
      i += 1;
    } else {
      // Unknown role: skip it rather than breaking the transcript.
      i += 1;
    }
  }

  return turns;
}

function findProjectedTurnStartIndexes(messages: ChatMessage[]): number[] {
  const starts: number[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];
    if (msg.role === "user") {
      starts.push(i);
      const assistantMsg =
        i + 1 < messages.length && messages[i + 1]?.role === "assistant"
          ? messages[i + 1]
          : null;
      i += assistantMsg ? 2 : 1;
      continue;
    }
    if (msg.role === "assistant" || msg.role === "system") {
      // Every system message is its own notice turn (see projectMessagesToTurns),
      // so it must count as a turn start or the recent-window slice misaligns.
      starts.push(i);
    }
    i += 1;
  }

  return starts;
}

export function projectRecentMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
  recentTurnLimit: number,
): { turns: ChatTurnState[]; hiddenTurnCount: number; totalTurnCount: number } {
  const starts = findProjectedTurnStartIndexes(messages);
  const totalTurnCount = starts.length;
  const limit = Math.max(0, Math.floor(recentTurnLimit));

  if (limit === 0 || totalTurnCount <= limit) {
    return {
      turns: projectMessagesToTurns(messages, isStreaming),
      hiddenTurnCount: 0,
      totalTurnCount,
    };
  }

  const firstVisibleTurn = totalTurnCount - limit;
  const firstVisibleMessageIndex = starts[firstVisibleTurn] ?? 0;
  return {
    turns: projectMessagesToTurns(messages.slice(firstVisibleMessageIndex), isStreaming),
    hiddenTurnCount: firstVisibleTurn,
    totalTurnCount,
  };
}

export function buildChatSurfaceState(
  messages: ChatMessage[],
  isStreaming: boolean,
  currentTurnId: string | null = null,
): ChatSurfaceState {
  return {
    turns: projectMessagesToTurns(messages, isStreaming),
    isStreaming,
    currentTurnId,
  };
}
