import type React from "react";
import type { StatusNoticeCellState } from "./cellTypes";

const TONE_STYLES: Record<
  StatusNoticeCellState["tone"],
  { border: string; bg: string; color: string; icon: string }
> = {
  info: {
    border: "var(--state-info, #4a9eff)",
    bg: "color-mix(in oklch, var(--state-info, #4a9eff) 8%, var(--surface-soft))",
    color: "var(--state-info, #4a9eff)",
    icon: "ℹ",
  },
  warning: {
    border: "var(--state-warning, #f0a030)",
    bg: "color-mix(in oklch, var(--state-warning, #f0a030) 8%, var(--surface-soft))",
    color: "var(--state-warning, #f0a030)",
    icon: "⚠",
  },
  success: {
    border: "var(--state-success, #2ea043)",
    bg: "color-mix(in oklch, var(--state-success, #2ea043) 8%, var(--surface-soft))",
    color: "var(--state-success, #2ea043)",
    icon: "✓",
  },
  danger: {
    border: "var(--state-danger, #f85149)",
    bg: "color-mix(in oklch, var(--state-danger, #f85149) 8%, var(--surface-soft))",
    color: "var(--state-danger, #f85149)",
    icon: "✕",
  },
};

export function StatusNoticeCell({
  cell,
}: {
  cell: StatusNoticeCellState;
}) {
  const s = TONE_STYLES[cell.tone] ?? TONE_STYLES.info;

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
      <span style={{ flexShrink: 0, fontWeight: 700 }}>{s.icon}</span>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 650 }}>{cell.title}</div>
        {cell.message && (
          <div style={{ marginTop: 2, opacity: 0.85 }}>{cell.message}</div>
        )}
      </div>
    </div>
  );
}
