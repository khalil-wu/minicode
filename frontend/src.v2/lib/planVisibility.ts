import type { TurnPlanStep } from "../protocol/events";
import type { PlanState, TodoItem } from "../stores/types";

export type PlanProgressStatus = "pending" | "running" | "completed" | "failed";

export function hasVisiblePlanSteps(plan: PlanState | null | undefined): plan is PlanState {
  return Boolean(plan?.plan?.length);
}

export function isPlanExecuting(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan) && plan.plan.some((step) => step.status === "in_progress");
}

export function shouldAutoFocusPlan(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan);
}

export function shouldSurfacePlanProgress(plan: PlanState | null | undefined): plan is PlanState {
  return hasVisiblePlanSteps(plan);
}

export function visiblePlanStepStatus(step: TurnPlanStep): TurnPlanStep["status"] {
  return step.status;
}

export function isVisiblePlanStepActive(step: TurnPlanStep): boolean {
  return step.status === "in_progress";
}

export function planStepProgressStatus(
  step: TurnPlanStep,
  isLive: boolean,
): PlanProgressStatus {
  switch (visiblePlanStepStatus(step)) {
    case "completed":
      return "completed";
    case "in_progress":
      return isLive ? "running" : "pending";
    case "pending":
    default:
      return "pending";
  }
}

export function planStepTodoStatus(step: TurnPlanStep): TodoItem["status"] {
  switch (visiblePlanStepStatus(step)) {
    case "completed":
      return "completed";
    case "in_progress":
      return "in_progress";
    case "pending":
    default:
      return "pending";
  }
}
