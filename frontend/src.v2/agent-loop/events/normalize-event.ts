import type {
  ActivityDetail,
  ActivityGroupItem,
  AgentTimelineItem,
  AgentTimelineStatus,
  AgentTurnState,
  AgentTurnSummary,
  BrowserPreviewItem,
  FileChangesItem,
  ProcessItem,
  SystemStatusItem,
} from "../types";
import { eventSeq, sortTimelineItems, upsertTimelineItem } from "./event-ordering";
import type { RawAgentLoopEvent, RawFileChange } from "./raw-events";

export interface AgentLoopEventState {
  turnId?: string;
  loopId?: string;
  timeline: AgentTimelineItem[];
  finalAnswer?: AgentTurnState["finalAnswer"];
  summary: AgentTurnSummary;
}

export function createAgentLoopEventState(
  initial?: Partial<AgentLoopEventState>,
): AgentLoopEventState {
  const timeline = sortTimelineItems(initial?.timeline ?? []);
  return {
    turnId: initial?.turnId,
    loopId: initial?.loopId,
    timeline,
    finalAnswer: initial?.finalAnswer,
    summary: initial?.summary ?? buildSummary(timeline),
  };
}

export function reduceAgentLoopEvents(
  events: readonly RawAgentLoopEvent[],
  initial?: Partial<AgentLoopEventState>,
): AgentLoopEventState {
  return events.reduce(
    (state, event, index) => mergeAgentLoopEvent(state, event, index),
    createAgentLoopEventState(initial),
  );
}

export function mergeAgentLoopEvent(
  state: AgentLoopEventState,
  rawEvent: RawAgentLoopEvent,
  fallbackIndex = 0,
): AgentLoopEventState {
  const metadata = metadataPatch(state, rawEvent);
  if (isFinalAnswerEvent(rawEvent.type) || rawEvent.type === "done") {
    return mergeFinalAnswerDelta({ ...state, ...metadata }, rawEvent);
  }

  const item = normalizeEvent(rawEvent, fallbackIndex);
  if (!item) {
    return metadata.turnId || metadata.loopId ? { ...state, ...metadata } : state;
  }

  const existing = state.timeline.find((current) => current.id === item.id);
  const nextItem = existing ? mergeTimelineItem(existing, item) : item;
  const timeline = upsertTimelineItem(state.timeline, nextItem);
  return {
    ...state,
    ...metadata,
    timeline,
    summary: buildSummary(timeline),
  };
}

export function normalizeEvent(
  rawEvent: RawAgentLoopEvent,
  fallbackIndex = 0,
): AgentTimelineItem | null {
  if (isDebugEvent(rawEvent) || isFinalAnswerEvent(rawEvent.type)) return null;

  const seq = eventSeq(rawEvent, fallbackIndex);
  switch (rawEvent.type) {
    case "agent.item":
    case "agent_item":
    case "agent.item.created":
    case "agent.item.updated":
    case "agent.item.completed":
      return normalizeAgentItem(rawEvent, seq);
    case "agent.progress":
    case "agent.system_status":
    case "context_compacted":
    case "error":
      return handleSystemStatus(rawEvent, seq);
    case "agent.tool_group.started":
    case "agent.tool_group.updated":
    case "agent.tool_group.completed":
      return activityGroupFromRaw(rawEvent, seq);
    case "tool_call":
    case "tool_result":
      return activityGroupFromToolEvent(rawEvent, seq);
    case "agent.file_changes.updated":
      return fileChangesFromRaw(rawEvent, seq);
    default:
      if (looksLikeFileChanges(rawEvent)) return fileChangesFromRaw(rawEvent, seq);
      if (looksLikeBrowserPreview(rawEvent)) return browserPreviewFromRaw(rawEvent, seq);
      if (looksLikeToolActivity(rawEvent)) return activityGroupFromRaw(rawEvent, seq);
      return null;
  }
}

export const normalizeAgentLoopEvent = normalizeEvent;

