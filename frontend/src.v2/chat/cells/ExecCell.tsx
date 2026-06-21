import { useCallback, useEffect, useMemo, useState } from "react";
import type React from "react";
import {
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  Clock3,
  Copy,
  LoaderCircle,
  StopCircle,
} from "lucide-react";
import type { ExecCellState } from "./cellTypes";
import { shortCommand } from "./activityCellHelpers";
import { extractCommandCommentLabel } from "../../lib/command-comment-label";
import "./cells.css";

const streamOutputText = (full: string | undefined, preview: string[]): string =>
  (full ?? preview.join("\n")).trimEnd();

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
    cell.status === "running" ||
    cell.status === "pending_approval" ||
    !cell.collapsed,
  );
  const [copied, setCopied] = useState(false);
  const stdoutText = useMemo(
    () => streamOutputText(cell.stdoutFull, cell.stdoutPreview),
    [cell.stdoutFull, cell.stdoutPreview],
  );
  const stderrText = useMemo(
    () => streamOutputText(cell.stderrFull, cell.stderrPreview),
    [cell.stderrFull, cell.stderrPreview],
  );
  const outputText = useMemo(() => {
    const stdout = stdoutText;
    const stderr = stderrText;
    if (stdout && stderr) return `${stdout}\n[stderr]\n${stderr}`;
    return stdout || stderr;
  }, [stderrText, stdoutText]);
  const hasOutput = Boolean(stdoutText || stderrText);
  const labelStreams = Boolean(stdoutText && stderrText);

  useEffect(() => {
    if (cell.status === "running") setExpanded(true);
  }, [cell.status]);

  const statusColor =
    cell.status === "running" || cell.status === "pending_approval"
      ? "running"
      : cell.status === "success"
        ? "success"
        : cell.status === "failed"
          ? "failed"
          : "cancelled";

  const statusLabel =
    cell.status === "pending_approval"
      ? "等待授权"
      : cell.status === "running"
        ? "运行中"
        : cell.status === "success"
          ? "成功"
          : cell.status === "failed"
            ? "失败"
            : "已取消";
  const title = commandTitle(cell.status);
  const commandPreview = shortCommand(cell.command).replace(/^\$\s*/, "");
  // Prefer a leading `# comment` as the human-readable label (what the model
  // wrote for the user to read); fall back to the raw command preview. The full
  // command stays available via the title tooltip and the expanded shell block.
  const commandLabel = extractCommandCommentLabel(cell.command) ?? commandPreview;

  const duration =
    cell.durationMs != null
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
    <div className={`exec-cell ${cellStateClass}`}>
      <div className="exec-cell-header-row">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="exec-cell-header-button"
          aria-expanded={expanded}
        >
          <span className={`exec-cell-status-badge exec-cell-status-${statusColor}`}>
            <StatusIcon status={cell.status} />
          </span>
          <span className="exec-cell-title">{title}</span>
          <span className="exec-cell-command-preview" title={cell.command}>{commandLabel}</span>
          <span className="exec-cell-meta">
            {statusLabel}
            {duration ? ` · ${duration}` : ""}
            {cell.exitCode != null && cell.status === "failed"
              ? ` · exit ${cell.exitCode}`
              : ""}
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
            <StopCircle size={12} />
            停止
          </button>
        )}
      </div>

      {expanded && (
        <div className="exec-cell-output-stack">
          <div className="exec-cell-shell-header">
            <span>Shell</span>
            <span>{statusLabel}</span>
          </div>
          <pre className="exec-cell-shell-command">{`$ ${cell.command}`}</pre>
          {hasOutput ? (
            <>
              {stdoutText && (
                <OutputSection
                  label={labelStreams ? "stdout" : undefined}
                  text={stdoutText}
                  tone="normal"
                />
              )}
              {stderrText && (
                <OutputSection
                  label="stderr"
                  text={stderrText}
                  tone={cell.status === "failed" ? "error" : "warning"}
                />
              )}
            </>
          ) : cell.status === "running" ? (
            <div className="exec-cell-waiting-output">
              等待输出...
            </div>
          ) : (
            <div className="exec-cell-empty-output">
              {cell.status === "cancelled" ? "已取消" : "无输出"}
            </div>
          )}
          <button
            type="button"
            onClick={handleCopy}
            title={copied ? "已复制" : "复制输出"}
            className="exec-cell-copy-button"
          >
            <Copy size={11} />
          </button>
        </div>
      )}
    </div>
  );
}

function commandTitle(status: ExecCellState["status"]): string {
  if (status === "pending_approval") return "等待运行命令";
  if (status === "running") return "正在运行命令";
  if (status === "failed") return "命令运行失败";
  if (status === "cancelled") return "命令已取消";
  return "已运行命令";
}

function StatusIcon({ status }: { status: ExecCellState["status"] }) {
  if (status === "running") return <LoaderCircle size={12} className="exec-cell-spin-icon" />;
  if (status === "pending_approval") return <Clock3 size={12} />;
  if (status === "success") return <CheckCircle2 size={12} />;
  if (status === "failed") return <CircleAlert size={12} />;
  return <CircleDashed size={12} />;
}

function OutputSection({
  label,
  text,
  tone,
}: {
  label?: "stdout" | "stderr";
  text: string;
  tone: "normal" | "warning" | "error";
}) {
  return (
    <section aria-label={label ? `${label} output` : "command output"} className="exec-cell-output-section">
      {label && (
        <div className={`exec-cell-output-label exec-cell-output-label-${tone}`}>
          {label}
        </div>
      )}
      <pre className={`exec-cell-output-pre exec-cell-output-pre-${tone}`}>
        {text}
      </pre>
    </section>
  );
}
