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
    cell.status === "failed" ||
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
      ? "waiting"
      : cell.status === "running"
        ? "running"
        : cell.status === "success"
          ? "passed"
          : cell.status === "failed"
            ? "failed"
            : "cancelled";

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
          <span className="exec-cell-command">{cell.command}</span>
          <span className="exec-cell-meta">
            {statusLabel}
            {duration ? ` - ${duration}` : ""}
            {cell.exitCode != null && cell.status === "failed"
              ? ` - exit ${cell.exitCode}`
              : ""}
          </span>
        </button>
        {cell.status === "running" && onStop && (
          <button
            type="button"
            onClick={stopCommand}
            title="Stop command"
            aria-label="Stop command"
            className="exec-cell-stop-button"
          >
            <StopCircle size={12} />
            Stop
          </button>
        )}
      </div>

      {expanded && hasOutput && (
        <div className="exec-cell-output-stack">
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
          <button
            type="button"
            onClick={handleCopy}
            title={copied ? "Copied" : "Copy output"}
            className="exec-cell-copy-button"
          >
            <Copy size={11} />
          </button>
        </div>
      )}

      {expanded && !hasOutput && cell.status !== "running" && (
        <div className="exec-cell-empty-output">
          {cell.status === "cancelled" ? "Cancelled before output" : "No output"}
        </div>
      )}

      {expanded && cell.status === "running" && !hasOutput && (
        <div className="exec-cell-waiting-output">
          Waiting for command output...
        </div>
      )}
    </div>
  );
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
