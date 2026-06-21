import type { PlanState } from "../stores/types";

export function hasVisiblePlanSteps(plan: PlanState | null | undefined): plan is PlanState {
  return Boolean(plan?.steps?.some((step) => step.title.trim()));
}
