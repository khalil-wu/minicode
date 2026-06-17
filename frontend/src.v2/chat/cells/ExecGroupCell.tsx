import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import type { ExecCellState } from "./cellTypes";
import { ExecCell } from "./ExecCell";
import "./cells.css";

export function ExecGroupCell({
  cells,
  isActive = false,
  onStop,
}: {
  cells: ExecCellState[];
  isActive?: boolean;
  onStop?: () => void;
}) {
  const status = execGroupStatus(cells, isActive);
  const [expanded, setExpanded] = useState(false);
  const title = useMemo(() => execGroupTitle(cells, status), [cells, status]);

  useEffect(() => {
    setExpanded(false);
  }, [cells, status]);

  if (cells.length === 0) return null;

  return (
    <section className={`exec-group-cell exec-group-cell-${status}`}>
      <button
        type="button"
        aria-label={expanded ? "Collapse command details" : "Expand command details"}
        aria-expanded={expanded}
        className="exec-group-header"
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <ExecGroupStatusIcon status={status} />
        <span className="exec-group-title">{title}</span>
      </button>
      {expanded && (
        <div className="exec-group-body">
          {cells.map((cell) => (
            <ExecCell
              key={cell.id}
              cell={{ ...cell, collapsed: true }}
              isActive={isActive && cell.status === "running"}
              onStop={onStop}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function execGroupStatus(cells: ExecCellState[], isActive: boolean): "running" | "done" | "failed" {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "cancelled")) return "failed";
  if (isActive || cells.some((cell) => cell.status === "running" || cell.status === "pending_approval")) {
    return "running";
  }
  return "done";
}

function execGroupTitle(cells: ExecCellState[], status: "running" | "done" | "failed"): string {
  const count = Math.max(1, cells.length);
  if (status === "running") return `正在运行 ${count} 条命令`;
  return `已运行 ${count} 条命令`;
}

function ExecGroupStatusIcon({ status }: { status: "running" | "done" | "failed" }) {
  if (status === "running") {
    return <Loader2 size={14} className="spinner exec-group-status-running" />;
  }
  if (status === "failed") {
    return <TriangleAlert size={14} className="exec-group-status-failed" />;
  }
  return <CheckCircle2 size={14} className="exec-group-status-done" />;
}
