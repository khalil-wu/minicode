/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (command: unknown, expectedCommand: string) => ({
    type: "command.result",
    command: expectedCommand,
    level: "success",
    message: "",
    data: { command },
  })),
  pushToast: vi.fn(),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: mocks.sendClientCommand,
  sendClientCommandAwaitResult: mocks.sendClientCommandAwaitResult,
  sendPromptResponseCommand: vi.fn(async () => null),
  commandResultSucceeded: (result: { level?: string }) =>
    !["error", "failed"].includes(String(result?.level || "").toLowerCase()),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: mocks.pushToast,
}));

import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { handleRuntimeEvent } from "./runtimeEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

const PLAN = "## 计划\n\n1. 补齐审批链路\n2. 补测试";

const planApprovalEvent = (overrides: Record<string, unknown> = {}) => ({
  type: "subagent.plan_approval_requested",
  conversation_id: "conv-plan",
  subagent_id: "sub-7",
  request_id: "plan-req-1",
  teammate_name: "builder",
  team_name: "release",
  plan_file_path: "/repo/.minicode/plans/builder.md",
  plan_content: PLAN,
  ...overrides,
});

describe("teammate plan approval prompt", () => {
  beforeEach(() => {
    mocks.sendClientCommand.mockClear();
    mocks.sendClientCommandAwaitResult.mockClear();
    mocks.pushToast.mockClear();
    useAppStore.setState({
      conversationId: "conv-plan",
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      diffReview: null,
      pendingAskUser: null,
      askUserQueue: [],
      inspectorEntries: [],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("projects the request into the blocking prompt slot", () => {
    expect(handleRuntimeEvent(planApprovalEvent() as unknown as ServerEvent, "conv-plan")).toBe(true);

    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "plan-req-1",
      conversationId: "conv-plan",
      question: "子智能体 builder 提交了计划，需要你批准后才能开始实现。",
      planReview: {
        subagentId: "sub-7",
        teammateName: "builder",
        teamName: "release",
        plan_file_path: "/repo/.minicode/plans/builder.md",
        planContent: PLAN,
      },
    });
  });

  it("shows the teammate name and the plan body to the user", () => {
    handleRuntimeEvent(planApprovalEvent() as unknown as ServerEvent, "conv-plan");
    render(<InlineAgentPrompt />);

    expect(screen.getByText("子智能体 builder 提交了计划，需要你批准后才能开始实现。")).toBeTruthy();
    expect(screen.getByText("补齐审批链路")).toBeTruthy();
    expect(screen.getByText("Plan 文件：/repo/.minicode/plans/builder.md")).toBeTruthy();
  });

  it("approves with subagent.plan_review and clears the prompt", async () => {
    handleRuntimeEvent(planApprovalEvent() as unknown as ServerEvent, "conv-plan");
    render(<InlineAgentPrompt />);

    fireEvent.click(screen.getByRole("button", { name: "批准子智能体的计划" }));

    await waitFor(() => expect(mocks.sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "subagent.plan_review",
      subagent_id: "sub-7",
      request_id: "plan-req-1",
      approved: true,
      conversation_id: "conv-plan",
    }, "subagent.plan_review"));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
  });

  it("rejects with approved: false", async () => {
    handleRuntimeEvent(planApprovalEvent() as unknown as ServerEvent, "conv-plan");
    render(<InlineAgentPrompt />);

    fireEvent.click(screen.getByRole("button", { name: "拒绝子智能体的计划" }));

    await waitFor(() => expect(mocks.sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "subagent.plan_review",
      subagent_id: "sub-7",
      request_id: "plan-req-1",
      approved: false,
      conversation_id: "conv-plan",
    }, "subagent.plan_review"));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
  });

  it("keeps the prompt and toasts when the backend refuses the decision", async () => {
    mocks.sendClientCommandAwaitResult.mockResolvedValueOnce({
      type: "command.result",
      command: "subagent.plan_review",
      level: "error",
      message: "Teammate sub-7 is not running; the request is no longer actionable.",
      data: {},
    });
    handleRuntimeEvent(planApprovalEvent() as unknown as ServerEvent, "conv-plan");
    render(<InlineAgentPrompt />);

    fireEvent.click(screen.getByRole("button", { name: "批准子智能体的计划" }));

    await waitFor(() => expect(mocks.pushToast).toHaveBeenCalledWith(
      "Teammate sub-7 is not running; the request is no longer actionable.",
      "error",
      4500,
    ));
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("plan-req-1");
    expect(screen.getByRole("alert").textContent).toContain("no longer actionable");
  });

  it("accepts the wire event and rejects one without a request id", () => {
    expect(normalizeInboundServerEvent(planApprovalEvent())).not.toBeNull();
    expect(normalizeInboundServerEvent(planApprovalEvent({ request_id: undefined }))).toBeNull();
    expect(normalizeInboundServerEvent(planApprovalEvent({ subagent_id: "" }))).toBeNull();
  });
});
