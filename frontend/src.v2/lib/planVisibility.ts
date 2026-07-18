import type { PlanState } from "../stores/types";
import type { PlanStep } from "../protocol/events";
import type { TodoItem } from "../stores/types";

export function hasVisiblePlanSteps(plan: PlanState | null | undefined): plan is PlanState {
  return Boolean(plan?.steps?.some((step) => step.title.trim()));
}

/**
 * A plan only appears as live progress once it is actually being executed.
 * Draft proposals and cancelled plans are workbench artifacts, not progress.
 */
export function shouldSurfacePlanProgress(plan: PlanState | null | undefined): plan is PlanState {
  if (!hasVisiblePlanSteps(plan)) return false;
  return plan.status !== "draft" && plan.status !== "cancelled";
}

/** Map a plan step onto the todo status vocabulary used by progress UI. */
export function planStepTodoStatus(plan: PlanState, step: PlanStep): TodoItem["status"] {
  switch (step.status) {
    case "done":
    case "skipped":
      return "completed";
    case "failed":
      return "blocked";
    case "running":
      // A "running" step in a plan that is not executing is only queued work.
      return plan.status === "executing" ? "in_progress" : "pending";
    default:
      return "pending";
  }
}
