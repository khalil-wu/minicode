import type { PlanStep } from "../protocol/events";
import type { PlanState, TodoItem } from "../stores/types";

export type PlanProgressStatus = "pending" | "running" | "completed" | "failed";

export function hasVisiblePlanSteps(plan: PlanState | null | undefined): plan is PlanState {
  return Boolean(plan?.steps?.some((step) => step.title.trim()));
}

export function isPlanExecuting(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan) && plan.status === "executing";
}

export function shouldAutoFocusPlan(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan) && (plan.status === "accepted" || plan.status === "executing");
}

export function shouldSurfacePlanProgress(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan) && plan.status !== "draft" && plan.status !== "cancelled";
}

export function visiblePlanStepStatus(plan: PlanState, step: PlanStep): PlanStep["status"] {
  if (plan.status === "executing") return step.status;
  if (step.status === "done" || step.status === "skipped" || step.status === "failed") return step.status;
  return "pending";
}

export function isVisiblePlanStepActive(plan: PlanState, step: PlanStep, index: number): boolean {
  return plan.status === "executing" && index === plan.currentStep && step.status === "running";
}

export function planStepProgressStatus(
  plan: PlanState,
  step: PlanStep,
  isLive: boolean,
): PlanProgressStatus {
  switch (visiblePlanStepStatus(plan, step)) {
    case "done":
    case "skipped":
      return "completed";
    case "failed":
      return "failed";
    case "running":
      return isLive ? "running" : "pending";
    case "pending":
    default:
      return "pending";
  }
}

export function planStepTodoStatus(plan: PlanState, step: PlanStep): TodoItem["status"] {
  switch (visiblePlanStepStatus(plan, step)) {
    case "done":
    case "skipped":
      return "completed";
    case "failed":
      return "blocked";
    case "running":
      return "in_progress";
    case "pending":
    default:
      return "pending";
  }
}
