import type { CSSProperties } from "react";
import type { BudgetBucket, ContextUsage } from "../stores/types";

export const UsageRing = ({
  buckets,
  contextUsage,
  totalBudgetPercent,
}: {
  buckets: BudgetBucket[];
  contextUsage: ContextUsage | null;
  totalBudgetPercent: number;
}) => {
  const contextPercent = contextUsage && contextUsage.limit > 0
    ? contextUsage.used / contextUsage.limit
    : 0;
  const percent = clampPercent((totalBudgetPercent ?? 0) > 0 ? totalBudgetPercent : (contextPercent ?? 0));
  const label = percent > 0 ? `${Math.round(percent * 100)}%` : "--";
  const color = "var(--accent-primary)";
  const title = buildTitle({ buckets, contextUsage, percent });

  return (
    <div title={title} style={shellStyle}>
      <span
        aria-label={`Context usage ${label}`}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percent * 100)}
        style={{
          ...ringStyle,
          background: `conic-gradient(${color} ${Math.round(percent * 360)}deg, var(--surface-soft) 0deg)`,
        }}
      >
        <span style={ringInnerStyle} />
      </span>
      <span style={{ ...labelStyle, color }}>{label}</span>
    </div>
  );
};

const clampPercent = (value: number): number => {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
};

const formatCount = (value: number): string => {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(Math.round(value));
};

const buildTitle = ({
  buckets,
  contextUsage,
  percent,
}: {
  buckets: BudgetBucket[];
  contextUsage: ContextUsage | null;
  percent: number;
}) => {
  const lines = [`Context budget: ${Math.round(percent * 100)}%`];
  if (contextUsage && contextUsage.limit > 0) {
    lines.push(`${formatCount(contextUsage.used)} / ${formatCount(contextUsage.limit)} tokens`);
  }
  for (const bucket of buckets.slice(0, 6)) {
    const pct = bucket.limit > 0 ? Math.round((bucket.used / bucket.limit) * 100) : 0;
    lines.push(`${bucket.name}: ${pct}% (${formatCount(bucket.used)} / ${formatCount(bucket.limit)})`);
  }
  return lines.join("\n");
};

const shellStyle: CSSProperties = {
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  padding: "1px 7px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  cursor: "default",
};

const ringStyle: CSSProperties = {
  width: 14,
  height: 14,
  borderRadius: "50%",
  display: "inline-grid",
  placeItems: "center",
  flexShrink: 0,
};

const ringInnerStyle: CSSProperties = {
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: "var(--surface-page)",
};

const labelStyle: CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  fontWeight: 600,
};
