/**
 * Chat surface state projection — converts flat ChatMessage[] into
 * cell-based ChatTurnState[] for Codex-like rendering.
 *
 * This is the bridge between the existing message-based store and the
 * new cell architecture. It wraps projectTurn to extract
 * ActivityCells, ExecCells, DiffCells, ErrorCells, and the final answer.
 */

import type { ChatMessage, ContentBlock } from "../stores/types";
import { useAppStore } from "../stores";
import { getContentBlocks } from "../lib/content-blocks";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { mergeCitationsWithWebSearchFallback } from "./citationProjection";
import {
  isGenericProcessPlaceholder,
  isInternalCollaborationNarration,
} from "../lib/turn-projection";
import {
  projectChatTurn,
  refreshChatActivityItem,
  type ChatActivityItem as TurnActivityItem,
  type ChatActivityStatus as TurnActivityStatus,
} from "./chatActivityProjection";
import {
  coordinatorNoticeKind,
  isInternalCoordinatorNotice,
  userFacingCoordinatorNotice,
} from "../lib/collaborationDisplay";
import {
  getToolDiffStats,
  isFileChangeToolRecord,
  isFileReadToolRecord,
  isWebFetchToolRecord,
  isWebSearchToolRecord,
  isWorkspaceSearchToolRecord,
  type ToolCallRecord,
} from "../lib/tool-call-reducer";
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

// ── Activity Title Helpers ──────────────────────────────────────────

function getActivityTitle(item: TurnActivityItem, lang: Lang = "zh"): string {
  const records = item.records ?? [];
  const first = records[0];
  const isRunning = item.status === "running" || item.status === "pending";
  const hasFailure =
    item.status === "failed" || item.status === "blocked" || item.status === "timeout" || item.hasFailure;
  const hasPartial = item.status === "partial" || records.some((r) => r.status === "partial");
  const hasSuccess = records.some((r) => r.status === "success");
  const partialFailure = hasFailure && hasSuccess;
  const positiveRecords = partialFailure ? successfulRecords(records) : records;
  switch (item.kind) {
    case "reasoning":
    case "planning":
    case "processNote":
    case "providerReasoning":
      return "";
    case "agentMessage":
      return "";
    case "skill": {
      const name = item.skillName || "unknown";
      if (hasFailure) return t(lang, `能力加载失败：${name}`, `Skill failed: ${name}`);
      if (isRunning) {
        return item.triggerMode === "implicit"
          ? t(lang, `自动匹配能力：${name}`, `Matched skill: ${name}`)
          : t(lang, `准备使用能力：${name}`, `Using skill: ${name}`);
      }
      return humanizeSkillTitle(item.title, name, lang) || t(lang, `已加载能力：${name}`, `Using skill: ${name}`);
    }
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
          ? t(lang, "正在搜索网页", "Searching the web")
          : t(lang, "正在读取网页", "Reading web pages");
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
      if (isRunning) return t(lang, "正在读取文件", "Reading files");
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
      const target = extractToolTarget(first);
      const verbZh = "修改";
      const verbEn = "Editing";
      const pastEn = "Edited";
      if (isRunning) {
        return target
          ? t(lang, `正在${verbZh} ${target}`, `${verbEn} ${target}`)
          : t(lang, `正在${verbZh}文件`, `${verbEn} files`);
      }
      if (hasFailure && !hasSuccess) {
        return target
          ? t(lang, `${verbZh}${target}失败`, `${verbEn} ${target} failed`)
          : t(lang, `${verbZh}文件失败`, `${verbEn} files failed`);
      }
      if (count === 1) {
        return target ? t(lang, `已${verbZh} ${target}`, `${pastEn} ${target}`) : t(lang, `已${verbZh}文件`, `${pastEn} file`);
      }
      return t(lang, `已${verbZh} ${count} 个文件`, `${pastEn} ${count} files`);
    }
    case "mcpToolCall": {
      const label = first?.name.startsWith("mcp__")
        ? first.name.split("__").slice(1, 3).join("/")
        : first?.name;
      if (isRunning) return label ? t(lang, `正在调用 MCP ${label}`, `Calling MCP ${label}`) : t(lang, "正在调用 MCP", "Calling MCP");
      return label ? t(lang, `已调用 MCP ${label}`, `Called MCP ${label}`) : t(lang, "已调用 MCP", "Called MCP");
    }
    case "progress":
      return item.progress?.at(-1)?.label || t(lang, "状态", "Status");
    default:
      if (records.length > 0 && records.every((record) => record.name === "todo_write")) {
        if (isRunning) return t(lang, "正在更新任务清单", "Updating task list");
        return t(lang, "已更新任务清单", "Updated task list");
      }
      if (first?.name === "task") return isRunning ? t(lang, "正在启动子代理", "Starting subagent") : t(lang, "已启动子代理", "Started subagent");
      if (first?.name === "workflow") return isRunning ? t(lang, "正在启动工作流", "Starting workflow") : t(lang, "已启动工作流", "Started workflow");
      if (first?.displayHint) return first.displayHint;
      const fallbackName = first ? userFacingGenericToolName(first.name, lang) : "";
      if (isRunning) return fallbackName ? t(lang, `正在调用 ${fallbackName}`, `Calling ${fallbackName}`) : t(lang, "正在调用工具", "Calling tool");
      return fallbackName ? t(lang, `已调用 ${fallbackName}`, `Called ${fallbackName}`) : t(lang, "已调用工具", "Called tool");
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
    const sources = webSourceCount(records);
    if (isRunning) {
      if (searches) return sources
        ? t(lang, `已完成 ${searches} 次搜索 · ${sources} 个来源`, `${searches} searches · ${sources} sources`)
        : t(lang, `已完成 ${searches} 次搜索`, `${searches} searches done`);
      if (pages) return t(lang, `已读取 ${pages} 个页面`, `${pages} pages read`);
      return records.length ? t(lang, `${records.length} 个来源`, `${records.length} sources`) : "";
    }
    if (searches) return sources
      ? t(lang, `${searches} 次搜索 · ${sources} 个来源`, `${searches} searches · ${sources} sources`)
      : t(lang, `${searches} 次搜索`, `${searches} searches`);
    if (pages) return t(lang, `${pages} 个页面`, `${pages} pages`);
    return records.length ? t(lang, `${records.length} 个来源`, `${records.length} sources`) : "";
  }
  if (item.kind === "skill") {
    if (item.reason) return item.reason.replace(/\bskill\b/gi, "能力");
    if (item.triggerMode === "implicit") return t(lang, "自动匹配", "implicit");
    if (item.triggerMode === "model") return t(lang, "模型调用", "model");
    if (item.triggerMode === "explicit") return t(lang, "显式调用", "explicit");
    return "";
  }
  return "";
}

