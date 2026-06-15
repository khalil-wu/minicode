import { useEffect, useRef, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import "./cells.css";

/** Real-time elapsed timer — ticks every second while tool is running */
function useElapsedTime(startedAt: number | undefined, isRunning: boolean): string {
  const [elapsed, setElapsed] = useState("");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!startedAt || !isRunning) {
      if (intervalRef.current) clearInterval(intervalRef.current);
      return;
    }
    const tick = () => {
      const ms = Date.now() - startedAt;
      setElapsed(formatDuration(ms));
    };
    tick();
    intervalRef.current = setInterval(tick, 1000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [startedAt, isRunning]);

  return elapsed;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remainSecs = secs % 60;
  return `${mins}m${remainSecs}s`;
}

/**
 * ActivityCell — Claude Code style compact tool-call display.
 *
 * ● tool_name (details) 1.2s          ← running: blinking dot + bold name + elapsed
 * ● tool_name (details)              ← done: static dot + dim name
 * ● tool_name (details)              ← failed: red dot + red name
 *
 * Expandable on click to show detailed records.
 */
export function ActivityCell({
  cell,
  isActive = false,
}: {
  cell: ActivityCellState;
  isActive?: boolean;
}) {
  const developerMode = useAppStore((s) => s.viewMode === "verbose");
  const shouldAutoExpand = cell.status === "failed" || !cell.collapsed;
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);
  const [showErrorDetail, setShowErrorDetail] = useState(false);

  useEffect(() => {
    setIsExpanded(shouldAutoExpand);
  }, [cell.id, cell.status, cell.collapsed, shouldAutoExpand]);

  const hasRecords = cell.toolCallRecords && cell.toolCallRecords.length > 0;
  const canToggle = !isActive && hasRecords;
  const isRunning = isActive || cell.status === "running";
  const isFailed = cell.status === "failed" || cell.status === "interrupted";
  const elapsed = useElapsedTime(cell.startedAt, isRunning);

  const name = readableTimelineTitle(cell);
  const detail = cell.subtitle;

  // Determine cell state class
  const cellStateClass = isRunning
    ? "activity-cell-running"
    : isFailed
      ? "activity-cell-failed"
      : "activity-cell-completed";

  return (
    <div className={`activity-cell ${cellStateClass}`}>
      {/* Main line: ● name (detail) elapsed */}
      <button
        type="button"
        aria-label={canToggle ? (isExpanded ? "Collapse activity details" : "Expand activity details") : undefined}
        aria-expanded={canToggle ? isExpanded : undefined}
        disabled={!canToggle}
        data-clickable={canToggle}
        className="activity-cell-line"
        onClick={() => { if (canToggle) setIsExpanded((v) => !v); }}
      >
        {/* Dot indicator */}
        <span
          className="activity-cell-dot"
          data-running={isRunning}
          data-failed={isFailed}
          data-completed={!isRunning && !isFailed}
        >
          ●
        </span>

        {/* Bold tool name */}
        <span className="activity-cell-name" data-failed={isFailed}>
          {name}
        </span>

        {/* Detail in parentheses */}
        {detail && <span className="activity-cell-detail">({detail})</span>}

        {/* Progress text for running tools */}
        {isRunning && cell.progress?.text && (
          <span className="activity-cell-progress">{cell.progress.text}</span>
        )}

        {/* Elapsed time */}
        {elapsed && <span className="activity-cell-elapsed">{elapsed}</span>}

        {/* Expand toggle */}
        {canToggle && (
          <span className="activity-cell-toggle">
            {isExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </span>
        )}

        {/* Long-running warning */}
        {isRunning && isLongRunning(cell.startedAt) && (
          <>
            <span className="activity-cell-long-running">(ctrl+b for background)</span>
            <span
              className="activity-cell-info-icon"
              title={getLongRunningExplanation(cell)}
              aria-label="为什么这么久"
            >
              ⓘ
            </span>
          </>
        )}
      </button>

      {/* Expanded: detailed records */}
      {isExpanded && hasRecords && (
        <div className="activity-cell-expanded">
          {describeRecordDetails(cell.toolCallRecords!, developerMode).map(({ label, target, targetKind, count, durationMs }, i) => (
            <div key={`${label}-${target}-${i}`} className="activity-cell-detail-row">
              <span className="activity-cell-detail-dot">⎿</span>
              <span className="activity-cell-detail-name">{label}</span>
              <DetailTarget target={target} targetKind={targetKind} />
              {count > 1 && <span className="activity-cell-detail-count">{`x${count}`}</span>}
              {developerMode && durationMs != null && durationMs > 0 && (
                <span className="activity-cell-detail-duration">{durationMs}ms</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Inline output preview for running commands */}
      {isActive && cell.activityKind === "commandExecution" && hasOutputPreview(cell.toolCallRecords) && (
        <div className="activity-cell-output-preview">
          <pre className="activity-cell-output-pre">{getOutputPreview(cell.toolCallRecords)}</pre>
        </div>
      )}

      {/* Failed tool actions */}
      {isFailed && !isActive && (
        <div className="activity-cell-failed-actions">
          <button
            type="button"
            className="activity-cell-action-btn activity-cell-action-retry"
            onClick={() => handleRetry(cell)}
            title="重试此操作"
          >
            🔄 重试
          </button>
          <button
            type="button"
            className="activity-cell-action-btn"
            onClick={() => setShowErrorDetail((v) => !v)}
            title={showErrorDetail ? "隐藏错误详情" : "查看错误详情"}
          >
            👁️ {showErrorDetail ? "隐藏详情" : "查看详情"}
          </button>
        </div>
      )}

      {/* Error detail panel */}
      {isFailed && showErrorDetail && hasRecords && (
        <div className="activity-cell-error-detail">
          {cell.toolCallRecords!.map((record, i) => {
            const error = record.error || record.outputPreview || "";
            if (!error) return null;
            return (
              <div key={`error-${i}`} className="activity-cell-error-item">
                <div className="activity-cell-error-label">{record.name}</div>
                <pre className="activity-cell-error-pre">{error}</pre>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Display helpers ──────────────────────────────────────────

function readableTimelineTitle(cell: ActivityCellState): string {
  const title = readableFallback(cell.title || "").trim();
  if (cell.activityKind === "reasoning" && cell.status !== "running") {
    return title && title !== "Thinking" ? title : "Thought process";
  }
  if (title) return title;
  return readableToolName(cell);
}

function readableToolName(cell: ActivityCellState): string {
  const records = cell.toolCallRecords ?? [];
  const first = records[0];
  const count = Math.max(1, records.length);

  switch (cell.activityKind) {
    case "fileRead":
      return count === 1 ? "Read" : `Read ${count} files`;
    case "workspaceSearch":
      return count === 1 ? "Search" : `Search ${count}×`;
    case "commandExecution":
      return "Run";
    case "fileChange":
      return count === 1 ? "Edit" : `Edit ${count} files`;
    case "webSearch":
      return count === 1 ? "Search web" : `Search web ${count}×`;
    case "mcpToolCall": {
      const server = first?.name?.match(/^mcp__([^_]+)__/)?.[1] ?? "";
      return server ? `${server}` : "MCP";
    }
    case "reasoning":
    case "planning":
    case "providerReasoning":
      return "Thinking";
    case "genericTool":
      return first?.name === "todo_write" ? "Plan" : (first?.name ?? "Tool");
    default:
      return cell.activityKind;
  }
}

function readableToolDetail(cell: ActivityCellState): string {
  const records = cell.toolCallRecords ?? [];
  const first = records[0];
  if (!first) return "";

  const args = first.args ?? {};
  switch (cell.activityKind) {
    case "fileRead":
    case "fileChange":
      return shortTarget(
        (args.file_path ?? args.path ?? args.target ?? args.filename ?? "") as string
      ) || first.displaySummary || "";
    case "workspaceSearch":
      return shortTarget(
        (args.pattern ?? args.query ?? args.search_term ?? "") as string
      ) || first.displaySummary || "";
    case "commandExecution":
      return shortCommand((args.command ?? "") as string) || first.displaySummary || "";
    case "webSearch":
      return ((args.query ?? "") as string).slice(0, 60) || first.displaySummary || "";
    case "mcpToolCall":
    case "genericTool":
      return first.displaySummary ?? first.inputSummary ?? records[records.length - 1]?.displaySummary ?? "";
    default:
      return first.displaySummary ?? first.inputSummary ?? "";
  }
}

function shortTarget(value: string): string {
  const text = String(value).replace(/\\/g, "/").trim();
  if (!text) return "";
  const fileName = text.split("/").pop() ?? text;
  return fileName.length > 50 ? `${fileName.slice(0, 47)}...` : fileName;
}

function shortCommand(cmd: string): string {
  const text = cmd.trim();
  if (!text) return "";
  if (text.length > 60) return `${text.slice(0, 57)}...`;
  return text;
}

function readableFallback(value: string): string {
  return isMojibake(value) ? "" : value;
}

function isMojibake(value: string): boolean {
  const suspicious = new Set([
    0x5b9c, 0x59dd, 0x93ac, 0x7487, 0x8be7, 0x941e, 0x8bf2, 0x7a0b,
    0x6769, 0x935b, 0x93c2, 0x6d60, 0x6d93, 0x6939, 0x7d31, 0x5f47,
  ]);
  return [...value].some((char) => suspicious.has(char.codePointAt(0) ?? 0));
}

function isLongRunning(startedAt: number | undefined): boolean {
  return startedAt != null && Date.now() - startedAt > 10_000;
}

// ── Record details ───────────────────────────────────────────

interface ActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  count: number;
  durationMs: number | null;
}

function describeRecordDetails(
  records: NonNullable<ActivityCellState["toolCallRecords"]>,
  developerMode: boolean,
): ActivityDetail[] {
  const details = new Map<string, ActivityDetail>();
  for (const record of records) {
    const { label, target, targetKind } = describeRecordDetail(record, developerMode);
    const key = `${label}\n${targetKind}\n${target}`;
    const existing = details.get(key);
    if (existing) {
      existing.count += 1;
      existing.durationMs = (existing.durationMs ?? 0) + (record.durationMs ?? 0);
      continue;
    }
    details.set(key, { label, target, targetKind, count: 1, durationMs: record.durationMs ?? null });
  }
  return [...details.values()];
}

function describeRecordDetail(
  record: NonNullable<ActivityCellState["toolCallRecords"]>[number],
  _developerMode = false,
): Pick<ActivityDetail, "label" | "target" | "targetKind"> {
  const args = record.args ?? {};
  const name = record.name.toLowerCase();

  if (name === "todo_write") {
    return {
      label: "Tasks",
      target: describeTodos(args.todos),
      targetKind: "text",
    };
  }
  if (/read_file|read_artifact/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return {
      label: "read",
      target,
      targetKind: detailTargetKind(target, "file"),
    };
  }
  if (/write_file/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return {
      label: "write",
      target,
      targetKind: detailTargetKind(target, "file"),
    };
  }
  if (/edit_file/i.test(name)) {
    const target = stringArg(args.file_path ?? args.path ?? args.target ?? "");
    return {
      label: "edit",
      target,
      targetKind: detailTargetKind(target, "file"),
    };
  }
  if (/grep|glob|list_files|fuzzy_search/i.test(name)) {
    return {
      label: "search",
      target: stringArg(args.pattern ?? args.query ?? args.path ?? args.directory ?? ""),
      targetKind: "text",
    };
  }
  if (/run_command|bash|powershell|terminal/i.test(name)) {
    return {
      label: "run",
      target: stringArg(args.command ?? ""),
      targetKind: "text",
    };
  }
  if (/web_search/i.test(name)) {
    return { label: "search", target: stringArg(args.query ?? ""), targetKind: "text" };
  }
  if (/web_fetch/i.test(name)) {
    const target = webTarget(record);
    return { label: "fetch", target, targetKind: detailTargetKind(target, "url") };
  }
  if (record.displaySummary) {
    const target = record.displaySummary;
    return {
      label: name.replace(/^mcp__\w+__/, ""),
      target,
      targetKind: detailTargetKind(target, "text"),
    };
  }
  return {
    label: name.replace(/^mcp__\w+__/, "").replace(/_/g, " "),
    target: "",
    targetKind: "text",
  };
}

function DetailTarget({
  target,
  targetKind,
}: {
  target: string;
  targetKind: ActivityDetail["targetKind"];
}) {
  const text = target.trim();
  if (!text) return null;

  if (targetKind === "url" && isHttpUrl(text)) {
    return (
      <a
        className="activity-cell-detail-path activity-cell-detail-link activity-cell-detail-link-url"
        href={text}
        target="_blank"
        rel="noreferrer"
        title={text}
        onClick={(event) => event.stopPropagation()}
      >
        {text}
      </a>
    );
  }

  if (targetKind === "file") {
    return (
      <button
        type="button"
        className="activity-cell-detail-path activity-cell-detail-link activity-cell-detail-link-file"
        title={text}
        aria-label={`Open ${text}`}
        onClick={(event) => {
          event.stopPropagation();
          useAppStore.getState().openEditorFile(text, fileLabel(text));
        }}
      >
        {text}
      </button>
    );
  }

  return (
    <span className="activity-cell-detail-path" title={text}>
      {text}
    </span>
  );
}

function fileLabel(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop() || path;
}

function stringArg(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
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

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function firstHttpUrl(value?: string): string {
  const match = value?.match(/https?:\/\/[^\s)]+/i);
  return match?.[0] ?? "";
}

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
  const parts = [`${todos.length} ${todos.length === 1 ? "item" : "items"}`];
  if (running) parts.push(`${running} running`);
  if (completed) parts.push(`${completed} completed`);
  if (blocked) parts.push(`${blocked} blocked`);
  return parts.join(", ");
}

// ── Output preview helpers ────────────────────────────────────

function hasOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): boolean {
  if (!records || records.length === 0) return false;
  return records.some((r) => Boolean(r.outputPreview?.trim()));
}

function getOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): string {
  if (!records || records.length === 0) return "";
  const last = records[records.length - 1];
  const output = last?.outputPreview || "";
  const lines = output.split("\n");
  const tail = lines.slice(-5).join("\n");
  return tail.length > 400 ? `...${tail.slice(-400)}` : tail;
}

function getLongRunningExplanation(cell: ActivityCellState): string {
  const records = cell.toolCallRecords ?? [];
  const first = records[0];
  const args = first?.args ?? {};

  // Command execution explanations
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

  // Web search/fetch explanations
  if (cell.activityKind === "webSearch" || cell.activityKind === "genericTool" && first?.name.includes("web")) {
    return "网络请求可能因远程服务器响应慢或网络延迟而耗时较长。";
  }

  // File operations
  if (cell.activityKind === "fileRead" || cell.activityKind === "fileChange") {
    return "处理大文件或多个文件时可能需要较长时间。";
  }

  // Default
  return "此操作通常需要较长时间完成。可以使用 Ctrl+B 放到后台继续。";
}

async function handleRetry(cell: ActivityCellState) {
  // TODO: 实现重试逻辑
  // 目前显示一个提示，未来可以发送重试命令到后端
  const { pushToast } = await import("../../overlays/ToastContainer");
  pushToast("重试功能正在开发中，敬请期待", "info", 3000);

  // 未来实现：
  // 1. 提取失败的工具调用信息
  // 2. 发送 WebSocket 命令重新执行
  // 3. 或者让用户编辑参数后重试
}
