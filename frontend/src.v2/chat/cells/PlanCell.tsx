import type React from "react";
import type { PlanCellState } from "./cellTypes";
import { sendClientCommand } from "../../protocol/ws-outbox";

/**
 * PlanCell — inline execution plan display.
 *
 * States:
 * - proposed: step list with checkboxes + status "等待确认"
 * - approved:  step list, ready to execute
 * - executing: step list with progress indicators (☑/◉/☐)
 * - completed: all steps checked
 * - cancelled: greyed out
 */
export function PlanCell({ cell }: { cell: PlanCellState }) {
  const completedCount = cell.steps.filter(
    (s) => s.status === "completed",
  ).length;

  const statusLabels: Record<string, string> = {
    proposed: "等待确认",
    approved: "已确认",
    executing: "执行中",
    completed: "已完成",
    cancelled: "已取消",
  };

  return (
    <div style={planCellStyle}>
      <div style={headerStyle}>
        <span className="shrink-0" style={{ color: "var(--accent-primary)" }}>
          ●
        </span>
        <span className="truncate" style={titleStyle}>{cell.title}</span>
        <span className="shrink-0 font-mono" style={metaStyle}>
          {cell.steps.length} 个步骤 ·{" "}
          {statusLabels[cell.status] ?? cell.status}
          {cell.status === "executing"
            ? ` (${completedCount}/${cell.steps.length})`
            : ""}
        </span>
      </div>

      {cell.steps.length > 0 && (
        <div style={stepsStyle}>
          {cell.steps.map((step, i) => (
            <div key={step.id || i} style={stepRowStyle(step.status)}>
              <StepMark status={step.status} />
              <span className="truncate" style={stepTextStyle(step.status)}>{step.title}</span>
              {step.description && (
                <span style={stepDescStyle}>{step.description}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {cell.requiresApproval && cell.status === "proposed" && (
        <div style={actionsStyle}>
          <button
            type="button"
            style={primaryButtonStyle}
            onClick={() => {
              const planId = cell.planId || cell.id.replace("plan-", "").split("-")[0];
              sendClientCommand({
                type: "plan.edit",
                plan_id: planId,
                action: "accept",
                accept: true,
              } as any);
            }}
          >
            开始执行
          </button>
          <button
            type="button"
            style={secondaryButtonStyle}
            onClick={() => {
              const planId = cell.planId || cell.id.replace("plan-", "").split("-")[0];
              sendClientCommand({
                type: "plan.edit",
                plan_id: planId,
                action: "reject",
                regenerate: true,
              } as any);
            }}
          >
            调整计划
          </button>
        </div>
      )}
    </div>
  );
}

function StepMark({
  status,
}: {
  status: PlanCellState["steps"][number]["status"];
}) {
  switch (status) {
    case "completed":
      return (
        <span className="shrink-0" style={{ color: "var(--state-success)" }}>
          ☑
        </span>
      );
    case "in_progress":
      return (
        <span
          className="shrink-0"
          style={{
            color: "var(--accent-primary)",
            fontWeight: 700,
          }}
        >
          ◉
        </span>
      );
    case "blocked":
      return (
        <span className="shrink-0" style={{ color: "var(--state-danger)" }}>
          ⊘
        </span>
      );
    default:
      return (
        <span className="shrink-0" style={{ color: "var(--text-muted)" }}>
          ☐
        </span>
      );
  }
}

// ── Styles ──────────────────────────────────────────────────────────

const planCellStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
  padding: "8px 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
  background: "var(--surface-soft)",
  fontSize: "var(--text-sm, 13px)",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  fontWeight: 650,
  color: "var(--text-primary)",
  minWidth: 0,
};

const metaStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const stepsStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  paddingLeft: 22,
};

const stepRowStyle = (
  status: PlanCellState["steps"][number]["status"],
): React.CSSProperties => ({
  display: "grid",
  gridTemplateColumns: "16px minmax(0, 1fr)",
  gap: 8,
  alignItems: "center",
  fontSize: "var(--text-xs)",
  color:
    status === "completed"
      ? "var(--text-muted)"
      : status === "cancelled"
        ? "var(--text-muted)"
        : "var(--text-secondary)",
  textDecoration:
    status === "completed" || status === "cancelled"
      ? "line-through"
      : "none",
  opacity: status === "cancelled" ? 0.6 : 1,
});

const stepTextStyle = (
  _status: PlanCellState["steps"][number]["status"],
): React.CSSProperties => ({
});

const stepDescStyle: React.CSSProperties = {
  gridColumn: 2,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  marginTop: 1,
};

const actionsStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  paddingLeft: 22,
  marginTop: 4,
};

const primaryButtonStyle: React.CSSProperties = {
  minHeight: 26,
  padding: "0 12px",
  border: "1px solid var(--accent-primary)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const secondaryButtonStyle: React.CSSProperties = {
  minHeight: 26,
  padding: "0 12px",
  border: "1px solid var(--border-soft)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "transparent",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};