export function mergeFinalAnswerDelta(
  state: AgentLoopEventState,
  rawEvent: RawAgentLoopEvent,
): AgentLoopEventState {
  const type = rawEvent.type;
  const id = stringValue(rawEvent.message_id ?? rawEvent.item_id ?? rawEvent.itemId ?? rawEvent.id) ||
    state.finalAnswer?.id ||
    `final-answer:${state.turnId ?? "turn"}`;

  if (type === "final_answer_retracted" || type === "final_answer.retracted") {
    const { finalAnswer: _finalAnswer, ...rest } = state;
    return rest;
  }

  if (type === "done") {
    if (!state.finalAnswer) return state;
    return {
      ...state,
      finalAnswer: { ...state.finalAnswer, status: "completed" },
    };
  }

  if (type === "final_answer_started" || type === "final_answer.started") {
    return {
      ...state,
      finalAnswer: {
        id,
        content: state.finalAnswer?.content ?? "",
        status: "streaming",
      },
    };
  }

  if (type === "final_answer_committed" || type === "final_answer.committed") {
    return {
      ...state,
      finalAnswer: {
        id,
        content: state.finalAnswer?.content ?? stringValue(rawEvent.content ?? rawEvent.message),
        status: "completed",
      },
    };
  }

  const delta = stringValue(rawEvent.content ?? rawEvent.message);
  return {
    ...state,
    finalAnswer: {
      id,
      content: `${state.finalAnswer?.content ?? ""}${delta}`,
      status: "streaming",
    },
  };
}

export function handleSystemStatus(
  rawEvent: RawAgentLoopEvent,
  seq = eventSeq(rawEvent),
): SystemStatusItem | null {
  const content = statusContent(rawEvent);
  if (!content) return null;
  const tone =
    rawEvent.type === "error" || isFailureStatus(rawEvent.status)
      ? "error"
      : isWarningStatus(rawEvent.status) || rawEvent.stage === "approval"
        ? "warning"
        : "subtle";
  return {
    id: itemId(rawEvent, "status"),
    type: "system_status",
    seq,
    content,
    detail: stringValue(rawEvent.detail ?? rawEvent.summary),
    tone,
  };
}

export function mergeToolCallsIntoActivityGroup(
  existing: ActivityGroupItem | undefined,
  next: ActivityGroupItem,
): ActivityGroupItem {
  if (!existing) return next;
  return {
    ...existing,
    ...next,
    seq: Math.min(existing.seq, next.seq),
    status: mergeActivityStatus(existing.status, next.status),
    details: mergeActivityDetails(existing.details, next.details),
    defaultCollapsed: true,
  };
}

function normalizeAgentItem(rawEvent: RawAgentLoopEvent, seq: number): AgentTimelineItem | null {
  const kind = stringValue(rawEvent.kind || rawEvent.activity_kind || rawEvent.activityKind);
  if (kind === "tool_group") return activityGroupFromRaw(rawEvent, seq);
  if (kind === "status" || kind === "plan") return handleSystemStatus(rawEvent, seq);

  const content = stringValue(rawEvent.content ?? rawEvent.summary ?? rawEvent.message ?? rawEvent.detail ?? rawEvent.title);
  if (!content) return null;
  return {
    id: itemId(rawEvent, "process"),
    type: "process",
    kind: normalizeProcessKind(kind),
    source: normalizeProcessSource(rawEvent.source),
    loopId: loopId(rawEvent),
    seq,
    content,
    status: "completed",
  };
}

function activityGroupFromRaw(rawEvent: RawAgentLoopEvent, seq: number): ActivityGroupItem {
  const activityKind = normalizeActivityKind(rawEvent);
  const status = normalizeActivityStatus(rawEvent.status, rawEvent.type);
  const count = positiveInteger((rawEvent as { count?: unknown }).count) ?? detailCount(rawEvent) ?? 1;
  const detail = detailFromRawActivity(rawEvent, activityKind);
  return {
    id: groupId(rawEvent),
    type: "activity_group",
    activityKind,
    loopId: loopId(rawEvent),
    seq,
    title: stringValue(rawEvent.title) || activityTitle(activityKind, status, count),
    summary: stringValue(rawEvent.summary) || activitySummary(activityKind, count),
    status,
    details: detail ? [detail] : [],
    defaultCollapsed: true,
  };
}

