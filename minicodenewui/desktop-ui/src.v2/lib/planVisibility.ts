import type { TurnPlanStep } from "../protocol/events";
import type { PlanState } from "../stores/types";

export type PlanProgressStatus = "pending" | "running" | "completed" | "failed";

export function hasVisiblePlanSteps(plan: PlanState | null | undefined): plan is PlanState {
  return Boolean(plan?.plan?.length);
}

export function planStepProgressStatus(
  step: TurnPlanStep,
  isLive: boolean,
): PlanProgressStatus {
  switch (step.status) {
    case "completed":
      return "completed";
    case "in_progress":
      return isLive ? "running" : "pending";
    case "pending":
    default:
      return "pending";
  }
}
