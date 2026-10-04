import { describe, expect, it } from "vitest";
import {
  hasVisiblePlanSteps,
  planStepProgressStatus,
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

    expect(hasVisiblePlanSteps(pending)).toBe(true);
    expect(planStepProgressStatus(step, true)).toBe("pending");
  });

  it("only treats in_progress steps as actively running", () => {
    const executing = plan(["in_progress", "pending"]);
    const step = executing.plan[0];

    expect(hasVisiblePlanSteps(executing)).toBe(true);
    expect(planStepProgressStatus(step, true)).toBe("running");
    expect(planStepProgressStatus(step, false)).toBe("pending");
  });

  it("surfaces completed plans without showing a running animation", () => {
    const completed = plan(["completed", "completed"]);
    const step = completed.plan[0];

    expect(hasVisiblePlanSteps(completed)).toBe(true);
    expect(planStepProgressStatus(step, true)).toBe("completed");
  });

  it("keeps blank MiniCode plan items as part of the canonical snapshot", () => {
    const blank: PlanState = {
      threadId: "thread-1",
      turnId: "turn-1",
      plan: [{ step: "", status: "pending" }],
    };

    expect(hasVisiblePlanSteps(blank)).toBe(true);
  });
});
