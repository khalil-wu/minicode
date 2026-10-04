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
  const hasContextUsage = Boolean(contextUsage && contextUsage.limit > 0);
  const hasBudgetUsage = !hasContextUsage && (
    buckets.some((bucket) => bucket.limit > 0)
    || Number(totalBudgetPercent) > 0
  );
  const usageKnown = hasContextUsage || hasBudgetUsage;
  const contextPercent = hasContextUsage && contextUsage
    ? contextUsage.used / contextUsage.limit
    : 0;
  // cc's ring shows context_window.used_percentage; the token-budget share
  // is a different quantity and only stands in when context is unknown.
  const percent = clampPercent(hasContextUsage ? contextPercent : (totalBudgetPercent ?? 0));
  const label = usageKnown ? `${Math.round(percent * 100)}%` : "--";
  const usageLabel = hasContextUsage ? "上下文" : hasBudgetUsage ? "任务预算" : "用量暂无数据";
  const color = "var(--accent-primary)";
  const title = buildTitle({ buckets, contextUsage, percent, usageKnown });

  return (
    <div title={title} style={shellStyle}>
      <span
        aria-label={`${usageLabel}${usageKnown ? ` ${label}` : ""}`}
        aria-valuetext={usageKnown ? `${usageLabel}已使用 ${label}` : "暂无上下文或任务预算数据"}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={usageKnown ? Math.round(percent * 100) : undefined}
        style={{
          ...ringStyle,
          background: `conic-gradient(${color} ${Math.round(percent * 360)}deg, var(--surface-soft) 0deg)`,
        }}
      >
        <span style={ringInnerStyle} />
      </span>
      {usageKnown && <span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{usageLabel}</span>}
      <span style={{ ...labelStyle, color: usageKnown ? color : "var(--text-muted)" }}>{usageKnown ? label : "暂无数据"}</span>
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
  usageKnown,
}: {
  buckets: BudgetBucket[];
  contextUsage: ContextUsage | null;
  percent: number;
  usageKnown: boolean;
}) => {
  const hasContext = Boolean(contextUsage && contextUsage.limit > 0);
  const lines = [usageKnown ? `${hasContext ? "上下文" : "任务预算"}：${Math.round(percent * 100)}%` : "暂无上下文或任务预算数据"];
  if (contextUsage && contextUsage.limit > 0) {
    lines.push(`当前上下文：${formatCount(contextUsage.used)} / ${formatCount(contextUsage.limit)} tokens`);
  }
  for (const bucket of buckets.slice(0, 6)) {
    const pct = bucket.limit > 0 ? Math.round((bucket.used / bucket.limit) * 100) : 0;
    lines.push(`任务预算 · ${bucket.name}：${bucket.limit > 0 ? `${pct}% (${formatCount(bucket.used)} / ${formatCount(bucket.limit)})` : `${formatCount(bucket.used)} tokens，未设置上限`}`);
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
  fontWeight: "var(--fw-semibold)",
};