function humanizeSkillTitle(title: string | undefined, skillName: string, lang: Lang): string {
  const raw = String(title || "").trim();
  if (!raw) return "";
  if (lang !== "zh" && !CJK_RE.test(raw)) return raw;
  return raw
    .replace(/^准备使用\s+Skill[:：]\s*/i, "准备使用能力：")
    .replace(/^使用\s+Skill[:：]\s*/i, "已加载能力：")
    .replace(/^已加载\s+Skill[:：]\s*/i, "已加载能力：")
    .replace(/^自动匹配\s+Skill[:：]\s*/i, "自动匹配能力：")
    .replace(/^跳过\s+Skill[:：]\s*/i, "跳过能力：")
    .replace(/^Skill[:：]\s*/i, `能力：${skillName}`);
}

function successfulRecords(records: ToolCallRecord[]): ToolCallRecord[] {
  return records.filter((record) => record.status === "success");
}

function webSourceCount(records: ToolCallRecord[]): number {
  const sources = new Set<string>();
  for (const record of records) {
    if (record.sourceUrl) sources.add(record.sourceUrl);
    const preview = String(record.contentPreview || record.outputPreview || "").trim();
    if (!preview.startsWith("[") && !preview.startsWith("{")) continue;
    try {
      const parsed = JSON.parse(preview) as unknown;
      const items = Array.isArray(parsed) ? parsed : [parsed];
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const url = String((item as Record<string, unknown>).url || "").trim();
        if (url) sources.add(url);
      }
    } catch {
      // Search previews from custom providers may be plain text.
    }
  }
  return sources.size;
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
  if (record.inputSummary) return record.inputSummary;
  if (record.displayHint) return record.displayHint;
  if (record.displaySummary) return record.displaySummary;
  const args = record.args ?? {};
  const path = args.file_path ?? args.path ?? args.target ?? args.filename;
  if (typeof path === "string") return path;
  const description = args.description ?? args.title ?? args.objective ?? args.task_id ?? args.workflow_id ?? args.name;
  if (typeof description === "string") return description.length > 80 ? `${description.slice(0, 77)}...` : description;
  const command = args.command;
  if (typeof command === "string") {
    return command.length > 80 ? `${command.slice(0, 77)}...` : command;
  }
  return "";
}

function userFacingGenericToolName(name: string, lang: Lang): string {
  const normalized = name.toLowerCase();
  if (normalized === "task") return t(lang, "子代理", "subagent");
  if (normalized === "workflow") return t(lang, "工作流", "workflow");
  if (normalized.startsWith("task_")) return t(lang, "协作任务", "workflow task");
  if (normalized.startsWith("team_")) return t(lang, "代理团队", "agent team");
  if (normalized === "send_message" || normalized === "message_list") return t(lang, "代理消息", "agent message");
  return name.replace(/^mcp__[^_]+__/i, "").replace(/_/g, " ");
}

function isWebSearchRecord(record: ToolCallRecord): boolean {
  return isWebSearchToolRecord(record);
}

function isWebFetchRecord(record: ToolCallRecord): boolean {
  return isWebFetchToolRecord(record);
}

function isLocalExplorationTool(record: ToolCallRecord): boolean {
  return isFileReadToolRecord(record) || isWorkspaceSearchToolRecord(record);
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
  return record.providerErrorType === "network";
}

function isPermissionBlockedRecord(record: ToolCallRecord): boolean {
  return record.errorKind === "permission_required" || record.projection === "approval";
}

function isMissingRequiredArgumentRecord(record: ToolCallRecord): boolean {
  return record.errorKind === "validation_error";
}

function isRepeatGuardRecord(record: ToolCallRecord): boolean {
  return record.errorKind === "repeat_guard";
}

function isGenericToolFailureCopy(value: string | undefined | null): boolean {
  const text = String(value ?? "").trim();
  return /^(?:工具执行失败。?|Tool execution failed\.?)$/i.test(text);
}

