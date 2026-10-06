import { useId, type CSSProperties } from "react";
import type { BudgetBucket, ContextLedgerCategory, ContextUsage } from "../stores/types";
import "./UsageRing.css";

const categoryColors: Record<ContextLedgerCategory, string> = {
  system_runtime: "var(--agent-identity-violet)",
  guidelines: "var(--agent-identity-blue)",
  skills: "var(--agent-identity-rose)",
  files_attachments: "var(--agent-identity-amber)",
  history: "var(--agent-identity-green)",
  tool_results: "var(--agent-identity-teal)",
  memory: "var(--state-warning)",
  compaction_summaries: "var(--state-success)",
};
const bucketColors = ["var(--agent-identity-violet)", "var(--agent-identity-green)", "var(--agent-identity-teal)", "var(--agent-identity-rose)", "var(--agent-identity-amber)", "var(--agent-identity-blue)"];

export const UsageRing = ({
  buckets,
  contextUsage,
  totalBudgetPercent,
}: {
  buckets: BudgetBucket[];
  contextUsage: ContextUsage | null;
  totalBudgetPercent: number;
}) => {
  const detailsId = useId();
  const hasContextUsage = Boolean(contextUsage && contextUsage.limit > 0);
  const hasBudgetUsage = !hasContextUsage && (
    buckets.some((bucket) => bucket.limit > 0)
    || Number(totalBudgetPercent) > 0
  );
  const usageKnown = hasContextUsage || hasBudgetUsage;
  const contextPercent = hasContextUsage && contextUsage
    ? contextUsage.used / contextUsage.limit
    : 0;
  // budget_update buckets describe prompt use, not a task spending limit.
  const percent = clampPercent(hasContextUsage ? contextPercent : (totalBudgetPercent ?? 0));
  const label = usageKnown ? `${Math.round(percent * 100)}%` : "暂无数据";
  const ledger = contextUsage?.ledger;
  const segments = (ledger ? ledger.entries.map((entry) => ({
    key: entry.category, label: entry.label, tokens: entry.estimated_tokens, color: categoryColors[entry.category],
  })) : buckets.map((bucket, index) => ({
    key: bucket.name, label: bucket.name, tokens: bucket.used, color: bucketColors[index % bucketColors.length],
  }))).filter((segment) => segment.tokens > 0);
  const segmentTotal = segments.reduce((sum, segment) => sum + segment.tokens, 0);

  return (
    <span className="mc-usage-ring">
      <span
        aria-label={usageKnown ? `会话用量 ${label}` : "用量暂无数据"}
        aria-valuetext={usageKnown ? `已使用 ${label}` : "暂无用量数据"}
        aria-describedby={detailsId}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={usageKnown ? Math.round(percent * 100) : undefined}
        className="mc-usage-ring-meter"
        style={{ background: `conic-gradient(var(--text-secondary) ${Math.round(percent * 360)}deg, var(--surface-soft) 0deg)` }}
      >
        <span className="mc-usage-ring-inner" />
      </span>
      <span className="mc-usage-ring-label">{label}</span>
      <span id={detailsId} className="mc-usage-details" role="tooltip">
        <span className="mc-usage-details-heading"><strong>用量</strong><span>{label}</span></span>
        {hasContextUsage && contextUsage && <span className="mc-usage-details-total">{formatCount(contextUsage.used)} / {formatCount(contextUsage.limit)} tokens</span>}
        {segments.length > 0 && <>
          <span className="mc-usage-details-bar" aria-hidden="true">
            {segments.map((segment) => <span key={segment.key} style={{ width: `${segment.tokens / segmentTotal * 100}%`, background: segment.color }} />)}
          </span>
          <span className="mc-usage-details-caption">{ledger ? "组成 · 后端估算" : "组成"}</span>
          <span className="mc-usage-details-segments">
            {segments.map((segment) => <span key={segment.key} className="mc-usage-details-segment" style={{ "--usage-segment-color": segment.color } as CSSProperties}>
              <span className="mc-usage-details-dot" aria-hidden="true" />
              <span>{segment.label}</span>
              <span>{formatCount(segment.tokens)} tokens</span>
            </span>)}
          </span>
        </>}
      </span>
    </span>
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
