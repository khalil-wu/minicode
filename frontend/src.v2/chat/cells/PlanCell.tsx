import { CheckCircle2, Circle, CircleDot, CircleSlash2, Play, RefreshCcw, XCircle } from "lucide-react";
import type { PlanCellState } from "./cellTypes";
import { sendClientCommand } from "../../protocol/ws-outbox";
import "./cells.css";

/**
 * PlanCell — inline execution plan display.
 *
 * States:
 * - proposed: step list with status "等待确认"
 * - approved:  step list, ready to execute
 * - executing: step list with progress indicators
 * - completed: all steps completed
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
    <div className={`plan-cell plan-cell-${cell.status}`}>
      <div className="plan-cell-header">
        <CircleDot size={14} className="plan-cell-status-icon" />
        <span className="plan-cell-title">{cell.title}</span>
        <span className="plan-cell-meta">
          {cell.steps.length} 个步骤 ·{" "}
          {statusLabels[cell.status] ?? cell.status}
          {cell.status === "executing"
            ? ` (${completedCount}/${cell.steps.length})`
            : ""}
        </span>
      </div>

      {cell.steps.length > 0 && (
        <div className="plan-cell-steps">
          {cell.steps.map((step, i) => (
            <div key={step.id || i} className={`plan-cell-step plan-cell-step-${step.status}`}>
              <StepMark status={step.status} />
              <span className="plan-cell-step-title">{step.title}</span>
              {step.description && (
                <span className="plan-cell-step-description">{step.description}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {cell.requiresApproval && cell.status === "proposed" && (
        <div className="plan-cell-actions">
          <button
            type="button"
            className="plan-cell-action plan-cell-action-primary"
            onClick={() => {
              const planId = cell.planId || cell.id.replace("plan-", "").split("-")[0];
              sendClientCommand({
                type: "plan.edit",
                plan_id: planId,
                action: "accept",
                steps: protocolPlanSteps(cell),
                current_step: currentPlanStepIndex(cell),
              });
            }}
          >
            <Play size={13} />
            开始执行
          </button>
          <button
            type="button"
            className="plan-cell-action plan-cell-action-secondary"
            onClick={() => {
              const planId = cell.planId || cell.id.replace("plan-", "").split("-")[0];
              sendClientCommand({
                type: "plan.edit",
                plan_id: planId,
                action: "reject",
                steps: protocolPlanSteps(cell),
                current_step: currentPlanStepIndex(cell),
              });
            }}
          >
            <RefreshCcw size={13} />
            调整计划
          </button>
        </div>
      )}
    </div>
  );
}

function protocolPlanSteps(cell: PlanCellState) {
  return cell.steps.map((step) => ({
    id: step.id,
    title: step.title,
    detail: step.description,
    status: protocolStepStatus(step.status),
  }));
}

function protocolStepStatus(status: PlanCellState["steps"][number]["status"]) {
  switch (status) {
    case "completed":
      return "done" as const;
    case "in_progress":
      return "running" as const;
    case "blocked":
      return "failed" as const;
    case "cancelled":
      return "skipped" as const;
    case "pending":
    default:
      return "pending" as const;
  }
}

function currentPlanStepIndex(cell: PlanCellState): number {
  const active = cell.steps.findIndex((step) => step.status === "in_progress");
  if (active >= 0) return active;
  const next = cell.steps.findIndex((step) => step.status !== "completed");
  return next >= 0 ? next : cell.steps.length;
}

function StepMark({
  status,
}: {
  status: PlanCellState["steps"][number]["status"];
}) {
  switch (status) {
    case "completed":
      return (
        <CheckCircle2 size={14} className="plan-cell-step-icon plan-cell-step-icon-completed" />
      );
    case "in_progress":
      return (
        <CircleDot size={14} className="plan-cell-step-icon plan-cell-step-icon-progress" />
      );
    case "blocked":
      return (
        <CircleSlash2 size={14} className="plan-cell-step-icon plan-cell-step-icon-blocked" />
      );
    case "cancelled":
      return (
        <XCircle size={14} className="plan-cell-step-icon plan-cell-step-icon-cancelled" />
      );
    default:
      return (
        <Circle size={14} className="plan-cell-step-icon plan-cell-step-icon-pending" />
      );
  }
}