function activityGroupFromToolEvent(rawEvent: RawAgentLoopEvent, seq: number): ActivityGroupItem | BrowserPreviewItem {
  const activityKind = normalizeActivityKind(rawEvent);
  if (activityKind === "browser" && rawEvent.url) {
    return browserPreviewFromRaw(rawEvent, seq);
  }
  const status = rawEvent.type === "tool_call"
    ? normalizeActivityStatus(rawEvent.status ?? "running", rawEvent.type)
    : normalizeActivityStatus(rawEvent.status ?? (rawEvent.exit_code || rawEvent.exitCode ? "failed" : "success"), rawEvent.type);
  const detail = detailFromToolEvent(rawEvent, activityKind);
  return {
    id: groupId(rawEvent),
    type: "activity_group",
    activityKind,
    loopId: loopId(rawEvent),
    seq,
    title: activityTitle(activityKind, status, 1),
    summary: activitySummary(activityKind, 1),
    status,
    details: detail ? [detail] : [],
    defaultCollapsed: true,
  };
}

function fileChangesFromRaw(rawEvent: RawAgentLoopEvent, seq: number): FileChangesItem {
  const files = (rawEvent.files ?? [])
    .map(normalizeFileChange)
    .filter((file): file is FileChangesItem["files"][number] => file != null);
  const added = finiteNumber(rawEvent.added ?? rawEvent.additions) ??
    files.reduce((sum, file) => sum + file.added, 0);
  const removed = finiteNumber(rawEvent.removed ?? rawEvent.deletions) ??
    files.reduce((sum, file) => sum + file.removed, 0);

  return {
    id: itemId(rawEvent, "files"),
    type: "file_changes",
    seq,
    added,
    removed,
    files,
    actions: {
      canReview: true,
      canUndo: true,
    },
  };
}

function browserPreviewFromRaw(rawEvent: RawAgentLoopEvent, seq: number): BrowserPreviewItem {
  return {
    id: itemId(rawEvent, "browser"),
    type: "browser_preview",
    seq,
    title: stringValue(rawEvent.title ?? rawEvent.summary ?? rawEvent.message) || "已打开浏览器预览",
    url: stringValue(rawEvent.url) || undefined,
    status: normalizeActivityStatus(rawEvent.status, rawEvent.type),
  };
}

function mergeTimelineItem(existing: AgentTimelineItem, next: AgentTimelineItem): AgentTimelineItem {
  if (existing.type === "activity_group" && next.type === "activity_group") {
    return mergeToolCallsIntoActivityGroup(existing, next);
  }
  if (existing.type === "file_changes" && next.type === "file_changes") {
    return {
      ...existing,
      ...next,
      seq: Math.min(existing.seq, next.seq),
      files: mergeFiles(existing.files, next.files),
      added: next.added || existing.added,
      removed: next.removed || existing.removed,
    };
  }
  return {
    ...existing,
    ...next,
    seq: Math.min(existing.seq, next.seq),
  } as AgentTimelineItem;
}

function buildSummary(timeline: readonly AgentTimelineItem[]): AgentTurnSummary {
  const summary: AgentTurnSummary = {
    commandCount: 0,
    searchCount: 0,
    readCount: 0,
    editedFileCount: 0,
    sourceCount: 0,
    testCount: 0,
  };

  for (const item of timeline) {
    if (item.type === "file_changes") {
      summary.editedFileCount += item.files.length;
      continue;
    }
    if (item.type !== "activity_group") continue;
    const count = Math.max(1, item.details.length);
    if (item.activityKind === "command") summary.commandCount += count;
    if (item.activityKind === "test") summary.testCount += 1;
    if (item.activityKind === "web_search") summary.searchCount += count;
    if (item.activityKind === "web_read" || item.activityKind === "file_read") {
      summary.readCount += count;
    }
    summary.sourceCount += item.details.filter((detail) => detail.kind === "source").length;
  }

  return summary;
}

function normalizeFileChange(file: RawFileChange): FileChangesItem["files"][number] | null {
  const path = stringValue(file.path ?? file.file_path);
  if (!path) return null;
  return {
    path,
    added: finiteNumber(file.added ?? file.additions) ?? 0,
    removed: finiteNumber(file.removed ?? file.deletions) ?? 0,
    status: normalizeFileStatus(file.status),
  };
}

