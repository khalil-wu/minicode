import type { ActivityCellState } from "./cellTypes";
import {
  getToolActivityDetail,
  registerToolActivityDetail,
  type ToolActivityDetail,
} from "../tool-calls/toolRendererRegistry";

// ── Activity detail types ──────────────────────────────────

export interface ActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  lineInfo?: string;
  count: number;
  durationMs: number | null;
}

type ActivityToolRecord = NonNullable<ActivityCellState["toolCallRecords"]>[number];

// ── Text formatting ────────────────────────────────────────

export function shortTarget(value: string): string {
  const text = String(value).replace(/\\/g, "/").trim();
  if (!text) return "";
  const fileName = text.split("/").pop() ?? text;
  return fileName.length > 50 ? `${fileName.slice(0, 47)}...` : fileName;
}

export function shortCommand(cmd: string): string {
  const text = cmd.trim();
  if (!text) return "";
  if (text.length > 60) return `${text.slice(0, 57)}...`;
  return text;
}

export function readableFallback(value: string): string {
  return isMojibake(value) ? "" : value;
}

const SUSPICIOUS_CODEPOINTS = new Set([
  0x5b9c, 0x59dd, 0x93ac, 0x7487, 0x8be7, 0x941e, 0x8bf2, 0x7a0b,
  0x6769, 0x935b, 0x93c2, 0x6d60, 0x6d93, 0x6939, 0x7d31, 0x5f47,
]);

function isMojibake(value: string): boolean {
  return [...value].some((char) => SUSPICIOUS_CODEPOINTS.has(char.codePointAt(0) ?? 0));
}

// ── Timeline title ─────────────────────────────────────────

export function readableTimelineTitle(cell: ActivityCellState): string {
  const title = readableFallback(cell.title || "").trim();
  if (cell.activityKind === "reasoning" || cell.activityKind === "planning" || cell.activityKind === "providerReasoning") {
    return title === "Thinking" || title === "Reasoning" ? "" : title;
  }
  if (title) return title;
  return readableToolName(cell);
}

export function readableToolName(cell: ActivityCellState): string {
  const records = cell.toolCallRecords ?? [];
  const first = records[0];
  const count = Math.max(1, records.length);

  switch (cell.activityKind) {
    case "fileRead":
      return count === 1 ? "已读取文件" : `已读取 ${count} 个文件`;
    case "workspaceSearch":
      return count === 1 ? "已搜索工作区" : `已搜索 ${count} 次`;
    case "commandExecution":
      return "已运行命令";
    case "fileChange":
      return count === 1 ? "已编辑文件" : `已编辑 ${count} 个文件`;
    case "webSearch":
      return count === 1 ? "已搜索网页" : `已搜索网页 ${count} 次`;
    case "mcpToolCall": {
      const server = first?.name?.match(/^mcp__([^_]+)__/)?.[1] ?? "";
      return server ? `已调用 ${server}` : "已调用 MCP";
    }
    case "reasoning":
    case "planning":
    case "providerReasoning":
      return "";
    case "genericTool":
      return first?.name === "todo_write" ? "已更新计划" : (first?.name ?? "已调用工具");
    default:
      return cell.activityKind;
  }
}

// ── URL helpers ────────────────────────────────────────────

export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function firstHttpUrl(value?: string): string {
  const match = value?.match(/https?:\/\/[^\s)]+/i);
  return match?.[0] ?? "";
}

function canonicalActivityTarget(target: string, targetKind: ActivityDetail["targetKind"]): string {
  const value = target.trim();
  if (targetKind !== "url" || !isHttpUrl(value)) return value;
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    url.hostname = url.hostname.replace(/^www\./i, "").toLowerCase();
    url.pathname = url.pathname.replace(/\/+$/g, "");
    return url.toString();
  } catch {
    return value;
  }
}

// ── Arg helpers ────────────────────────────────────────────