function isRecoverableLocalProbeFailureRecord(record: ToolCallRecord): boolean {
  if (record.status !== "failed" && record.status !== "blocked") return false;
  if (!isLocalExplorationTool(record)) return false;
  if (isNetworkLikeToolFailure(record)) return false;
  if (isPermissionBlockedRecord(record)) return false;
  if (isMissingRequiredArgumentRecord(record)) return false;
  return record.errorKind === "not_found" || record.projection === "status";
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
  return (record.status === "failed" || record.status === "blocked" || record.status === "timeout") && !isNonFatalToolRecord(record);
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
  if (records.some((record) => (record.status === "failed" || record.status === "timeout") && isFailedToolRecord(record))) return "failed";
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

function isInternalProcessActivity(item: TurnActivityItem): boolean {
  if (isRuntimeActionSummary(item) && !isVisibleRuntimeActionSummary(item)) return true;
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
  return false;
}

function isRuntimeActionSummary(item: TurnActivityItem): boolean {
  return item.kind === "processNote" && item.source === "runtime" && item.itemKind === "action_summary";
}

function isLowValueToolPrepareStatusActivity(item: TurnActivityItem): boolean {
  if (item.kind !== "processNote" && item.kind !== "progress") return false;
  return item.blocks.some((block) => {
    if (block.type !== "process" && block.type !== "progress") return false;
    const id = String(block.id ?? "");
    const stepId = "stepId" in block ? String(block.stepId ?? "") : "";
    const panelHint = "panelHint" in block ? String(block.panelHint ?? "").toLowerCase() : "";
    const status = "status" in block ? String(block.status ?? "") : "";
    const title = "title" in block ? String(block.title ?? "") : "";
    const label = "label" in block ? String(block.label ?? "") : "";
    const text = `${title} ${label} ${"content" in block ? block.content : block.message ?? ""}`;
    const isToolPrepare = id.includes(":tool-prepare:") || stepId.includes("tool-prepare");
    if (!isToolPrepare) return false;
    if (panelHint === "terminal" && /准备命令|命令已开始/i.test(text)) {
      return status === "completed";
    }
    return (
      panelHint === "diff" &&
      /生成文件内容|生成修改内容|准备写入|准备修改/i.test(text)
    );
  });
}

function isVisibleRuntimeActionSummary(item: TurnActivityItem): boolean {
  if (!isRuntimeActionSummary(item)) return false;
  return item.blocks.some((block) => {
    if (block.type !== "process") return false;
    const scope = String(block.displayScope ?? "").toLowerCase();
    return (
      block.itemKind === "action_summary" &&
      block.source === "runtime" &&
      Boolean(block.content?.trim()) &&
      block.visibility !== "debug" &&
      scope !== "inspector" &&
      scope !== "silent"
    );
  });
}

function isVisibleAgentMessageActivity(
  item: TurnActivityItem,
  _index: number,
  _items: TurnActivityItem[],
): boolean {
  return item.kind === "agentMessage" &&
    Boolean(item.content?.trim()) &&
    !isGenericProcessPlaceholder(item.content);
}

function isVisibleProcessNoteActivity(item: TurnActivityItem, _items: TurnActivityItem[]): boolean {
  if (item.kind !== "processNote" || item.itemKind !== "process_text") return false;
  const source = thinkingSourceForItem(item);
  if (source !== "model_preamble" && source !== "post_tool") return false;
  const content = visibleProcessContent(item).trim();
  return Boolean(content) &&
    !isGenericProcessPlaceholder(content);
}

function isVisibleThinkingActivity(item: TurnActivityItem): boolean {
  if ((item.kind !== "providerReasoning" && item.kind !== "reasoning") || !item.content?.trim()) return false;
  if (isGenericProcessPlaceholder(item.content)) return false;

  const thinkingBlocks = item.blocks.filter((block) => block.type === "thinking");
  if (thinkingBlocks.length === 0) return true;
  if (thinkingBlocks.some((block) => block.is_raw_provider_reasoning || block.visibility === "debug")) {
    return false;
  }

  return (
    thinkingBlocks.some((block) =>
      Boolean(block.content?.trim()) &&
      !isGenericProcessPlaceholder(block.content),
    )
  );
}

function thinkingSourceForItem(item: TurnActivityItem): ThinkingCellState["source"] {
  if (item.kind === "processNote" && item.source === "runtime") return "runtime";
  if (item.kind === "processNote" && item.source === "post_tool") return "post_tool";
  if (item.kind === "processNote") return "model_preamble";
  if (item.kind === "agentMessage" && item.source === "post_tool") return "post_tool";
  if (item.kind === "agentMessage") return "model_preamble";
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

function isGenericStatusProgress(item: TurnActivityItem): boolean {
  if (item.kind !== "progress") return false;
  const progress = item.progress ?? [];
  if (progress.length === 0) return false;
  return progress.every((entry) => {
    if (entry.stage !== "status") return false;
    const visibleParts = [
      entry.label,
      entry.message,
      entry.summary,
      entry.detail,
    ].map((part) => part?.trim()).filter(Boolean) as string[];
    return visibleParts.length > 0 && visibleParts.every(isGenericProcessPlaceholder);
  });
}

function isSchemaOrRepeatGuardRecord(record: ToolCallRecord): boolean {
  const raw = `${record.summary ?? ""} ${record.displaySummary ?? ""}`;
  if (["silent", "status", "warning"].includes(String(record.projection || ""))) return true;
  if (["missing_generated_content", "routing_error", "stale_evidence", "repeat_guard", "tool_disabled"].includes(String(record.errorKind || ""))) return true;
  return isInternalCoordinatorNotice(raw) ||
    /Invalid tool call|missing required argument|Skipped repeated failed tool call|repeated failed tool call/i.test(raw);
}

function isWebGuardGuidanceRecord(record: ToolCallRecord): boolean {
  const raw = `${record.summary ?? ""} ${record.displaySummary ?? ""}`;
  if (isInternalCoordinatorNotice(raw)) return true;
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
  // Only merge ADJACENT todo_write activity items. The earlier version merged
  // every later todo item into the first occurrence, which pulled non-adjacent
  // todo updates ahead of the tool calls that happened between them and broke
  // the timeline order (e.g. [todo, read, todo] became [todo(merged), read]).
  const result: TurnActivityItem[] = [];

  for (const item of items) {
    const previous = result.at(-1);
    if (previous && isTodoActivityItem(previous) && isTodoActivityItem(item)) {
      mergeActivityItemRecords(previous, item);
      continue;
    }
    result.push(item);
  }

  return result;
}

function canMergeContextActivityItems(existing: TurnActivityItem, item: TurnActivityItem): boolean {
  if (existing.kind !== item.kind) return false;
  return item.kind === "fileRead" || item.kind === "workspaceSearch";
}

function aggregateContextActivityItems(items: TurnActivityItem[]): TurnActivityItem[] {
  const result: TurnActivityItem[] = [];

  for (const item of items) {
    const previous = result.at(-1);
    if (
      previous &&
      item.records?.length &&
      canMergeContextActivityItems(previous, item)
    ) {
      mergeActivityItemRecords(previous, item);
      continue;
    }
    result.push(item);
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
    if (!developerMode && withoutGuard.canonical?.visibility === "developer") return [];
    if (!developerMode && shouldHideRecoverableLocalProbeActivity(withoutGuard, options)) return [];
    if (!developerMode && isLowValueToolPrepareStatusActivity(withoutGuard)) return [];
    const isFinalAnswerSealingProgress = isFinalAnswerSealingIterationProgress(withoutGuard, {
      hasFinalAnswer: options.hasFinalAnswer,
      isStreaming: options.isStreaming,
    });
    const visibleThinkingActivity = isVisibleThinkingActivity(withoutGuard);
    const visibleAgentMessage = isVisibleAgentMessageActivity(withoutGuard, index, items);
    const hiddenGenericIteration = !developerMode &&
      isGenericIterationProgress(withoutGuard) &&
      !isFinalAnswerSealingProgress;
    const hiddenGenericStatusProgress = !developerMode && isGenericStatusProgress(withoutGuard);
    const hiddenFinalAnswerSealingProgress = !developerMode && isFinalAnswerSealingProgress;
    return !hiddenGenericIteration &&
      !hiddenGenericStatusProgress &&
      !hiddenFinalAnswerSealingProgress &&
      (!isInternalProcessActivity(withoutGuard) || visibleThinkingActivity || visibleAgentMessage) &&
      (developerMode || !isHiddenSchemaFailureActivity(withoutGuard))
      ? [withoutGuard]
      : [];
  });
  const todoAggregated = aggregateTodoActivityItems(visibleItems);
  const contextAggregated = aggregateContextActivityItems(todoAggregated);
  const webAggregated = aggregateWebActivityItems(contextAggregated);
  return webAggregated;
}

function isFinalAnswerSealingIterationProgress(
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

const CREATED_PATCH_RE = /^(?:new file mode\b|---\s+\/dev\/null$)/m;
const DELETED_PATCH_RE = /^(?:deleted file mode\b|\+\+\+\s+\/dev\/null$)/m;

function inferDiffChangeType(
  record: ToolCallRecord,
  stats: { plus: number; minus: number },
): NonNullable<DiffFileChange["changeType"]> {
  const patch = record.diff?.patch ?? "";
  if (DELETED_PATCH_RE.test(patch)) {
    return "deleted";
  }
  if (
    CREATED_PATCH_RE.test(patch)
  ) {
    return "created";
  }
  return "updated";
}

// ── Cell Extractors ─────────────────────────────────────────────────

function nonEmptyOutputLines(text: string): string[] {
  return text
    .split("\n")
    .filter((line) => line.trim());
}

function previewOutputLines(text: string, isRunning: boolean): string[] {
  const lines = nonEmptyOutputLines(text);
  return isRunning ? lines.slice(-12) : lines.slice(0, 8);
}

function commandResultOutputText(text: string | undefined): string {
  const raw = String(text || "").trimEnd();
  if (!raw) return "";
  const withoutStatus = raw.replace(/^Exit code:\s*-?\d+(?:\s+\(failed\))?\s*(?:\r?\n){0,2}/i, "");
  return withoutStatus.trimEnd();
}

function extractCommandExitCode(record: ToolCallRecord): number | undefined {
  const text = [record.summary, record.outputPreview, record.stdoutPreview, record.stderrPreview]
    .filter((value): value is string => typeof value === "string" && value.length > 0)
    .join("\n");
  const match = /\bExit code:\s*(-?\d+)\b/i.exec(text);
  if (!match) return record.status === "failed" || record.status === "blocked" || record.status === "timeout" ? 1 : 0;
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
      const legacyOutputText = commandResultOutputText(record.outputPreview || record.summary);
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
            : record.status === "failed" || record.status === "blocked" || record.status === "timeout"
              ? "failed"
              : "success",
        exitCode: extractCommandExitCode(record),
        stdoutPreview: previewOutputLines(stdoutText, isRunning),
        stderrPreview: previewOutputLines(stderrText, isRunning),
        stdoutFull: stdoutText || undefined,
        stderrFull: stderrText || undefined,
        durationMs: record.durationMs,
        collapsed: !(isRunning || record.status === "failed" || record.status === "timeout"),
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
    isFileChangeToolRecord(record) &&
    (Boolean(record.diff) || (
      (record.status === "success" || record.status === "partial") &&
      Boolean(extractToolTarget(record))
    ))
  ));
  if (allRecords.length === 0) return null;
  const recordsByPath = new Map<string, ToolCallRecord>();
  for (const record of allRecords) {
    const path = extractToolTarget(record) || `${record.name}:${record.id}`;
    recordsByPath.set(path, record);
  }

  const files: DiffFileChange[] = [];
  for (const [path, record] of recordsByPath) {
    const stats = record.diff ? getToolDiffStats(record.diff) : { plus: 0, minus: 0 };
    files.push({
      path,
      patch: record.diff?.patch,
      additions: stats.plus,
      deletions: stats.minus,
      changeType: inferDiffChangeType(record, stats),
      isLarge: stats.plus + stats.minus > 200,
    });
  }
  const totals = files.reduce(
    (acc, file) => ({
      added: acc.added + file.additions,
      deleted: acc.deleted + file.deletions,
    }),
    { added: 0, deleted: 0 },
  );

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
    collapsed: true,
    createdAt: fileChangeItems[0]?.startedAt ?? Date.now(),
  };
}

