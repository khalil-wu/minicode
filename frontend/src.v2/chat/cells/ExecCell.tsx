import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import {
  Copy,
  StopCircle,
} from "lucide-react";
import stripAnsi from "strip-ansi";
import type { ExecCellState } from "./cellTypes";
import { shortCommand } from "./activityCellHelpers";
import { extractCommandCommentLabel } from "../../lib/command-comment-label";
import { StatusIcon } from "../../components/icons";
import "./cells.css";

const streamOutputText = (full: string | undefined, preview: string[]): string =>
  stripAnsi(full ?? preview.join("\n")).trimEnd();

/** ExecCell renders a standalone command execution summary. */
export function ExecCell({
  cell,
  isActive = false,
  onStop,
}: {
  cell: ExecCellState;
  isActive?: boolean;
  onStop?: () => void;
}) {
  const [expanded, setExpanded] = useState(
    cell.status === "pending_approval" || (cell.status !== "running" && !cell.collapsed),
  );
  const [copied, setCopied] = useState(false);
  const materializeOutput = expanded || cell.status === "running";
  const stdoutText = useMemo(
    () => materializeOutput ? streamOutputText(cell.stdoutFull, cell.stdoutPreview) : "",
    [cell.stdoutFull, cell.stdoutPreview, materializeOutput],
  );
  const stderrText = useMemo(
    () => materializeOutput ? streamOutputText(cell.stderrFull, cell.stderrPreview) : "",
    [cell.stderrFull, cell.stderrPreview, materializeOutput],
  );
  const outputText = useMemo(() => {
    const stdout = stdoutText;
    const stderr = stderrText;
    if (stdout && stderr) return `${stdout}\n[stderr]\n${stderr}`;
    return stdout || stderr;
  }, [stderrText, stdoutText]);
  const hasOutput = Boolean(stdoutText || stderrText);
  const labelStreams = Boolean(stdoutText && stderrText);

  const statusColor =
    cell.status === "running" || cell.status === "pending_approval"
      ? "running"
      : cell.status === "success"
        ? "success"
        : cell.status === "failed"
          ? "failed"
          : cell.status === "partial"
            ? "partial"
            : "cancelled";

  const statusLabel =
    cell.status === "pending_approval"
      ? "等待授权"
      : cell.status === "running"
        ? "运行中"
        : cell.background && cell.status === "success"
          ? "后台运行"
          : cell.status === "success"
            ? "成功"
            : cell.status === "failed"
              ? "失败"
              : cell.status === "partial"
                ? "未完整结束"
                : "已取消";
  const statusMeta = cell.exitCode != null ? `exit ${cell.exitCode}` : statusLabel;
  const title = commandTitle(cell.status, Boolean(cell.background));
  const commandPreview = shortCommand(cell.command).replace(/^\$\s*/, "");
  // Prefer a leading `# comment` as the human-readable label (what the model
  // wrote for the user to read); fall back to the raw command preview. The full
  // command stays available via the title tooltip and the expanded shell block.
  const commandLabel = extractCommandCommentLabel(cell.command) ?? commandPreview;
  const showExpandedCommand = commandLabel !== commandPreview;
  const collapsedOutputPreview = useMemo(
    () => compactInlineOutputPreview(stdoutText || stderrText),
    [stderrText, stdoutText],
  );

  const duration =
    !cell.background && cell.durationMs != null
      ? cell.durationMs < 1000
        ? `${cell.durationMs}ms`
        : `${(cell.durationMs / 1000).toFixed(1)}s`
      : "";

  const cellStateClass =
    cell.status === "failed"
      ? "exec-cell-failed"
      : cell.status === "pending_approval"
        ? "exec-cell-pending"
        : isActive || cell.status === "running"
          ? "exec-cell-running"
          : "";

  const handleCopy = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      const full = outputText || cell.command;
      navigator.clipboard.writeText(full).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    },
    [cell.command, outputText],
  );

  const stopCommand = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    onStop?.();
  }, [onStop]);

  return (
    <div
      className={`exec-cell ${cellStateClass}`}
      data-status={cell.status}
      data-expanded={expanded ? "true" : "false"}
    >
      <div className="exec-cell-header-row">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="exec-cell-header-button"
          aria-expanded={expanded}
        >
          <span className={`exec-cell-status-badge exec-cell-status-${statusColor}`}>
            <StatusIcon status={cell.status} size={14} spinningClassName="exec-cell-spin-icon" />
          </span>
          <span className="exec-cell-title">{title}</span>
          <span className="exec-cell-command-preview" title={cell.command}>{commandLabel}</span>
          <span className="exec-cell-meta">
            {statusMeta}
            {duration ? ` · ${duration}` : ""}
          </span>
        </button>
        {cell.status === "running" && onStop && (
          <button
            type="button"
            onClick={stopCommand}
            title="停止命令"
            aria-label="停止命令"
            className="exec-cell-stop-button"
          >
            <StopCircle size={14} />
            停止
          </button>
        )}
      </div>

      {expanded && (
        <div className="exec-cell-output-stack">
          <div className="exec-cell-shell-header">
            <span>Shell</span>
            <span>{statusMeta}</span>
          </div>
          {showExpandedCommand ? (
            <pre className="exec-cell-shell-command">
              <span className="exec-cell-shell-prompt">$</span>
              <span className="exec-cell-shell-command-text">{cell.command}</span>
            </pre>
          ) : null}
          {hasOutput ? (
            <>
              {stdoutText && (
                <OutputSection
                  label={labelStreams ? "stdout" : undefined}
                  text={stdoutText}
                  tone="normal"
                  followTail={cell.status === "running"}
                />
              )}
              {stderrText && (
                <OutputSection
                  label="stderr"
                  text={stderrText}
                  tone={cell.status === "failed" ? "error" : "warning"}
                  followTail={cell.status === "running"}
                />
              )}
            </>
          ) : cell.status === "running" ? (
            <div className="exec-cell-waiting-output">
              等待输出...
            </div>
          ) : (
            <div className="exec-cell-empty-output">
              {cell.status === "cancelled"
                ? "已取消"
                : cell.background && cell.status === "success"
                  ? "后台命令已启动；状态和输出会保留在活动任务中。"
                  : cell.status === "success"
                    ? "命令已完成，无 stdout/stderr 输出。"
                    : "无输出"}
            </div>
          )}
          <button
            type="button"
            onClick={handleCopy}
            title={copied ? "已复制" : "复制输出"}
            className="exec-cell-copy-button"
          >
            <Copy size={14} />
          </button>
        </div>
      )}
      {!expanded && cell.status === "running" && (
        <div className="exec-cell-collapsed-output" aria-label="Command output preview">
          <span className="exec-cell-collapsed-output-marker" aria-hidden="true">&gt;</span>
          <span className="exec-cell-collapsed-output-label">
            {hasOutput ? (stderrText && !stdoutText ? "stderr" : "output") : "waiting"}
          </span>
          <span className="exec-cell-collapsed-output-text">
            {hasOutput ? collapsedOutputPreview : "等待输出..."}
          </span>
        </div>
      )}
    </div>
  );
}