export function stringArg(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function fileLabel(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop() || path;
}

function webTarget(record: NonNullable<ActivityCellState["toolCallRecords"]>[number]): string {
  const args = record.args ?? {};
  return (
    stringArg(args.url) ||
    stringArg(args.source_url) ||
    stringArg(record.sourceUrl) ||
    firstHttpUrl(record.displaySummary) ||
    firstHttpUrl(record.summary) ||
    ""
  );
}

function detailTargetKind(target: string, preferred: ActivityDetail["targetKind"]): ActivityDetail["targetKind"] {
  if (isHttpUrl(target)) return "url";
  return target ? preferred : "text";
}

// ── Read-file line metadata ───────────────────────────────

export function readFileLineInfoLabel(record: ActivityToolRecord): string {
  const range = readFileLineRangeLabel(record);
  if (range) return range;
  const totalLines = readFileTotalLineCount(record);
  return totalLines ? `L1-L${totalLines}` : "";
}

function readFileLineRangeLabel(record: ActivityToolRecord): string {
  if (!/^read_file$/i.test(record.name)) return "";
  const args = record.args ?? {};
  const start = positiveIntArg(args.start_line ?? args.startLine ?? args.line);
  const end = positiveIntArg(args.end_line ?? args.endLine);
  if (start && end) return `L${start}-L${end}`;
  if (start) return `L${start}+`;
  if (end) return `L1-L${end}`;
  return "";
}

function readFileTotalLineCount(record: ActivityToolRecord): number | null {
  if (!/^read_file$/i.test(record.name)) return null;
  for (const text of [record.summary, record.contentPreview]) {
    const count = readFileTotalLineCountFromText(text);
    if (count) return count;
  }
  return null;
}

function readFileTotalLineCountFromText(text: string | undefined): number | null {
  if (!text) return null;

  const artifactHeader = text.match(/^File .+ \((\d+) lines, approx [^)]+\) was saved as an artifact\./m);
  const artifactLines = positiveIntArg(artifactHeader?.[1]);
  if (artifactLines) return artifactLines;

  let maxLineNumber = 0;
  for (const match of text.matchAll(/^\s*(\d+)→/gm)) {
    const lineNumber = positiveIntArg(match[1]);
    if (lineNumber && lineNumber > maxLineNumber) maxLineNumber = lineNumber;
  }
  if (maxLineNumber > 0) return maxLineNumber;

  const inlineContent = text.match(/^([\s\S]*?)\n\n\[(?:content_hash|range_hash):[^\]]+\](?:\n\[range only;[^\]]+\])?\s*$/);
  if (inlineContent?.[1] != null) {
    return inlineContent[1].split(/\r?\n/).length;
  }
  return null;
}

function positiveIntArg(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    const intValue = Math.trunc(value);
    return intValue > 0 ? intValue : null;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number.parseInt(value.trim(), 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }
  return null;
}

// ── Record details ─────────────────────────────────────────

function describeTodos(value: unknown): string {
  const todos = Array.isArray(value) ? value : [];
  if (todos.length === 0) return "";
  const statusOf = (item: unknown): string => {
    if (!item || typeof item !== "object") return "";
    return String((item as { status?: unknown }).status ?? "");
  };
  const running = todos.filter((item) => statusOf(item) === "in_progress").length;
  const completed = todos.filter((item) => statusOf(item) === "completed").length;
  const blocked = todos.filter((item) => statusOf(item) === "blocked").length;
  const parts = [`${todos.length} 项`];
  if (running) parts.push(`${running} 进行中`);
  if (completed) parts.push(`${completed} 已完成`);
  if (blocked) parts.push(`${blocked} 阻塞`);
  return parts.join("，");
}

function registerActivityDetailProvider(
  names: string[],
  provider: (record: ActivityToolRecord, args: Record<string, unknown>, name: string) => ToolActivityDetail,
): void {
  for (const toolName of names) {
    registerToolActivityDetail(toolName, ({ record, args, name }) => provider(record, args, name));
  }
}

