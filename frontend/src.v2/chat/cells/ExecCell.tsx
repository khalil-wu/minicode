import { Check, ChevronDown, ChevronRight, Copy, Square, TerminalSquare } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ExecCellState } from "./cellTypes";
import { StatusIcon } from "../../components/icons";
import {
  cellStatusTone,
  execCellStatus,
  formatCellDuration,
  isRunningCellStatus,
} from "./cellStatus";
import "./cells.css";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { toolCleanupNotice } from "../../lib/tool-call-reducer";

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
  const title = commandTitle(cell.status, Boolean(cell.background));
  const duration = cell.background ? "" : formatCellDuration(cell.durationMs);
  const outputMeta = [cell.exitCode != null ? `exit ${cell.exitCode}` : "", duration].filter(Boolean).join(" · ");
  const running = isRunningCellStatus(status);
  const shouldAutoExpand = !cell.collapsed || cell.status === "failed" || cell.status === "partial";
  const [expansionPreference, setExpanded] = useState(shouldAutoExpand);
  const expanded = useTranscriptSearch() || expansionPreference;
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
  const cleanupNotice = toolCleanupNotice(cell.cleanupReceipt);

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
          aria-label={`${expanded ? "收起" : "展开"}命令详情：${title} ${cell.command}`}
          onClick={() => {
            userToggled.current = true;
            setExpanded((value) => !value);
          }}
        >
          <span className={`exec-cell-status-badge exec-cell-status-${statusColor}`}>
            {cell.status === "success" || cell.status === "running" ? (
              <TerminalSquare size={15} aria-hidden="true" />
            ) : (
              <span aria-hidden="true"><StatusIcon status={cell.status} size={15} /></span>
            )}
          </span>
          <span className="exec-cell-title">{title}</span>
          <span className="exec-cell-command-preview" title={cell.command}>{cell.command}</span>
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
      {cleanupNotice && <div className="exec-cell-meta" role="status">{cleanupNotice}</div>}
      {expanded && (
        <div className="exec-cell-expanded" role="region" aria-label="命令输出">
          <div className="exec-cell-output-toolbar">
            <span>Shell</span>
            {cell.cwd && <span className="exec-cell-meta" title={cell.cwd}>{cell.cwd}</span>}
            {outputMeta && <span className="exec-cell-meta">{outputMeta}</span>}
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
        </div>
      )}
    </div>
  );
}

function commandTitle(status: ExecCellState["status"], background: boolean): string {
  if (status === "pending_approval") return "Run · Awaiting approval";
  if (status === "running") return "Running";
  if (background && status === "success") return "Run · Started in background";
  if (status === "partial") return "Run · Partial";
  if (status === "cancelled") return "Run · Cancelled";
  if (status === "failed") return "Run · Failed";
  return "Run";
}
