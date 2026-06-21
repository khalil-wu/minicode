import { useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { ActivityCell } from "./ActivityCell";
import {
  firstHttpUrl,
  shortCommand,
  shortTarget,
  stringArg,
} from "./activityCellHelpers";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import "./cells.css";

export function ActivityGroupCell({
  cells,
  isActive = false,
  defaultCollapsed = false,
}: {
  cells: ActivityCellState[];
  isActive?: boolean;
  defaultCollapsed?: boolean;
}) {
  const status = groupStatus(cells, isActive);
  const [expanded, setExpanded] = useState(() => !defaultCollapsed);
  const groupIdentity = useMemo(() => cells.map((cell) => cell.id).join("|"), [cells]);
  const previousGroupIdentity = useRef(groupIdentity);
  const userToggledRef = useRef(false);
  const summaryItems = useMemo(() => buildSummaryItems(cells), [cells]);
  const previewItems = useMemo(() => buildPreviewItems(cells), [cells]);

  const progress = useMemo(() => {
    const completed = cells.filter(c => c.status === "done").reduce((sum, cell) => sum + recordCount(cell), 0);
    const failed = cells.filter(c => c.status === "failed" || c.status === "interrupted").reduce((sum, cell) => sum + recordCount(cell), 0);
    const running = cells.filter(c => c.status === "running").reduce((sum, cell) => sum + recordCount(cell), 0);
    const total = totalRecordCount(cells);
    return { completed, failed, running, total };
  }, [cells]);

  // Only re-sync to defaultCollapsed when it actually flips — NOT on every
  // stream tick. The previous deps ([cells, ..., status]) reset `expanded`
  // whenever the rebuilt `cells` array reference changed (every event during
  // streaming), discarding the user's manual collapse/expand.
  useEffect(() => {
    if (previousGroupIdentity.current !== groupIdentity) {
      previousGroupIdentity.current = groupIdentity;
      userToggledRef.current = false;
      setExpanded(!defaultCollapsed);
      return;
    }
    if (userToggledRef.current) return;
    setExpanded(!defaultCollapsed);
  }, [defaultCollapsed, groupIdentity]);

  if (cells.length === 0) return null;

  const kind = groupKind(cells);
  const title = groupTitle(cells, status);
  const effectiveExpanded = expanded;
  const progressLabel = kind === "context"
    ? `${progress.total} 个来源`
    : `${progress.completed + progress.failed}/${progress.total}`;

  const groupStateClass = `activity-group-cell-${status}`;

  return (
    <section className={`activity-group-cell ${groupStateClass}`} data-group-kind={kind}>
      <button
        type="button"
        aria-label={effectiveExpanded ? "Collapse activity details" : "Expand activity details"}
        aria-expanded={effectiveExpanded}
        data-toggleable="true"
        onClick={() => {
          userToggledRef.current = true;
          setExpanded((value) => !value);
        }}
        className="activity-group-header"
      >
        {effectiveExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <StatusIcon status={status} />
        <span className="activity-group-title">{title}</span>

        {cells.length > 1 && status !== "done" && (
          <span className="activity-group-progress-counter">
            {progressLabel}
          </span>
        )}

        {!effectiveExpanded && status === "running" && cells.length > 1 ? (
          <span className="activity-group-inline-progress">
            {cells.filter(c => c.status === "running").slice(0, 2).map((cell) => (
              <span key={cell.id} className="activity-group-inline-progress-item">
                {extractShortLabel(cell)}
              </span>
            ))}
          </span>
        ) : !effectiveExpanded && previewItems.length > 0 ? (
          <span className="activity-group-preview-list">
            {previewItems.map((item) => (
              <span key={item.key} className="activity-group-preview-item" title={item.title || item.text}>
                <span className="activity-group-preview-label">{item.label}</span>
                <span className="activity-group-preview-text">{item.text}</span>
                {item.meta && (
                  <span className="activity-group-preview-meta">{item.meta}</span>
                )}
              </span>
            ))}
          </span>
        ) : !effectiveExpanded && summaryItems.length > 0 ? (
          <span className="activity-group-summary">
            {summaryItems.map((item) => (
              <span key={item} className="activity-group-pill">
                {item}
              </span>
            ))}
          </span>
        ) : null}
      </button>
      {effectiveExpanded && (
        <div className="activity-group-body">
          {cells.map((cell) => (
            <ActivityCell
              key={cell.id}
              cell={cell}
              forceExpanded
              isActive={isActive && cell.status === "running"}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function StatusIcon({ status }: { status: "running" | "done" | "failed" }) {
  if (status === "running") {
    return <Loader2 size={14} className="spinner" style={{ color: "#3B82F6" }} />;
  }
  if (status === "failed") {
    return <TriangleAlert size={14} style={{ color: "var(--state-danger)" }} />;
  }
  return <CheckCircle2 size={14} style={{ color: "var(--text-muted)" }} />;
}

function groupStatus(cells: ActivityCellState[], isActive: boolean): "running" | "done" | "failed" {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "interrupted")) return "failed";
  if (isActive || cells.some((cell) => cell.status === "running")) return "running";
  return "done";
}

function groupTitle(cells: ActivityCellState[], status: "running" | "done" | "failed"): string {
  if (status === "failed") return "需要处理";

  const counts = activityCounts(cells);
  const total = totalRecordCount(cells);
  const fileRead = counts.fileRead ?? 0;
  const workspaceSearch = counts.workspaceSearch ?? 0;
  const webSearch = counts.webSearch ?? 0;
  const mcpToolCall = counts.mcpToolCall ?? 0;
  const commandExecution = counts.commandExecution ?? 0;
  const fileChange = counts.fileChange ?? 0;
  const isContextGroup = (
    fileRead > 0 ||
    workspaceSearch > 0 ||
    webSearch > 0 ||
    mcpToolCall > 0
  ) && commandExecution === 0 && fileChange === 0;

  if (isContextGroup) {
    return status === "running"
      ? "正在收集上下文"
      : total === 1 ? "已收集上下文" : `已收集 ${total} 个上下文来源`;
  }
  if (commandExecution > 0 && commandExecution === total) {
    return status === "running"
      ? "正在运行命令"
      : total === 1 ? "已运行命令" : `已运行 ${total} 条命令`;
  }
  if (fileChange > 0 && fileChange === total) {
    return total === 1 ? "已编辑文件" : `已编辑 ${total} 个文件`;
  }
  if ((counts.genericTool ?? 0) > 0 && (counts.genericTool ?? 0) === total) {
    const title = cells[0]?.title?.trim();
    return total === 1 && title ? title : `已处理 ${total} 项`;
  }

  const firstKind = cells[0]?.activityKind;
  switch (firstKind) {
    case "fileRead":
      return total === 1 ? "已读取文件" : `已读取 ${total} 个文件`;
    case "workspaceSearch":
      return total === 1 ? "已搜索工作区" : `已搜索 ${total} 次`;
    case "webSearch":
      return total === 1 ? "已搜索网页" : `已搜索网页 ${total} 次`;
    case "commandExecution":
      return total === 1 ? "已运行命令" : `已运行 ${total} 条命令`;
    case "fileChange":
      return total === 1 ? "已编辑文件" : `已编辑 ${total} 个文件`;
    case "mcpToolCall":
      return total === 1 ? "已调用 MCP" : `已调用 ${total} 个 MCP 工具`;
    case "reasoning":
    case "planning":
    case "providerReasoning":
      return "思考过程";
    default:
      return `已处理 ${total} 项`;
  }
}

function groupKind(cells: ActivityCellState[]): "context" | "command" | "change" | "tool" | "mixed" {
  const counts = activityCounts(cells);
  const total = totalRecordCount(cells);
  const fileRead = counts.fileRead ?? 0;
  const workspaceSearch = counts.workspaceSearch ?? 0;
  const webSearch = counts.webSearch ?? 0;
  const mcpToolCall = counts.mcpToolCall ?? 0;
  const commandExecution = counts.commandExecution ?? 0;
  const fileChange = counts.fileChange ?? 0;
  const contextTotal = fileRead + workspaceSearch + webSearch + mcpToolCall;
  if (contextTotal > 0 && commandExecution === 0 && fileChange === 0) return "context";
  if (commandExecution > 0 && commandExecution === total) return "command";
  if (fileChange > 0 && fileChange === total) return "change";
  if ((counts.genericTool ?? 0) > 0 && (counts.genericTool ?? 0) === total) return "tool";
  return "mixed";
}

function buildSummaryItems(cells: ActivityCellState[]): string[] {
  const items: string[] = [];
  const counts = activityCounts(cells);
  const fileRead = counts.fileRead ?? 0;
  const workspaceSearch = counts.workspaceSearch ?? 0;
  const webSearch = counts.webSearch ?? 0;
  const commandExecution = counts.commandExecution ?? 0;
  const fileChange = counts.fileChange ?? 0;
  const mcpToolCall = counts.mcpToolCall ?? 0;
  if (fileRead) items.push(`${fileRead} 个文件`);
  if (workspaceSearch) items.push(`${workspaceSearch} 次搜索`);
  if (webSearch) items.push(`${webSearch} 次联网`);
  if (commandExecution) items.push(`${commandExecution} 条命令`);
  if (fileChange) items.push(`${fileChange} 处编辑`);
  if (mcpToolCall) items.push(`${mcpToolCall} 个 MCP`);

  if (items.length === 0) {
    const seen = new Set<string>();
    for (const cell of cells) {
      const item = summaryForCell(cell);
      if (!item || seen.has(item)) continue;
      seen.add(item);
      items.push(item);
    }
  }
  return items.slice(0, 4);
}

interface ActivityPreviewItem {
  key: string;
  label: string;
  text: string;
  title?: string;
  meta?: string;
}

function buildPreviewItems(cells: ActivityCellState[]): ActivityPreviewItem[] {
  const items: ActivityPreviewItem[] = [];
  const seen = new Set<string>();
  for (const cell of cells) {
    const records = cell.toolCallRecords ?? [];
    if (records.length === 0) {
      const fallback = previewForCellWithoutRecords(cell);
      if (fallback) addPreviewItem(items, seen, fallback);
      continue;
    }
    for (const record of records) {
      const item = previewForRecord(cell, record);
      if (item) addPreviewItem(items, seen, item);
      if (items.length >= 3) break;
    }
    if (items.length >= 3) break;
  }
  return items;
}

function addPreviewItem(items: ActivityPreviewItem[], seen: Set<string>, item: Omit<ActivityPreviewItem, "key">) {
  const key = `${item.label}\n${item.text}\n${item.meta ?? ""}`;
  if (seen.has(key)) return;
  seen.add(key);
  items.push({ ...item, key });
}

function previewForCellWithoutRecords(cell: ActivityCellState): Omit<ActivityPreviewItem, "key"> | null {
  if (
    cell.activityKind === "reasoning" ||
    cell.activityKind === "planning" ||
    cell.activityKind === "providerReasoning" ||
    cell.activityKind === "processNote"
  ) {
    const text = cell.status === "running" ? "正在思考" : "思考过程";
    return {
      label: cell.status === "running" ? "思考" : "推理",
      text,
      title: text,
    };
  }

  const text = readableFallback(cell.subtitle || cell.title).trim();
  if (!text) return null;
  return {
    label: labelForActivityKind(cell.activityKind),
    text: shortPreviewText(text, 74),
    title: text,
  };
}

function previewForRecord(
  cell: ActivityCellState,
  record: ToolCallRecord,
): Omit<ActivityPreviewItem, "key"> | null {
  const args = record.args ?? {};
  const name = record.name.toLowerCase();
  const displayText = record.displaySummary || record.inputSummary || record.summary || "";

  if (cell.activityKind === "commandExecution" || /run_command|bash|powershell|terminal|shell/i.test(name)) {
    const command = stringArg(args.command ?? args.cmd) || displayText;
    if (!command) return null;
    const output = compactOutputPreview(record.outputPreview || record.stdoutPreview || record.stderrPreview || "");
    return {
      label: "运行",
      text: shortCommand(command).replace(/^\$\s*/, ""),
      title: command,
      meta: output || statusMeta(record),
    };
  }

  if (cell.activityKind === "fileChange" || /write|edit|patch|delete|remove|create|move|rename|save/i.test(name)) {
    const target = fileTarget(record);
    const diff = record.diff ? `+${record.diff.plus} -${record.diff.minus}` : "";
    return {
      label: /write/i.test(name) ? "写入" : "编辑",
      text: target ? shortPath(target) : shortPreviewText(displayText || record.name, 74),
      title: target || displayText,
      meta: diff || statusMeta(record),
    };
  }

  if (cell.activityKind === "webSearch" || /web_search|search_web|web_fetch|fetch/i.test(name)) {
    const url = stringArg(args.url ?? args.source_url ?? record.sourceUrl) || firstHttpUrl(displayText);
    const query = stringArg(args.query ?? args.q);
    const text = url ? hostLabel(url) : query || displayText;
    if (!text) return null;
    return {
      label: url ? "读取" : "搜索",
      text: shortPreviewText(text, 78),
      title: url || query || displayText,
      meta: record.provider || statusMeta(record),
    };
  }

  if (cell.activityKind === "mcpToolCall" || name.startsWith("mcp__")) {
    const [, server = "", tool = ""] = record.name.match(/^mcp__([^_]+)__(.+)$/i) ?? [];
    const target = (
      stringArg(args.query) ||
      stringArg(args.prompt) ||
      stringArg(args.path) ||
      stringArg(args.url) ||
      displayText
    );
    return {
      label: server || "MCP",
      text: shortPreviewText(target || tool || record.name, 78),
      title: target || record.name,
      meta: tool ? shortPreviewText(tool.replace(/_/g, " "), 24) : statusMeta(record),
    };
  }

  if (cell.activityKind === "fileRead" || /read_file|read_artifact/i.test(name)) {
    const target = fileTarget(record);
    if (!target && !displayText) return null;
    return {
      label: "读取",
      text: target ? shortPath(target) : shortPreviewText(displayText, 74),
      title: target || displayText,
      meta: statusMeta(record),
    };
  }

  if (cell.activityKind === "workspaceSearch" || /grep|glob|list_files|fuzzy_search/i.test(name)) {
    const target = (
      stringArg(args.pattern) ||
      stringArg(args.query) ||
      stringArg(args.glob) ||
      stringArg(args.directory) ||
      stringArg(args.path) ||
      displayText
    );
    if (!target) return null;
    return {
      label: /list/i.test(name) ? "列出" : "搜索",
      text: shortPreviewText(target, 78),
      title: target,
      meta: statusMeta(record),
    };
  }

  const target = displayText || record.inputSummary || record.name;
  return target
    ? {
      label: labelForActivityKind(cell.activityKind),
      text: shortPreviewText(target, 78),
      title: target,
      meta: statusMeta(record),
    }
    : null;
}

function fileTarget(record: ToolCallRecord): string {
  return stringArg(record.args.file_path ?? record.args.path ?? record.args.target ?? record.args.filename);
}

function shortPath(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 3) return shortTarget(normalized);
  return `.../${parts.slice(-3).join("/")}`;
}

function shortPreviewText(value: string, max = 80): string {
  const text = String(value).replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}…`;
}

function statusMeta(record: ToolCallRecord): string {
  if (record.status === "failed" || record.status === "blocked") return "失败";
  if (record.status === "partial") return "部分完成";
  if (record.status === "running" || record.status === "pending") return "运行中";
  return "";
}

function compactOutputPreview(value: string): string {
  const lines = String(value)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return shortPreviewText(lines.slice(-2).join(" · "), 88);
}

function hostLabel(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

function labelForActivityKind(kind: ActivityCellState["activityKind"]): string {
  switch (kind) {
    case "fileRead":
      return "读取";
    case "workspaceSearch":
      return "搜索";
    case "webSearch":
      return "联网";
    case "commandExecution":
      return "运行";
    case "fileChange":
      return "编辑";
    case "mcpToolCall":
      return "MCP";
    case "reasoning":
    case "planning":
    case "processNote":
    case "providerReasoning":
      return "备注";
    default:
      return "工具";
  }
}

function totalRecordCount(cells: ActivityCellState[]): number {
  return Math.max(1, cells.reduce((sum, cell) => sum + Math.max(1, cell.toolCallRecords?.length ?? 1), 0));
}

function recordCount(cell: ActivityCellState): number {
  return Math.max(1, cell.toolCallRecords?.length ?? Number(firstNumber(cell.subtitle || cell.title) || 1));
}

function activityCounts(cells: ActivityCellState[]) {
  return cells.reduce((counts, cell) => {
    counts[cell.activityKind] = (counts[cell.activityKind] ?? 0) + recordCount(cell);
    return counts;
  }, {} as Partial<Record<ActivityCellState["activityKind"], number>>);
}

function summaryForCell(cell: ActivityCellState): string {
  const subtitle = cell.subtitle ?? "";
  const count = String(recordCount(cell));
  switch (cell.activityKind) {
    case "reasoning":
    case "planning":
    case "processNote":
    case "providerReasoning":
      return cell.status === "running" ? "思考" : "推理";
    case "progress":
      return "进度";
    case "webSearch":
      if (/page|web|read/i.test(`${cell.title} ${subtitle}`)) {
        return count ? `${count} 个页面` : "已读取网页";
      }
      return count ? `${count} 次搜索` : "已搜索网页";
    case "fileRead":
      return count ? `${count} 个文件` : cell.title;
    case "workspaceSearch":
      return count ? `${count} 次搜索` : cell.title;
    case "commandExecution":
      return count ? `${count} 条命令` : cell.title;
    case "fileChange":
      return count ? `${count} 处编辑` : cell.title;
    case "mcpToolCall":
      return count ? `${count} 个 MCP` : cell.title;
    default:
      return readableFallback(cell.title);
  }
}

function firstNumber(text: string): string {
  return text.match(/\d+/)?.[0] ?? "";
}

function readableFallback(value: string): string {
  return isMojibake(value) ? "activity" : value;
}

function isMojibake(value: string): boolean {
  const suspicious = new Set([
    0x5b9c, 0x59dd, 0x93ac, 0x7487, 0x8be7, 0x941e, 0x8bf2, 0x7a0b,
    0x6769, 0x935b, 0x93c2, 0x6d60, 0x6d93, 0x6939, 0x7d31, 0x5f47,
  ]);
  return [...value].some((char) => suspicious.has(char.codePointAt(0) ?? 0));
}

function extractShortLabel(cell: ActivityCellState): string {
  const firstRecord = cell.toolCallRecords?.[0];
  if (!firstRecord) return cell.title.slice(0, 20);

  const args = firstRecord.args ?? {};
  const target = args.file_path ?? args.command ?? args.query ?? "";
  if (typeof target === "string" && target) {
    const parts = target.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1]?.slice(0, 20) || target.slice(0, 20);
  }
  return cell.title.slice(0, 20);
}