function registerBuiltInActivityDetailProviders(): void {
  registerActivityDetailProvider(["todo_write"], (_record, args) => ({
    label: "任务",
    target: describeTodos(args.todos),
    targetKind: "text",
  }));

  registerActivityDetailProvider(["read_artifact"], (_record, args) => ({
    label: "读取",
    target: "完整内容",
    targetKind: "text",
  }));

  registerActivityDetailProvider(["read_file"], (record, args) => {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return {
      label: "读取",
      target,
      targetKind: detailTargetKind(target, "file"),
      lineInfo: readFileLineInfoLabel(record),
    };
  });

  registerActivityDetailProvider(["write_file"], (_record, args) => {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return { label: "写入", target, targetKind: detailTargetKind(target, "file") };
  });

  registerActivityDetailProvider(["edit_file", "apply_patch"], (_record, args) => {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return { label: "编辑", target, targetKind: detailTargetKind(target, "file") };
  });

  registerActivityDetailProvider(["grep", "grep_files", "glob", "glob_files", "list_files", "fuzzy_search"], (_record, args) => ({
    label: "搜索",
    target: stringArg(args.pattern ?? args.query ?? args.path ?? args.directory ?? ""),
    targetKind: "text",
  }));

  registerActivityDetailProvider(["run_command", "shell_command", "bash", "powershell", "terminal"], (_record, args) => ({
    label: "运行",
    target: stringArg(args.command ?? args.cmd ?? args.script ?? ""),
    targetKind: "text",
  }));

  registerActivityDetailProvider(["web_search", "search_web"], (_record, args) => ({
    label: "搜索",
    target: stringArg(args.query ?? args.q ?? ""),
    targetKind: "text",
  }));

  registerActivityDetailProvider(["web_fetch"], (record) => {
    const target = webTarget(record);
    return { label: "读取", target, targetKind: detailTargetKind(target, "url") };
  });
}

registerBuiltInActivityDetailProviders();

function describeRecordDetail(
  record: ActivityToolRecord,
  developerMode = false,
): Pick<ActivityDetail, "label" | "target" | "targetKind" | "lineInfo"> {
  const args = record.args ?? {};
  const name = record.name.toLowerCase();
  const registered = getToolActivityDetail(record.name)?.({ record, args, name, developerMode });
  if (registered) return registered;

  if (name === "todo_write") {
    return { label: "任务", target: describeTodos(args.todos), targetKind: "text" };
  }
  if (/read_artifact/i.test(name)) {
    return { label: "读取", target: "完整内容", targetKind: "text" };
  }
  if (/read_file/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return {
      label: "读取",
      target,
      targetKind: detailTargetKind(target, "file"),
      lineInfo: readFileLineInfoLabel(record),
    };
  }
  if (/write_file/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return { label: "写入", target, targetKind: detailTargetKind(target, "file") };
  }
  if (/edit_file/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return { label: "编辑", target, targetKind: detailTargetKind(target, "file") };
  }
  if (/grep|glob|list_files|fuzzy_search/i.test(name)) {
    return { label: "搜索", target: stringArg(args.pattern ?? args.query ?? args.path ?? args.directory ?? ""), targetKind: "text" };
  }
  if (/run_command|bash|powershell|terminal/i.test(name)) {
    return { label: "运行", target: stringArg(args.command ?? ""), targetKind: "text" };
  }
  if (/web_search/i.test(name)) {
    return { label: "搜索", target: stringArg(args.query ?? ""), targetKind: "text" };
  }
  if (/web_fetch/i.test(name)) {
    const target = webTarget(record);
    return { label: "读取", target, targetKind: detailTargetKind(target, "url") };
  }
  if (record.displaySummary) {
    return { label: name.replace(/^mcp__\w+__/, ""), target: record.displaySummary, targetKind: detailTargetKind(record.displaySummary, "text") };
  }
  return { label: name.replace(/^mcp__\w+__/, "").replace(/_/g, " "), target: "", targetKind: "text" };
}