function compactInlineOutputPreview(value: string): string {
  const lines = value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.slice(-2).join(" · ");
}

function commandTitle(status: ExecCellState["status"], background: boolean): string {
  if (status === "pending_approval") return "等待运行命令";
  if (status === "running") return "正在运行命令";
  if (background && status === "success") return "已启动后台命令";
  if (status === "partial") return "命令未完整结束";
  if (status === "cancelled") return "命令已取消";
  return "已运行命令";
}

function OutputSection({
  label,
  text,
  tone,
  followTail = false,
}: {
  label?: "stdout" | "stderr";
  text: string;
  tone: "normal" | "warning" | "error";
  followTail?: boolean;
}) {
  const outputRef = useRef<HTMLPreElement>(null);
  const stickToTailRef = useRef(true);

  useLayoutEffect(() => {
    const output = outputRef.current;
    if (!output || !followTail || !stickToTailRef.current) return;
    output.scrollTop = output.scrollHeight;
  }, [followTail, text]);

  return (
    <section aria-label={label ? `${label} output` : "command output"} className="exec-cell-output-section">
      {label && (
        <div className={`exec-cell-output-label exec-cell-output-label-${tone}`}>
          {label}
        </div>
      )}
      <pre
        ref={outputRef}
        className={`exec-cell-output-pre exec-cell-output-pre-${tone}`}
        onScroll={(event) => {
          const output = event.currentTarget;
          stickToTailRef.current = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
        }}
      >
        {text}
      </pre>
    </section>
  );
}