function detailFromToolEvent(
  rawEvent: RawAgentLoopEvent,
  activityKind: ActivityGroupItem["activityKind"],
): ActivityDetail | null {
  const args = rawEvent.args ?? rawEvent.input ?? {};
  const toolName = toolNameFromRaw(rawEvent);
  const command = stringValue(args.command ?? args.cmd ?? rawEvent.input_summary ?? rawEvent.display_summary);
  const output = stringValue(rawEvent.output ?? rawEvent.result ?? rawEvent.display_summary ?? rawEvent.summary ?? rawEvent.content_preview);

  if (activityKind === "command" || activityKind === "test") {
    return {
      kind: "shell",
      title: activityKind === "test" ? "Test" : "Shell",
      command: command || toolName,
      output,
      exitCode: finiteNumber(rawEvent.exit_code ?? rawEvent.exitCode) ?? undefined,
    };
  }

  const url = stringValue(args.url ?? args.source_url ?? rawEvent.source_url ?? rawEvent.url) || firstHttpUrl(output);
  if (url) {
    return {
      kind: "source",
      title: activityKind === "web_search" ? "搜索结果" : "来源",
      url,
      excerpt: output || undefined,
    };
  }

  const query = stringValue(args.query ?? args.q ?? args.pattern ?? args.glob);
  if (query) {
    return {
      kind: "source",
      title: activityKind === "file_read" ? "搜索工作区" : "搜索",
      query,
      excerpt: query,
    };
  }

  const path = stringValue(args.path ?? args.file_path ?? args.target ?? args.filename);
  if (path) {
    return {
      kind: "source",
      title: /write|edit/i.test(toolName) ? "编辑文件" : "读取文件",
      path,
      excerpt: path,
    };
  }

  const content = output || stringValue(rawEvent.content ?? rawEvent.message ?? toolName);
  return content ? { kind: "text", title: readableToolName(toolName), content } : null;
}

function detailFromRawActivity(
  rawEvent: RawAgentLoopEvent,
  activityKind: ActivityGroupItem["activityKind"],
): ActivityDetail | null {
  const toolDetail = detailFromToolEvent(rawEvent, activityKind);
  if (toolDetail) return toolDetail;
  const content = stringValue(rawEvent.detail ?? rawEvent.content ?? rawEvent.message);
  return content ? { kind: "text", title: stringValue(rawEvent.title) || "Activity", content } : null;
}

function normalizeActivityKind(rawEvent: RawAgentLoopEvent): ActivityGroupItem["activityKind"] {
  const explicit = stringValue(rawEvent.activity_kind ?? rawEvent.activityKind ?? rawEvent.result_kind ?? rawEvent.resultKind).toLowerCase();
  const toolName = toolNameFromRaw(rawEvent).toLowerCase();
  const args = rawEvent.args ?? rawEvent.input ?? {};
  const command = stringValue(args.command ?? args.cmd);
  const haystack = `${explicit} ${toolName} ${command}`.toLowerCase();

  if (/\b(browser|playwright|preview)\b/.test(haystack)) return "browser";
  if (/\b(mcp|mcp__)/.test(haystack)) return "mcp";
  if (/\b(test|pytest|vitest|jest|tsc|typecheck)\b/.test(haystack)) return "test";
  if (/\b(command|shell|terminal|powershell|bash|run_command|cmd)\b/.test(haystack)) return "command";
  if (/\b(web_read|fetch|read_url|open_url|browser_fetch)\b/.test(haystack)) return "web_read";
  if (/\b(web|search|web_search)\b/.test(haystack)) return "web_search";
  if (/\b(file|read_file|workspace|grep|glob|list_files)\b/.test(haystack)) return "file_read";
  if (stringValue(args.url ?? rawEvent.url ?? rawEvent.source_url)) return "web_read";
  if (stringValue(args.path ?? args.file_path ?? args.pattern ?? args.glob)) return "file_read";
  return "unknown";
}

function normalizeActivityStatus(
  status?: RawAgentLoopEvent["status"],
  eventType?: string,
): ActivityGroupItem["status"] {
  const value = stringValue(status).toLowerCase();
  if (eventType?.endsWith(".started")) return "running";
  if (eventType?.endsWith(".completed")) return "completed";
  if (value === "running" || value === "pending") return "running";
  if (value === "failed" || value === "error" || value === "interrupted") return "failed";
  return "completed";
}