export function describeRecordDetails(
  records: NonNullable<ActivityCellState["toolCallRecords"]>,
  developerMode: boolean,
): ActivityDetail[] {
  const details = new Map<string, ActivityDetail>();
  for (const record of records) {
    const { label, target, targetKind, lineInfo } = describeRecordDetail(record, developerMode);
    const key = `${label}\n${targetKind}\n${canonicalActivityTarget(target, targetKind)}\n${lineInfo ?? ""}`;
    const existing = details.get(key);
    if (existing) {
      existing.count += 1;
      existing.durationMs = (existing.durationMs ?? 0) + (record.durationMs ?? 0);
      continue;
    }
    details.set(key, { label, target, targetKind, lineInfo, count: 1, durationMs: record.durationMs ?? null });
  }
  return [...details.values()];
}

// ── Output preview ─────────────────────────────────────────

export function hasOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): boolean {
  if (!records || records.length === 0) return false;
  return records.some((r) => Boolean(recordOutputText(r)));
}

export function getOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): string {
  if (!records || records.length === 0) return "";
  const last = records[records.length - 1];
  const output = last ? recordOutputText(last) : "";
  const lines = output.split("\n");
  const tail = lines.slice(-24).join("\n");
  return tail.length > 1600 ? `...${tail.slice(-1600)}` : tail;
}

function recordOutputText(record: NonNullable<ActivityCellState["toolCallRecords"]>[number]): string {
  const direct = record.outputPreview?.trim() || record.contentPreview?.trim();
  if (direct) return direct;
  const summaryOutput = commandResultOutputText(record.summary);
  if (summaryOutput) return summaryOutput;
  if (/^read_artifact$/i.test(record.name)) return record.summary?.trim() || "";
  return "";
}

function commandResultOutputText(text: string | undefined): string {
  const raw = String(text || "").trimEnd();
  if (!raw) return "";
  const withoutStatus = raw.replace(/^Exit code:\s*-?\d+(?:\s+\(failed\))?\s*(?:\r?\n){0,2}/i, "");
  return withoutStatus.trimEnd();
}

// ── Long running ───────────────────────────────────────────

export function isLongRunning(startedAt: number | undefined): boolean {
  return startedAt != null && Date.now() - startedAt > 10_000;
}

export function getLongRunningExplanation(cell: ActivityCellState): string {
  const records = cell.toolCallRecords ?? [];
  const first = records[0];
  const args = first?.args ?? {};

  if (cell.activityKind === "commandExecution") {
    const cmd = String(args.command ?? "").toLowerCase();
    if (/npm\s+install|yarn\s+install|pnpm\s+install/.test(cmd)) {
      return "正在下载依赖包，这通常需要 20-60 秒。可以使用 Ctrl+B 放到后台继续。";
    }
    if (/git\s+clone/.test(cmd)) {
      return "正在克隆仓库，取决于仓库大小可能需要 30-120 秒。";
    }
    if (/pytest|npm\s+test|yarn\s+test/.test(cmd)) {
      return "正在运行测试，完整测试套件可能需要数分钟。";
    }
    if (/build|compile/.test(cmd)) {
      return "正在构建项目，编译时间取决于项目大小。";
    }
    return "某些命令（如安装、测试、构建）需要较长时间。可以使用 Ctrl+B 放到后台。";
  }

  if (cell.activityKind === "webSearch" || (cell.activityKind === "genericTool" && first?.name.includes("web"))) {
    return "网络请求可能因远程服务器响应慢或网络延迟而耗时较长。";
  }

  if (cell.activityKind === "fileRead" || cell.activityKind === "fileChange") {
    return "处理大文件或多个文件时可能需要较长时间。";
  }

  return "此操作通常需要较长时间完成。可以使用 Ctrl+B 放到后台继续。";
}

// ── Duration formatting ────────────────────────────────────

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remainSecs = secs % 60;
  return `${mins}m${remainSecs}s`;
}