function buildThinkingCell(
  item: TurnActivityItem,
  assistantMsg: ChatMessage,
): ThinkingCellState {
  const thinkingMetadata = firstThinkingMetadata(item);
  return {
    kind: "thinking",
    id: `${assistantMsg.id}-thinking-${item.id}`,
    content: visibleProcessContent(item),
    source: thinkingSourceForItem(item),
    isRawProviderReasoning: thinkingMetadata?.is_raw_provider_reasoning,
    providerReasoningType: thinkingMetadata?.provider_reasoning_type,
    createdAt: item.startedAt ?? assistantMsg.timestamp,
  };
}

function firstThinkingMetadata(
  item: TurnActivityItem,
): Pick<Extract<ContentBlock, { type: "thinking" }>, "is_raw_provider_reasoning" | "provider_reasoning_type"> | null {
  const block = item.blocks.find((candidate): candidate is Extract<ContentBlock, { type: "thinking" }> =>
    candidate.type === "thinking",
  );
  return block ?? null;
}

function visibleProcessContent(item: TurnActivityItem): string {
  if (item.content?.trim() && !isGenericProcessPlaceholder(item.content)) return item.content;
  return item.blocks
    .flatMap((block) => {
      if (
        (block.type === "process" || block.type === "thinking" || block.type === "text") &&
        block.content?.trim() &&
        !isGenericProcessPlaceholder(block.content)
      ) {
        return [block.content];
      }
      return [];
    })
    .join("");
}