function normalizeProcessKind(kind: string): ProcessItem["kind"] {
  if (kind === "action_summary") return "action_summary";
  if (kind === "observation") return "observation";
  return "process_text";
}

function normalizeProcessSource(source: unknown): ProcessItem["source"] {
  return source === "runtime" || source === "system" || source === "tool" ? "runtime" : "model";
}

function normalizeFileStatus(status: RawFileChange["status"]): FileChangesItem["files"][number]["status"] {
  const value = stringValue(status).toLowerCase();
  if (value === "created" || value === "added") return "created";
  if (value === "deleted" || value === "removed") return "deleted";
  return "modified";
}

function activityTitle(
  kind: ActivityGroupItem["activityKind"],
  status: ActivityGroupItem["status"],
  count: number,
): string {
  if (kind === "command") return status === "running" ? "正在运行命令" : `已运行 ${count} 条命令`;
  if (kind === "test") return status === "running" ? "正在运行测试" : "已运行测试";
  if (kind === "web_search") return status === "running" ? "正在搜索实时信息" : `已搜索 ${count} 次`;
  if (kind === "web_read") return status === "running" ? "正在读取网页资料" : `已读取 ${count} 个网页来源`;
  if (kind === "file_read") return status === "running" ? "正在读取相关文件" : `已读取 ${count} 个文件来源`;
  if (kind === "browser") return status === "running" ? "正在打开浏览器预览" : "已打开浏览器预览";
  if (kind === "mcp") return status === "running" ? "正在调用 MCP 工具" : `已调用 ${count} 个 MCP 工具`;
  return status === "running" ? "正在处理" : `已处理 ${count} 项`;
}

function activitySummary(kind: ActivityGroupItem["activityKind"], count: number): string {
  switch (kind) {
    case "command":
    case "test":
      return `${count} 条命令`;
    case "web_search":
      return `${count} 次搜索`;
    case "web_read":
      return `${count} 个页面`;
    case "file_read":
      return `${count} 个来源`;
    case "mcp":
      return `${count} 个工具`;
    case "browser":
      return "预览";
    default:
      return `${count} 项`;
  }
}

function statusContent(rawEvent: RawAgentLoopEvent): string {
  if (rawEvent.type === "context_compacted") {
    return stringValue(rawEvent.message ?? rawEvent.summary ?? rawEvent.content) || "上下文已自动压缩";
  }
  if (rawEvent.type === "error") {
    return stringValue(rawEvent.message ?? rawEvent.summary ?? rawEvent.content ?? rawEvent.detail) || "任务出错";
  }
  return stringValue(rawEvent.message ?? rawEvent.content ?? rawEvent.title ?? rawEvent.summary);
}

function mergeActivityStatus(
  existing: ActivityGroupItem["status"],
  next: ActivityGroupItem["status"],
): ActivityGroupItem["status"] {
  if (existing === "failed" || next === "failed") return "failed";
  return next;
}

function mergeActivityDetails(
  existing: readonly ActivityDetail[],
  next: readonly ActivityDetail[],
): ActivityDetail[] {
  const merged = [...existing];
  for (const detail of next) {
    const index = merged.findIndex((item) => activityDetailKey(item) === activityDetailKey(detail));
    if (index >= 0) {
      merged[index] = { ...merged[index], ...detail } as ActivityDetail;
    } else if (
      detail.kind === "shell" &&
      merged.length === 1 &&
      merged[0].kind === "shell" &&
      isGenericShellCommand(detail.command)
    ) {
      merged[0] = {
        ...merged[0],
        ...detail,
        command: merged[0].command,
      };
    } else {
      merged.push(detail);
    }
  }
  return merged;
}

function activityDetailKey(detail: ActivityDetail): string {
  if (detail.kind === "shell") return `shell:${detail.command}`;
  if (detail.kind === "source") return `source:${detail.url ?? detail.path ?? detail.query ?? detail.title}`;
  return `text:${detail.title}:${detail.content}`;
}

