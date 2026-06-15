import type React from "react";
import { formatModelLabel } from "../../lib/model-label";
import { useAppStore } from "../../stores";

/**
 * BottomStatusBar shows streaming or plan execution state.
 */
export function BottomStatusBar() {
  const isStreaming = useAppStore((s) => s.isStreaming);
  const permissionMode = useAppStore((s) => s.permissionMode);
  const plan = useAppStore((s) => s.plan);
  const currentModel = useAppStore((s) => s.currentModel);

  if (!isStreaming && !plan) return null;

  const statusText = plan && plan.status === "executing"
    ? `Executing plan - Step ${plan.currentStep + 1}/${plan.steps.length}`
    : isStreaming
      ? "Processing"
      : "";

  const modeLabel =
    permissionMode === "bypass"
      ? "Full access"
      : permissionMode === "ask_permissions"
        ? "Ask"
        : "Auto";

  const modelLabel = formatModelLabel(currentModel, "");

  return (
    <div style={barStyle}>
      <div style={leftStyle}>
        {isStreaming && (
          <span style={dotStyle}>
            <span className="thinking-mini-dot" />
          </span>
        )}
        <span style={statusTextStyle}>{statusText}</span>
        {modeLabel && <span style={badgeStyle}>{modeLabel}</span>}
      </div>
      <div style={rightStyle}>
        {modelLabel && (
          <span style={modelStyle} title={currentModel || undefined}>{modelLabel}</span>
        )}
        <span style={hintStyle}>Esc to interrupt</span>
      </div>
    </div>
  );
}

const barStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  height: 28,
  padding: "0 16px",
  borderTop: "1px solid var(--border-subtle, rgba(255,255,255,0.06))",
  background: "var(--surface-page, rgba(0,0,0,0.1))",
  fontSize: "var(--text-xs, 12px)",
  color: "var(--text-muted)",
  flexShrink: 0,
};

const leftStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const rightStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
};

const dotStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
};

const statusTextStyle: React.CSSProperties = {
  fontWeight: 600,
  color: "var(--text-secondary)",
};

const badgeStyle: React.CSSProperties = {
  padding: "1px 6px",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs, 11px)",
};

const modelStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs, 11px)",
};

const hintStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs, 11px)",
  opacity: 0.7,
};
