import { CheckCircle2, CircleAlert, Info, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { StatusNoticeCellState } from "./cellTypes";

const TONE_STYLES: Record<
  StatusNoticeCellState["tone"],
  { border: string; bg: string; color: string; icon: LucideIcon }
> = {
  info: {
    border: "var(--state-info)",
    bg: "color-mix(in oklch, var(--state-info) 8%, var(--surface-soft))",
    color: "var(--state-info)",
    icon: Info,
  },
  warning: {
    border: "var(--state-warning)",
    bg: "color-mix(in oklch, var(--state-warning) 8%, var(--surface-soft))",
    color: "var(--state-warning)",
    icon: TriangleAlert,
  },
  success: {
    border: "var(--state-success)",
    bg: "color-mix(in oklch, var(--state-success) 8%, var(--surface-soft))",
    color: "var(--state-success)",
    icon: CheckCircle2,
  },
  danger: {
    border: "var(--state-danger)",
    bg: "color-mix(in oklch, var(--state-danger) 8%, var(--surface-soft))",
    color: "var(--state-danger)",
    icon: CircleAlert,
  },
};

export function StatusNoticeCell({
  cell,
}: {
  cell: StatusNoticeCellState;
}) {
  const s = TONE_STYLES[cell.tone] ?? TONE_STYLES.info;
  const NoticeIcon = s.icon;

  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "flex-start",
        gap: 8,
        maxWidth: "100%",
        padding: "7px 10px",
        background: s.bg,
        border: `1px solid ${s.border}`,
        borderRadius: "var(--radius-sm, 6px)",
        fontSize: "var(--text-xs)",
        color: s.color,
      }}
    >
      <NoticeIcon size={14} strokeWidth={1.75} aria-hidden="true" style={{ flexShrink: 0, marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: "var(--fw-semibold)" }}>{cell.title}</div>
        {cell.message && (
          <div style={{
            marginTop: 2,
            opacity: 0.85,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            fontFamily: cell.title === "命令执行记录" ? "var(--font-mono)" : undefined,
          }}>{cell.message}</div>
        )}
      </div>
    </div>
  );
}