function isGenericShellCommand(command: string): boolean {
  return /^(?:tool|run_command|run command|shell|terminal|bash|powershell)$/i.test(command.trim());
}

function mergeFiles(
  existing: readonly FileChangesItem["files"][number][],
  next: readonly FileChangesItem["files"][number][],
): FileChangesItem["files"] {
  const files = new Map<string, FileChangesItem["files"][number]>();
  for (const file of existing) files.set(file.path, file);
  for (const file of next) files.set(file.path, { ...files.get(file.path), ...file });
  return [...files.values()];
}

function metadataPatch(
  state: AgentLoopEventState,
  rawEvent: RawAgentLoopEvent,
): Partial<Pick<AgentLoopEventState, "turnId" | "loopId">> {
  return {
    turnId: stringValue(rawEvent.turn_id ?? rawEvent.turnId) || state.turnId,
    loopId: stringValue(rawEvent.loop_id ?? rawEvent.loopId) || state.loopId,
  };
}

function groupId(rawEvent: RawAgentLoopEvent): string {
  return stringValue(rawEvent.group_id ?? rawEvent.groupId ?? rawEvent.item_id ?? rawEvent.itemId ?? rawEvent.id) ||
    `${rawEvent.type}:${eventSeq(rawEvent)}`;
}

function itemId(rawEvent: RawAgentLoopEvent, prefix: string): string {
  return stringValue(rawEvent.item_id ?? rawEvent.itemId ?? rawEvent.id ?? rawEvent.event_id ?? rawEvent.eventId) ||
    `${prefix}:${eventSeq(rawEvent)}`;
}

function loopId(rawEvent: RawAgentLoopEvent): string | undefined {
  return stringValue(rawEvent.loop_id ?? rawEvent.loopId) || undefined;
}

function toolNameFromRaw(rawEvent: RawAgentLoopEvent): string {
  return stringValue(rawEvent.tool_name ?? rawEvent.toolName ?? rawEvent.name ?? rawEvent.kind) || "tool";
}

function readableToolName(name: string): string {
  return name.replace(/^mcp__[^_]+__/i, "").replace(/_/g, " ");
}

function isDebugEvent(rawEvent: RawAgentLoopEvent): boolean {
  return rawEvent.visibility === "debug";
}

function isFinalAnswerEvent(type: string): boolean {
  return [
    "final_answer_started",
    "final_answer_delta",
    "final_answer_retracted",
    "final_answer_committed",
    "final_answer.started",
    "final_answer.delta",
    "final_answer.retracted",
    "final_answer.committed",
  ].includes(type);
}

function looksLikeFileChanges(rawEvent: RawAgentLoopEvent): boolean {
  return Array.isArray(rawEvent.files) && rawEvent.files.length > 0;
}

function looksLikeBrowserPreview(rawEvent: RawAgentLoopEvent): boolean {
  const text = `${rawEvent.type} ${rawEvent.kind ?? ""} ${rawEvent.title ?? ""} ${rawEvent.url ?? ""}`;
  return /browser|preview|playwright/i.test(text);
}

function looksLikeToolActivity(rawEvent: RawAgentLoopEvent): boolean {
  return Boolean(rawEvent.tool_name || rawEvent.toolName || rawEvent.name || rawEvent.args || rawEvent.input);
}

function detailCount(rawEvent: RawAgentLoopEvent): number | null {
  const files = Array.isArray(rawEvent.files) ? rawEvent.files.length : 0;
  return files > 0 ? files : null;
}

function positiveInteger(value: unknown): number | null {
  const number = finiteNumber(value);
  return number != null && number > 0 ? Math.floor(number) : null;
}

function finiteNumber(value: unknown): number | null {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function stringValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (value == null) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function firstHttpUrl(value?: string): string {
  return value?.match(/https?:\/\/[^\s)]+/i)?.[0] ?? "";
}

function isFailureStatus(status?: AgentTimelineStatus | string): boolean {
  const value = stringValue(status).toLowerCase();
  return value === "failed" || value === "error" || value === "interrupted";
}

function isWarningStatus(status?: AgentTimelineStatus | string): boolean {
  const value = stringValue(status).toLowerCase();
  return value === "warning" || value === "blocked" || value === "pending_approval";
}
