import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./index";

beforeEach(() => {
  useAppStore.setState({
    plan: null,
    todos: [],
    subagents: [],
    agentProgress: [],
    conversationAgentStates: {},
    conversationId: "conv-agent",
    rightStackTab: "preview",
    rightStackTabLocked: false,
    rightPanelOpen: true,
    rightSidebarWidth: 380,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("agent slice right-panel routing", () => {
  it("does not open Plan for an empty executing plan", () => {
    useAppStore.getState().setPlan({
      planId: "empty-plan",
      status: "executing",
      currentStep: 0,
      steps: [],
    });

    expect(useAppStore.getState().rightStackTab).toBe("preview");
  });

  it("keeps executing plans in the inline composer progress instead of opening Plan", () => {
    useAppStore.getState().setPlan({
      planId: "active-plan",
      status: "executing",
      currentStep: 0,
      steps: [{ id: "step-1", title: "Patch UI", status: "running" }],
    });

    expect(useAppStore.getState().rightStackTab).toBe("preview");
    expect(useAppStore.getState().plan?.steps[0]?.title).toBe("Patch UI");
  });

  it("keeps planning progress inline without opening the right sidebar", () => {
    useAppStore.getState().appendAgentProgress({
      id: "planning-progress",
      stage: "planning",
      phase: "planning",
      status: "running",
      message: "Planning next action",
      visibility: "compact",
    });

    expect(useAppStore.getState().rightStackTab).toBe("preview");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
  });

  it("does not route internal timeline phases to the right sidebar", () => {
    useAppStore.getState().appendAgentProgress({
      id: "agent-phase:run-1:execute",
      stage: "planning",
      phase: "tool",
      status: "running",
      message: "Model deciding next action",
      visibility: "timeline",
    });

    expect(useAppStore.getState().rightStackTab).toBe("preview");
  });

  it("keeps main agent progress serial by completing the previous running phase", () => {
    useAppStore.getState().appendAgentProgress({
      id: "agent-run:run-1",
      stage: "planning",
      phase: "planning",
      status: "running",
      message: "Agent run started",
      visibility: "timeline",
    });
    useAppStore.getState().appendAgentProgress({
      id: "agent-phase:run-1:context",
      stage: "planning",
      phase: "planning",
      status: "running",
      message: "Preparing agent context",
      visibility: "timeline",
    });
    useAppStore.getState().appendAgentProgress({
      id: "agent-phase:run-1:execute",
      stage: "planning",
      phase: "tool",
      status: "running",
      message: "Model deciding next action",
      visibility: "timeline",
    });

    const progress = useAppStore.getState().agentProgress;
    expect(progress.map((item) => [item.id, item.status])).toEqual([
      ["agent-run:run-1", "completed"],
      ["agent-phase:run-1:context", "completed"],
      ["agent-phase:run-1:execute", "running"],
    ]);
  });

  it("keeps cumulative provider detail through terminal, duplicate, and late frames", () => {
    useAppStore.getState().appendAgentProgress({
      id: "provider:mcp-1",
      stage: "tool",
      phase: "tool",
      status: "running",
      message: "MCP tool call prepared: lookup",
      summary: "MCP tool call prepared: lookup",
      visibility: "timeline",
      detail: "Server: audit-local · Tool: lookup · Arguments: 47 characters",
      ephemeral: true,
    });
    useAppStore.getState().appendAgentProgress({
      id: "provider:mcp-1",
      stage: "tool",
      phase: "tool",
      status: "completed",
      message: "MCP tool completed: lookup",
      summary: "MCP tool completed: lookup",
      visibility: "timeline",
      detail: "Server: audit-local · Tool: lookup",
    });
    useAppStore.getState().appendAgentProgress({
      id: "provider:mcp-1",
      stage: "tool",
      phase: "tool",
      status: "running",
      message: "MCP tool in progress: lookup",
      summary: "MCP tool in progress: lookup",
      visibility: "timeline",
    });

    expect(useAppStore.getState().agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:mcp-1",
        status: "completed",
        message: "MCP tool completed: lookup",
        summary: "MCP tool completed: lookup",
        detail: "Server: audit-local · Tool: lookup · Arguments: 47 characters",
      }),
    ]);
    expect(useAppStore.getState().agentProgress[0]).not.toHaveProperty("ephemeral");
  });

  it("keeps a provider retry row's first-seen timestamp stable", () => {
    vi.spyOn(Date, "now")
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(200);

    useAppStore.getState().appendAgentProgress({
      id: "provider:retry",
      stage: "status",
      phase: "model",
      status: "running",
      message: "正在重连",
      visibility: "timeline",
      retryAttempt: 1,
      maxRetries: 5,
    });
    useAppStore.getState().appendAgentProgress({
      id: "provider:retry",
      stage: "status",
      phase: "model",
      status: "running",
      message: "正在重连",
      visibility: "timeline",
      retryAttempt: 2,
      maxRetries: 5,
    });

    expect(useAppStore.getState().agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:retry",
        timestamp: 100,
        retryAttempt: 2,
        maxRetries: 5,
      }),
    ]);
  });

  it("updates an existing subagent in place instead of moving it to the bottom", () => {
    useAppStore.getState().addSubagent({
      id: "subagent-a",
      role: "explorer",
      status: "running",
      summary: "Reading files",
    });
    useAppStore.getState().addSubagent({
      id: "subagent-b",
      role: "reviewer",
      status: "running",
      summary: "Checking UI",
    });

    useAppStore.getState().addSubagent({
      id: "subagent-a",
      role: "explorer",
      status: "done",
      summary: "Read files",
      resultContent: "Done",
    });

    expect(useAppStore.getState().subagents.map((subagent) => subagent.id)).toEqual([
      "subagent-a",
      "subagent-b",
    ]);
    expect(useAppStore.getState().subagents[0]).toMatchObject({
      status: "done",
      resultContent: "Done",
    });
  });

  it("retains more than 20 subagents and upserts unknown updates", () => {
    for (let index = 0; index < 25; index += 1) {
      useAppStore.getState().addSubagent({
        id: `subagent-${index}`,
        role: "worker",
        status: "running",
      });
    }

    useAppStore.getState().updateSubagent("late-subagent", {
      role: "reviewer",
      status: "done",
      summary: "Recovered from done event",
    });

    const subagents = useAppStore.getState().subagents;
    expect(subagents).toHaveLength(26);
    expect(subagents[0]?.id).toBe("subagent-0");
    expect(subagents.at(-1)).toMatchObject({
      id: "late-subagent",
      role: "reviewer",
      status: "done",
      summary: "Recovered from done event",
    });
  });
});
