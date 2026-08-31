import { describe, expect, it } from "vitest";
import {
  isPlanExecuting,
  isVisiblePlanStepActive,
  planStepProgressStatus,
  planStepTodoStatus,
  shouldAutoFocusPlan,
  shouldSurfacePlanProgress,
  visiblePlanStepStatus,
} from "./planVisibility";
import type { PlanState } from "../stores/types";

const plan = (statuses: PlanState["plan"][number]["status"][]): PlanState => ({
  threadId: "thread-1",
  turnId: "turn-1",
  plan: statuses.map((status, index) => ({
    step: `Step ${index + 1}`,
    status,
  })),
});

describe("plan lifecycle visibility", () => {
  it("surfaces a pending canonical plan without treating it as executing", () => {
    const pending = plan(["pending", "pending"]);
    const step = pending.plan[0];

    expect(shouldAutoFocusPlan(pending)).toBe(true);
    expect(shouldSurfacePlanProgress(pending)).toBe(true);
    expect(isPlanExecuting(pending)).toBe(false);
    expect(visiblePlanStepStatus(step)).toBe("pending");
    expect(isVisiblePlanStepActive(step)).toBe(false);
    expect(planStepProgressStatus(step, true)).toBe("pending");
    expect(planStepTodoStatus(step)).toBe("pending");
  });

  it("only treats in_progress steps as actively running", () => {
    const executing = plan(["in_progress", "pending"]);
    const step = executing.plan[0];

    expect(shouldAutoFocusPlan(executing)).toBe(true);
    expect(shouldSurfacePlanProgress(executing)).toBe(true);
    expect(isPlanExecuting(executing)).toBe(true);
    expect(visiblePlanStepStatus(step)).toBe("in_progress");
    expect(isVisiblePlanStepActive(step)).toBe(true);
    expect(planStepProgressStatus(step, true)).toBe("running");
    expect(planStepProgressStatus(step, false)).toBe("pending");
    expect(planStepTodoStatus(step)).toBe("in_progress");
  });

  it("surfaces completed plans without showing a running animation", () => {
    const completed = plan(["completed", "completed"]);
    const step = completed.plan[0];

    expect(shouldAutoFocusPlan(completed)).toBe(true);
    expect(shouldSurfacePlanProgress(completed)).toBe(true);
    expect(isPlanExecuting(completed)).toBe(false);
    expect(visiblePlanStepStatus(step)).toBe("completed");
    expect(isVisiblePlanStepActive(step)).toBe(false);
  });

  it("keeps blank MiniCode plan items as part of the canonical snapshot", () => {
    const blank: PlanState = {
      threadId: "thread-1",
      turnId: "turn-1",
      plan: [{ step: "", status: "pending" }],
    };

    expect(shouldAutoFocusPlan(blank)).toBe(true);
    expect(shouldSurfacePlanProgress(blank)).toBe(true);
  });
});
