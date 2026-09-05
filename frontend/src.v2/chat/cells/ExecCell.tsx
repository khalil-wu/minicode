import { Check, ChevronDown, ChevronRight, Copy, Square, TerminalSquare } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ExecCellState } from "./cellTypes";
import { StatusIcon } from "../../components/icons";
import {
  cellStatusLabel,
  cellStatusTone,
  execCellStatus,
  formatCellDuration,
  isRunningCellStatus,
} from "./cellStatus";
import "./cells.css";

/**
 * A command has one compact lifecycle row and one optional output panel. The
 * row is the canonical process projection; the panel is only mounted after
 * explicit disclosure (or while the command is live).
 */
export function ExecCell({
  cell,
  onStop,
}: {
  cell: ExecCellState;
  isActive?: boolean;
  onStop?: () => void;
}) {
  const status = execCellStatus(cell.status);
  const statusColor = cellStatusTone(status);
  const statusLabel = cell.status === "pending_approval"
    ? "等待授权"
    : cell.background && status === "success"
      ? "后台运行"
      : cellStatusLabel(status);
  const statusMeta = cell.exitCode != null ? `exit ${cell.exitCode}` : statusLabel;
  const title = commandTitle(cell.status, Boolean(cell.background));
  const duration = cell.background ? "" : formatCellDuration(cell.durationMs);
  const running = isRunningCellStatus(status);
  const shouldAutoExpand = !cell.collapsed || cell.status === "failed" || cell.status === "partial";
  const [expanded, setExpanded] = useState(shouldAutoExpand);
  const [copied, setCopied] = useState(false);
  const userToggled = useRef(false);
  const previousId = useRef(cell.id);
  const previousRunning = useRef(running);

  useEffect(() => {
    const changed = previousId.current !== cell.id;
    const settled = previousRunning.current && !running;
    if (changed) {
      userToggled.current = false;
      setExpanded(shouldAutoExpand);
    } else if (!userToggled.current && settled) {
      setExpanded(shouldAutoExpand);
    } else if (!userToggled.current && shouldAutoExpand) {
      setExpanded(shouldAutoExpand);
    }
    previousId.current = cell.id;
    previousRunning.current = running;
  }, [cell.collapsed, cell.id, running, shouldAutoExpand]);

  const stdout = cell.stdoutFull ?? cell.stdoutPreview.join("\n");
  const stderr = cell.stderrFull ?? cell.stderrPreview.join("\n");
  const hasOutput = Boolean(stdout.trim() || stderr.trim());

  return (
    <div
      className={`exec-cell ${
        cell.status === "failed"
          ? "exec-cell-failed"
          : cell.status === "pending_approval"
            ? "exec-cell-pending"
            : running
              ? "exec-cell-running"
              : ""
      }`}
      data-status={cell.status}
      data-expanded={expanded ? "true" : "false"}
    >
      <div className="exec-cell-header-row">
        <button
          type="button"
          className="exec-cell-header-button"
          aria-expanded={expanded}
          aria-label={expanded ? "收起命令详情" : "展开命令详情"}
          onClick={() => {
            userToggled.current = true;
            setExpanded((value) => !value);
          }}
        >
          <span className={`exec-cell-status-badge exec-cell-status-${statusColor}`}>
            <TerminalSquare size={15} aria-hidden="true" />
          </span>
          <span className="exec-cell-title">{running ? "正在运行" : title}</span>
          <span className="exec-cell-command-preview" title={cell.command}>{cell.command}</span>
          <span className="exec-cell-meta">
            {statusMeta}
            {duration ? ` · ${duration}` : ""}
          </span>
          <span className="exec-cell-toggle" aria-hidden="true">
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
        </button>
        {cell.status === "running" && onStop && (
          <button
            type="button"
            onClick={onStop}
            title="停止命令"
            aria-label="停止命令"
            className="exec-cell-stop-button"
          >
            <Square size={13} fill="currentColor" aria-hidden="true" />
          </button>
        )}
      </div>
      {expanded && (
        <div className="exec-cell-expanded" role="region" aria-label="命令输出">
          <div className="exec-cell-output-toolbar">
            <span>Shell</span>
            <button type="button" className="cell-action-btn" aria-label={copied ? "已复制命令输出" : "复制命令输出"} title={copied ? "已复制" : "复制命令输出"}
              onClick={() => navigator.clipboard.writeText([`$ ${cell.command}`, stdout, stderr].filter(Boolean).join("\n")).then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1200);
              })}>
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          <pre className="exec-cell-output-pre">
            <span className="exec-cell-output-command">$ {cell.command}</span>
            {stdout && <span className="exec-cell-output-stdout">{stdout}</span>}
            {stderr && <span className="exec-cell-output-stderr">{stderr}</span>}
            {!hasOutput && <span className="exec-cell-no-output">无输出</span>}
          </pre>
          <div className="exec-cell-output-status" data-status={status}>
            <StatusIcon status={cell.status} size={13} spinningClassName="exec-cell-spin-icon" />
            <span>{statusLabel}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function commandTitle(status: ExecCellState["status"], background: boolean): string {
  if (status === "pending_approval") return "等待运行命令";
  if (status === "running") return "正在运行命令";
  if (background && status === "success") return "已启动后台命令";
  if (status === "partial") return "命令未完整结束";
  if (status === "cancelled") return "命令已取消";
  return "已运行命令";
}