function buildProcessNoteCell(
  item: TurnActivityItem,
  assistantMsg: ChatMessage,
): ThinkingCellState {
  const source = thinkingSourceForItem(item);
  return {
    kind: "thinking",
    id: `${assistantMsg.id}-process-${item.id}`,
    content: visibleProcessContent(item),
    source,
    phase: item.kind === "agentMessage" || source === "model_preamble" || source === "post_tool"
      ? "public_output"
      : undefined,
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
  const canonical = item.canonical ?? refreshChatActivityItem(item, assistantMsg.id).canonical!;
  const records = item.records ?? [];
  const isRunning = item.status === "running" || item.status === "pending";
  // Populate progress with real-time count for aggregatable activities.
  let progress: ActivityCellState["progress"];
  if (item.progress?.length) {
    progress = { text: item.progress.at(-1)?.summary };
  } else if (isRunning && records.length > 0) {
    const doneCount = records.filter((r) => r.status === "success").length;
    progress = { current: doneCount, total: undefined, text: t(lang, `已完成 ${doneCount} 个`, `${doneCount} done`) };
  }
  const hasItemFailure = canonical.hasFailure || canonical.status === "failed" || canonical.status === "blocked";
  const hasItemSuccess = records.some((r) => r.status === "success");
  const hasPartial = item.status === "partial" || records.some((r) => r.status === "partial");
  const isPartialFailure = hasItemFailure && hasItemSuccess;
  const visibleToolRecords = isPartialFailure ? successfulRecords(records) : item.records;
  return {
    kind: "activity",
    id: item.id,
    activityKind: item.kind,
    title: getActivityTitle(item, lang) || canonical.title,
    subtitle: getActivitySubtitle(item, lang) || canonical.summary,
    status: isPartialFailure ? "done" : mapCanonicalActivityStatus(canonical.status),
    collapsed:
      !hasPartial &&
      (!item.hasFailure || isPartialFailure) &&
      item.status !== "running" &&
      item.status !== "pending",
    toolCallRecords: visibleToolRecords,
    progress,
    skill: item.kind === "skill"
      ? {
          name: item.skillName,
          triggerMode: item.triggerMode,
          sourceLevel: item.sourceLevel,
          reason: item.reason || item.summary,
          tokenEstimate: item.tokenEstimate,
          content: item.content,
        }
      : undefined,
    startedAt: canonical.startedAt ?? assistantMsg.timestamp,
    completedAt: canonical.finishedAt,
    canonical,
  };
}

function mapCanonicalActivityStatus(status: NonNullable<TurnActivityItem["canonical"]>["status"]): ActivityCellState["status"] {
  if (status === "queued" || status === "running") return "running";
  if (status === "failed" || status === "blocked") return "failed";
  if (status === "cancelled") return "interrupted";
  return "done";
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
  let fileChangeItems: TurnActivityItem[] = [];
  let fileChangeErrorCells: ErrorCellState[] = [];
  let emittedAnyErrors = false;

  const flushFileChangeItems = () => {
    if (fileChangeItems.length === 0) return;
    const aggregatedDiffCell = buildDiffCellForFileChangeItems(fileChangeItems);
    if (aggregatedDiffCell) {
      orderedCells.push(aggregatedDiffCell);
      diffCells.push(aggregatedDiffCell);
    }
    if (fileChangeErrorCells.length > 0) {
      orderedCells.push(...fileChangeErrorCells);
      emittedAnyErrors = true;
    }
    fileChangeItems = [];
    fileChangeErrorCells = [];
  };

  for (const [index, item] of items.entries()) {
    if (isVisibleAgentMessageActivity(item, index, items) || isVisibleProcessNoteActivity(item, items)) {
      flushFileChangeItems();
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
      flushFileChangeItems();
      const cell = buildThinkingCell(item, options.assistantMsg);
      orderedCells.push(cell);
      const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
      if (errorCells.length > 0) {
        orderedCells.push(...errorCells);
        emittedAnyErrors = true;
      }
      continue;
    }

    if (item.kind === "processNote" || item.kind === "agentMessage") {
      const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
      if (errorCells.length > 0) {
        flushFileChangeItems();
        orderedCells.push(...errorCells);
        emittedAnyErrors = true;
      }
      continue;
    }

    if (item.kind === "commandExecution") {
      flushFileChangeItems();
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
        fileChangeItems.push(item);
        const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
        if (errorCells.length > 0) {
          fileChangeErrorCells.push(...errorCells);
        }
        continue;
      }
    }

    flushFileChangeItems();
    const activityCell = buildActivityCell(item, options);
    orderedCells.push(activityCell);
    activityCells.push(activityCell);
    const errorCells = buildErrorCellsForItem(item, options.assistantMsg, options.hasFinalAnswer);
    if (errorCells.length > 0) {
      orderedCells.push(...errorCells);
      emittedAnyErrors = true;
    }
  }

  flushFileChangeItems();

  if (!emittedAnyErrors && options.fallbackErrorCells?.length) {
    orderedCells.push(...options.fallbackErrorCells);
    emittedAnyErrors = true;
  }

  const failureAlreadyVisible = orderedCells.some((cell) =>
    cell.kind === "exec" && cell.status === "failed",
  );
  if (!emittedAnyErrors && options.assistantMsg.terminalStatus === "failed" && !failureAlreadyVisible) {
    orderedCells.push({
      kind: "error",
      id: `err-${options.assistantMsg.id}`,
      title: "处理失败",
      message: options.assistantMsg.failureMessage || options.assistantMsg.content || "Agent 处理时发生错误",
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
  const failureAlreadyVisible = items.some((item) =>
    item.kind === "commandExecution" &&
    (item.status === "failed" || item.status === "blocked" || item.status === "timeout" || item.hasFailure) &&
    (item.records ?? []).some((record) => isFailedToolRecord(record)),
  );
  if (cells.length === 0 && assistantMsg.terminalStatus === "failed" && !failureAlreadyVisible) {
    cells.push({
      kind: "error",
      id: `err-${assistantMsg.id}`,
      title: "处理失败",
      message: assistantMsg.failureMessage || assistantMsg.content || "Agent 处理时发生错误",
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
    const coordinatorNotice = coordinatorNoticeKind(`${record.summary ?? ""} ${record.displaySummary ?? ""}`);
    if (!developerMode && coordinatorNotice) {
      return userFacingCoordinatorNotice(coordinatorNotice);
    }
    if (!developerMode && record.userSummary && !isGenericToolFailureCopy(record.userSummary)) {
      return record.userSummary.slice(0, 300);
    }
    if (!developerMode && record.errorInfo?.user_message && !isGenericToolFailureCopy(record.errorInfo.user_message)) {
      return record.errorInfo.user_message.slice(0, 300);
    }
    if (developerMode) return raw.slice(0, 300);
    if (record.errorKind === "missing_generated_content") {
      return "写入文件失败：模型没有提供文件路径或完整内容，已停止重复调用。";
    }
    if (record.errorKind === "validation_error") return "工具调用缺少必要参数。";
    if (record.errorKind === "repeat_guard") {
      return "工具调用已停止：模型连续尝试了相同的无效调用。";
    }
    if (!developerMode && isPermissionBlockedRecord(record)) {
      return isFileReadToolRecord(record)
        ? "读取文件被阻止：目标路径不在允许范围。"
        : "工具调用被权限策略阻止。";
    }
    return normalizeAgentErrorMessage(raw).slice(0, 300);
  };
  const userFacingToolTitle = (record: ToolCallRecord): string => {
    if (!developerMode && coordinatorNoticeKind(`${record.summary ?? ""} ${record.displaySummary ?? ""}`)) {
      return "分工已收敛";
    }
    if (!developerMode && isPermissionBlockedRecord(record)) {
      return isFileReadToolRecord(record) ? "读取文件被权限策略阻止" : "工具调用被权限策略阻止";
    }
    return "工具调用未完成";
  };
  const isUserFacingToolFailure = (record: ToolCallRecord): boolean =>
    Boolean(
      (record.userSummary && !isGenericToolFailureCopy(record.userSummary)) ||
      (record.errorInfo?.user_message && !isGenericToolFailureCopy(record.errorInfo.user_message)),
    ) ||
    isPermissionBlockedRecord(record) ||
    isMissingRequiredArgumentRecord(record);
  const isProxyOrNetworkFailure = (record: ToolCallRecord): boolean => {
    return record.providerErrorType === "network";
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
  if (!developerMode && item.kind === "commandExecution" && !hasNetworkFailure) {
    return cells;
  }
  if (!item.hasFailure && item.status !== "failed" && item.status !== "blocked" && item.status !== "timeout" && !hasNetworkFailure)
    return cells;
  for (const record of item.records ?? []) {
    const rawFailure = record.status === "failed" || record.status === "blocked" || record.status === "timeout";
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
  assistantMsg: ChatMessage,
  includeGlobalPlan: boolean,
): Exclude<HistoryCellState, UserMessageCellState | StreamingAssistantTailCellState>[] {
  const globalPlan = useAppStore.getState().plan;
  if (!includeGlobalPlan) return [];
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
  lang: Lang = "zh",
): TurnSummaryCellState | null {
  const items: TurnSummaryCellState["items"] = [];
  const categories = new Set<string>();
  const runningExec = cells.execCells.find((cell) => cell.status === "running");
  if (runningExec) {
    return null;
  } else if (cells.execCells.length > 0) {
    categories.add("command");
    const failed = cells.execCells.filter((cell) => cell.status === "failed").length;
    const n = cells.execCells.length;
    const commandCount = t(lang, `${n} 条命令`, `${n} command${n === 1 ? "" : "s"}`);
    items.push({
      kind: "command",
      label: commandCount,
      tone: failed ? "danger" : "success",
    });
  }

  const diff = cells.diffCells[0];
  if (diff) {
    categories.add("diff");
    const n = diff.summary.modifiedFiles;
    items.push({
      kind: "diff",
      label: t(lang, `${n} 个文件`, `${n} file${n === 1 ? "" : "s"}`),
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
      label: t(lang, `${sourceCount} 个来源`, `${sourceCount} source${sourceCount === 1 ? "" : "s"}`),
      tone: "neutral",
    });
  }

  if (cells.errorCells.length > 0) {
    categories.add("error");
    const n = cells.errorCells.length;
    items.push({
      kind: "error",
      label: t(lang, `${n} 个问题`, `${n} issue${n === 1 ? "" : "s"}`),
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

function latestLiveTextBlock(blocks: ContentBlock[]): Extract<ContentBlock, { type: "text" }> | null {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.type !== "text") continue;
    if (!isLiveAnswerTextBlock(block)) continue;
    const activityAfterBlocks = blocks.slice(index + 1).filter((candidate) =>
      candidate.type === "tool_call" ||
      candidate.type === "progress" ||
      candidate.type === "thinking" ||
      candidate.type === "process"
    );
    const hasActivityAfter = activityAfterBlocks.length > 0;
    const content = block.content.trim();
    const phase = String(block.phase ?? "").toLowerCase();
    const onlyGenericIterationAfter = activityAfterBlocks.length > 0 && activityAfterBlocks.every((candidate) => {
      if (candidate.type !== "progress") return false;
      const text = [candidate.phase, candidate.label, candidate.message, candidate.summary].filter(Boolean).join(" ");
      return String(candidate.phase ?? "") === "iteration" || /Agent working|Iteration\s+\d+\s*\/\s*\d+|agent:iter/i.test(text);
    });
    // A draft text block that has real tool activity after it is preamble
    // (e.g. "我先检查一下" before a tool call), not the live answer — don't keep
    // it in the result area just because the provider tagged it final_answer,
    // or it will jump result→process when the tool event lands. Only "stream +
    // generic iteration progress after" is still treated as the live answer.
    if (hasActivityAfter && phase !== "final" && !(block.source === "stream" && onlyGenericIterationAfter)) continue;
    if (
      !content ||
      isGenericProcessPlaceholder(content) ||
      isInternalCollaborationNarration(content) ||
      isBareMarkdownMarker(content)
    ) continue;
    return block;
  }
  return null;
}

function isLiveAnswerTextBlock(block: Extract<ContentBlock, { type: "text" }>): boolean {
    if (block.sealed === true) return false;
    if (block.visibility !== "unsealed" && block.visibility !== "draft") return false;
    if (String(block.phase ?? "").toLowerCase() === "commentary") return false;
  if (block.role === "runtime") return false;
  return block.source !== "model_preamble" && block.source !== "post_tool" && block.source !== "runtime";
}

function isBareMarkdownMarker(content: string): boolean {
  return /^[\s•·*\-–—]+$/.test(content.trim());
}

// ── Turn Builder ────────────────────────────────────────────────────

function buildTurn(
  userMsg: ChatMessage | null,
  assistantMsg: ChatMessage | null,
  options: { dismissTransientRunErrors?: boolean; includeGlobalPlan?: boolean } = {},
): ChatTurnState {
  const userCell: UserMessageCellState | null = userMsg
    ? {
        kind: "user_message",
        id: userMsg.id,
        content: userMsg.content,
        attachments: userMsg.attachmentRefs?.map((a) => ({
          id: a.id,
          artifactId: a.artifactId,
          docId: a.docId,
          name: a.name,
          type: a.mediaType,
          size: a.sizeBytes,
          dataUrl: a.dataUrl,
        })),
        createdAt: userMsg.timestamp,
        queueState: userMsg.queueState,
        queuePosition: userMsg.queuePosition,
        queueMessageId: userMsg.queueMessageId,
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
  const projection = projectChatTurn(blocks, {
    isStreaming: assistantMsg.isStreaming,
    isThinkingStreaming: assistantMsg.isThinkingStreaming,
    terminalStatus:
      assistantMsg.terminalStatus === "failed" ? "failed" : undefined,
    artifactCount: assistantMsg.artifacts?.length,
  }, assistantMsg.id);
  const finalTextBlock = blocks.find(
    (b): b is Extract<ContentBlock, { type: "text" }> =>
      b.type === "text" && b.visibility === "final",
  );
  const hasCommittedFinalAnswerBlock = Boolean(finalTextBlock);
  const providerCitations = finalTextBlock?.providerRaw?.citations;
  const effectiveCitations = mergeCitationsWithWebSearchFallback(
    assistantMsg.citations,
    blocks,
    projection.finalAnswer,
    providerCitations,
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
  }).map((item) => refreshChatActivityItem(item, assistantMsg.id));
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

  const planCells = extractPlanCell(assistantMsg, Boolean(options.includeGlobalPlan));
  const summaryCell = buildTurnSummaryCell(assistantMsg, {
    activityCells,
    execCells,
    diffCells,
    errorCells,
  }, lang);

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
        isStreaming: Boolean(assistantMsg.isStreaming && !hasCommittedFinalAnswerBlock),
        source:
          projection.finalAnswerSource === "reply" ||
          projection.finalAnswerSource === "fallback" ||
          projection.finalAnswerSource === "partial"
            ? projection.finalAnswerSource
            : "stream",
        attachments: assistantMsg.replyAttachments?.length
          ? assistantMsg.replyAttachments.map((a) => ({
              path: a.path,
              size: a.size,
              isImage: a.isImage,
            }))
          : undefined,
      }
    : null;

  // Determine active cell during streaming
  let activeCell: HistoryCellState | null = null;
  const committedCells: CommittedCellState[] = [];
  const liveTextBlock = assistantMsg.isStreaming ? latestLiveTextBlock(blocks) : null;
  // Draft text is answer-only. It never creates process cells, and it is hidden
  // once real activity appears after it in the turn.
  if (
    liveTextBlock &&
    !hasFinalAnswer &&
    (liveTextBlock.visibility === "draft" || liveTextBlock.visibility === "unsealed")
  ) {
    activeCell = {
      kind: "streaming_assistant_tail",
      id: liveTextBlock.segmentId || `${assistantMsg.id}-live-answer`,
      partialMarkdown: liveTextBlock.content,
      updatedAt: Date.now(),
    };
  }

  if (assistantMsg.isStreaming) {
    if (summaryCell) committedCells.push(summaryCell);
    committedCells.push(...processCells);
    committedCells.push(...planCells);
  } else {
    // Not streaming — all cells are committed
    if (summaryCell) committedCells.push(summaryCell);
    committedCells.push(...processCells);
    committedCells.push(...planCells);
  }

  const dismissTransientRunError = Boolean(
    options.dismissTransientRunErrors &&
    shouldDismissAssistantRunError(assistantMsg, finalAnswerCell),
  );

  return {
    id: assistantMsg.id,
    userCell,
    committedCells: dismissTransientRunError ? [] : committedCells,
    activeCell: dismissTransientRunError ? null : activeCell,
    finalAnswerCell: dismissTransientRunError ? null : finalAnswerCell,
    status: dismissTransientRunError
      ? "completed"
      : assistantMsg.isStreaming
      ? "streaming"
      : assistantMsg.terminalStatus === "failed"
        ? "failed"
        : "completed",
    startedAt: userMsg?.timestamp ?? assistantMsg.timestamp,
    completedAt: assistantMsg.isStreaming
      ? undefined
      : assistantMsg.completedAt ?? assistantMsg.timestamp,
  };
}

// ── Public API ──────────────────────────────────────────────────────

/**
 * Turn projection cache. A turn is a pure function of (userMsg, assistantMsg,
 * isStreaming, viewMode, plan). The store replaces a message object only when
 * that message changes, so completed history messages keep a stable reference
 * and reuse their cached projection verbatim — only the streaming turn rebuilds
 * per event. Keeps MessageList's per-event work O(active turn), not O(history).
 */
interface CachedTurnEntry {
  assistantMsg: ChatMessage;
  userMsg: ChatMessage | null;
  isStreaming: boolean;
  viewMode: string;
  plan: unknown;
  includeGlobalPlan: boolean;
  dismissTransientRunErrors: boolean;
  turn: ChatTurnState;
}
const turnProjectionCache = new Map<string, CachedTurnEntry>();

function projectAssistantTurnCached(
  assistantMsg: ChatMessage,
  userMsg: ChatMessage | null,
  isStreaming: boolean,
  viewMode: string,
  plan: unknown,
  includeGlobalPlan: boolean,
  applyStreamingOverride: boolean,
  dismissTransientRunErrors = false,
): ChatTurnState {
  const cached = turnProjectionCache.get(assistantMsg.id);
  if (
    cached &&
    cached.assistantMsg === assistantMsg &&
    cached.userMsg === userMsg &&
    cached.isStreaming === isStreaming &&
    cached.viewMode === viewMode &&
    cached.plan === plan &&
    cached.includeGlobalPlan === includeGlobalPlan &&
    cached.dismissTransientRunErrors === dismissTransientRunErrors
  ) {
    return cached.turn;
  }
  const turn = buildTurn(userMsg, assistantMsg, { dismissTransientRunErrors, includeGlobalPlan });
  // Override streaming status with global flag (only for user-led turns, to
  // preserve the original per-branch behaviour).
  if (applyStreamingOverride && turn.status === "streaming" && !isStreaming) {
    turn.status = "completed";
  }
  // Only cache complete turns. Streaming turns are constantly changing
  // (incoming text chunks, tool calls) and must be re-projected every time.
  if (!assistantMsg.isStreaming) {
    turnProjectionCache.set(assistantMsg.id, { assistantMsg, userMsg, isStreaming, viewMode, plan, includeGlobalPlan, dismissTransientRunErrors, turn });
  }
  return turn;
}

const isTransientCommandBacklogSystemMessage = (msg: ChatMessage): boolean =>
  msg.role === "system" &&
  /^Error:\s*Too many pending commands; please wait for current work to finish\.?$/i.test(msg.content.trim());

const isQuietSystemNoticeMessage = (msg: ChatMessage): boolean =>
  msg.role === "system" &&
  (
    msg.id === "system-guidelines-updated" ||
    /^Project guidelines have been updated\.?$/i.test(msg.content.trim())
  );

function runErrorText(text: string | undefined): string {
  return String(text || "").replace(/^Error:\s*/i, "").replace(/\s+/g, " ").trim();
}

function isTransientRunErrorText(text: string | undefined): boolean {
  const normalized = runErrorText(text);
  return (
    /the user interrupted the current run/i.test(normalized) ||
    /tool calls failed before the assistant produced a reply/i.test(normalized) ||
    /tool calls failed and the model did not produce a final reply/i.test(normalized)
  );
}

function hasLaterUserMessage(messages: ChatMessage[], index: number): boolean {
  return messages.slice(index + 1).some((message) => message.role === "user");
}

function shouldDismissSystemRunError(msg: ChatMessage, hasLaterUser: boolean): boolean {
  return hasLaterUser && msg.role === "system" && isTransientRunErrorText(msg.content);
}

function shouldDismissAssistantRunError(
  assistantMsg: ChatMessage,
  finalAnswerCell: AssistantMarkdownCellState | null,
): boolean {
  if (finalAnswerCell?.markdownSource.trim()) return false;
  if (assistantMsg.terminalStatus === "interrupted") return true;
  if (assistantMsg.terminalStatus !== "failed") return false;
  return isTransientRunErrorText([assistantMsg.failureMessage, assistantMsg.content].filter(Boolean).join(" "));
}

export function projectMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
): ChatTurnState[] {
  const { viewMode, plan } = useAppStore.getState();
  const latestAssistantId = [...messages].reverse().find((message) => message.role === "assistant")?.id;
  const turns: ChatTurnState[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];
    if (isQuietSystemNoticeMessage(msg)) {
      i += 1;
      continue;
    }
    if (isTransientCommandBacklogSystemMessage(msg)) {
      i += 1;
      continue;
    }
    const hasLaterUser = hasLaterUserMessage(messages, i);
    if (shouldDismissSystemRunError(msg, hasLaterUser)) {
      i += 1;
      continue;
    }

    if (msg.role === "user") {
      const userMsg = msg;
      const assistantMsg =
        i + 1 < messages.length && messages[i + 1]?.role === "assistant"
          ? messages[i + 1]
          : null;
      const turn = assistantMsg
        ? projectAssistantTurnCached(
            assistantMsg,
            userMsg,
            isStreaming,
            viewMode,
            plan,
            assistantMsg.id === latestAssistantId,
            true,
            hasLaterUserMessage(messages, i + 1),
          )
        : buildTurn(userMsg, null);
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
      turns.push(projectAssistantTurnCached(
        msg,
        null,
        isStreaming,
        viewMode,
        plan,
        msg.id === latestAssistantId,
        false,
        hasLaterUser,
      ));
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
    if (isQuietSystemNoticeMessage(msg)) {
      i += 1;
      continue;
    }
    if (isTransientCommandBacklogSystemMessage(msg)) {
      i += 1;
      continue;
    }
    if (shouldDismissSystemRunError(msg, hasLaterUserMessage(messages, i))) {
      i += 1;
      continue;
    }
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
