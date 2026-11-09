import { useI18n } from "../../i18n";
import { CheckCircle2, CircleAlert, Info, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { StatusNoticeCellState } from "./cellTypes";

const TONE_STYLES: Record<
  StatusNoticeCellState["tone"],
  { color: string; icon: LucideIcon }
> = {
  info: {
    color: "var(--text-muted)",
    icon: Info,
  },
  warning: {
    color: "var(--state-warning)",
    icon: TriangleAlert,
  },
  success: {
    color: "var(--text-muted)",
    icon: CheckCircle2,
  },
  danger: {
    color: "var(--state-danger)",
    icon: CircleAlert,
  },
};

export function StatusNoticeCell({
  cell,
}: {
  cell: StatusNoticeCellState;
}) {
  const { t: tr } = useI18n();
  const s = TONE_STYLES[cell.tone];
  const NoticeIcon = s.icon;

  return (
    <div
      className="status-notice-cell"
      data-tone={cell.tone}
      data-processing={cell.processing || undefined}
      role={cell.tone === "danger" ? "alert" : cell.processing ? "status" : undefined}
      style={{
        display: "inline-flex",
        alignItems: "flex-start",
        gap: 8,
        maxWidth: "100%",
        padding: "2px 0",
        fontFamily: "var(--font-ui)",
        fontSize: "var(--text-xs)",
        color: s.color,
      }}
    >
      <NoticeIcon size={14} strokeWidth={1.75} aria-hidden="true" style={{ flexShrink: 0, marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>
        <div className="status-notice-title" style={{ fontWeight: "var(--fw-regular)" }}>{cell.title}</div>
        {cell.message && (
          <div style={{
            marginTop: 2,
            opacity: 0.85,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            fontFamily: cell.title === tr("命令执行记录") ? "var(--font-mono)" : undefined,
          }}>{cell.message}</div>
        )}
      </div>
    </div>
  );
}
