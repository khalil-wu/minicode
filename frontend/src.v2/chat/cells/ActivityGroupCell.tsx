import { useMemo, useState } from "react";
import type React from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { ActivityCell } from "./ActivityCell";
import "./cells.css";

export function ActivityGroupCell({
  cells,
  isActive = false,
}: {
  cells: ActivityCellState[];
  isActive?: boolean;
}) {
  const status = groupStatus(cells, isActive);
  const [expanded, setExpanded] = useState(status !== "done");
  const summaryItems = useMemo(() => buildSummaryItems(cells), [cells]);

  // Calculate progress for batch operations
  const progress = useMemo(() => {
    const completed = cells.filter(c => c.status === "completed").length;
    const failed = cells.filter(c => c.status === "failed" || c.status === "interrupted").length;
    const running = cells.filter(c => c.status === "running").length;
    const total = cells.length;
    return { completed, failed, running, total };
  }, [cells]);

  if (cells.length === 0) return null;

  const title =
    status === "running"
      ? "Working"
      : status === "failed"
        ? "Stopped"
        : "Completed";

  const groupStateClass = `activity-group-cell-${status}`;

  return (
    <section className={`activity-group-cell ${groupStateClass}`}>
      <button
        type="button"
        aria-label={expanded ? "Collapse activity details" : "Expand activity details"}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="activity-group-header"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <StatusIcon status={status} />
        <span className="activity-group-title">{title}</span>

        {/* Progress counter for batch operations */}
        {cells.length > 1 && (
          <span className="activity-group-progress-counter">
            ({progress.completed + progress.failed}/{progress.total})
          </span>
        )}

        {/* 折叠时：running显示实时进度，done/failed显示汇总 */}
        {!expanded && status === "running" && cells.length > 1 ? (
          <span className="activity-group-inline-progress">
            {cells.filter(c => c.status === "running").slice(0, 2).map((cell) => (
              <span key={cell.id} className="activity-group-inline-progress-item">
                {extractShortLabel(cell)}
              </span>
            ))}
          </span>
        ) : !expanded ? (
          <span className="activity-group-summary">
            {summaryItems.map((item) => (
              <span key={item} className="activity-group-pill">
                {item}
              </span>
            ))}
          </span>
        ) : null}
      </button>
      {expanded && (
        <div className="activity-group-body">
          {cells.map((cell) => (
            <ActivityCell
              key={cell.id}
              cell={{ ...cell, collapsed: false }}
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
    return <Loader2 size={14} className="spinner" style={{ color: "var(--accent-primary)" }} />;
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

function buildSummaryItems(cells: ActivityCellState[]): string[] {
  const seen = new Set<string>();
  const items: string[] = [];
  for (const cell of cells) {
    const item = summaryForCell(cell);
    if (!item || seen.has(item)) continue;
    seen.add(item);
    items.push(item);
  }
  return items;
}

function summaryForCell(cell: ActivityCellState): string {
  const subtitle = cell.subtitle ?? "";
  const count = String(Math.max(1, cell.toolCallRecords?.length ?? Number(firstNumber(cell.subtitle || cell.title) || 1)));
  switch (cell.activityKind) {
    case "reasoning":
    case "planning":
    case "processNote":
    case "providerReasoning":
      return cell.status === "running" ? "thinking" : "reasoning";
    case "progress":
      return "progress";
    case "webSearch":
      if (/page|web|read/i.test(`${cell.title} ${subtitle}`)) {
        return count ? `pages ${count}` : "read web pages";
      }
      return count ? `searches ${count}` : "searched web";
    case "fileRead":
      return count ? `files ${count}` : cell.title;
    case "workspaceSearch":
      return count ? `searches ${count}` : cell.title;
    case "commandExecution":
      return count ? `commands ${count}` : cell.title;
    case "fileChange":
      return count ? `changed ${count}` : cell.title;
    case "mcpToolCall":
      return count ? `MCP ${count}` : cell.title;
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
