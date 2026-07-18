import { memo } from "react";
import { useAppStore } from "../../stores";
import { getWebSocket } from "../../hooks/useWebSocket";

/**
 * ContextBudgetIndicator — shows real-time token/context usage.
 *
 * Display format: [◐ 45K / 200K]
 * - Green when < 70% used
 * - Yellow when 70-90% used
 * - Red when > 90% used
 *
 * Clicking requests detailed usage from the backend.
 */
export const ContextBudgetIndicator = memo(function ContextBudgetIndicator() {
  const contextUsage = useAppStore((s) => s.contextUsage);
  const isStreaming = useAppStore((s) => s.isStreaming);

  if (!contextUsage) return null;

  const { used, limit } = contextUsage;
  const usedK = Math.round(used / 1000);
  const limitK = Math.round(limit / 1000);
  const percentage = limit > 0 ? (used / limit) * 100 : 0;

  // Determine color based on usage
  const color =
    percentage > 90
      ? "var(--state-danger)"
      : percentage > 70
        ? "var(--state-warning)"
        : "var(--state-success)";

  // Icon based on percentage
  const icon = percentage > 90 ? "◉" : percentage > 50 ? "◐" : "○";

  const handleClick = () => {
    getWebSocket()?.send({ type: "session.usage.inspect", source: "usage_indicator" });
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      className="context-budget-indicator"
      style={{ color }}
      title={`Context usage: ${used.toLocaleString()} / ${limit.toLocaleString()} tokens (${percentage.toFixed(1)}%)\n点击查看详情`}
      data-streaming={isStreaming}
    >
      <span className="context-budget-icon">{icon}</span>
      <span className="context-budget-text">
        {usedK}K / {limitK}K
      </span>
    </button>
  );
});
