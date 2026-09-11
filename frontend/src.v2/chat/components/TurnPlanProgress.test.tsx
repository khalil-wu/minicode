/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { TurnPlanProgress } from "./TurnPlanProgress";

describe("TurnPlanProgress", () => {
  beforeEach(() => {
    useAppStore.setState({
      plan: null,
      todos: [],
      subagents: [],
      messages: [],
      isStreaming: false,
      conversationId: null,
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
      turnDiffs: {},
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("does not project todos or subagents into the MiniCode plan pill", () => {
    useAppStore.setState({
      isStreaming: true,
      todos: [{ id: "todo-1", content: "legacy", activeForm: "legacy", status: "in_progress" }],
      subagents: [{ id: "agent-1", role: "explore", status: "running", objective: "child work" }],
    });

    const { container } = render(<TurnPlanProgress />);

    expect(container.firstChild).toBeNull();
  });

  it("renders the current-turn plan without projecting file diffs", () => {
    useAppStore.setState({
      conversationId: "conv-1",
      isStreaming: true,
      messages: [{
        id: "assistant-1",
        turnId: "turn-1",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      plan: {
        threadId: "conv-1",
        turnId: "turn-1",
        explanation: "Execution order changed after inspection",
        plan: [
          { step: "Inspect sources", status: "in_progress" },
          { step: "Apply fixes", status: "pending" },
          { step: "Verify", status: "pending" },
        ],
      },
    });

    render(<TurnPlanProgress />);

    const pill = screen.getByRole("button", { name: "已完成 0 / 3 项 · Inspect sources" });
    expect(pill).toBeTruthy();
    expect(screen.getByText("Inspect sources")).toBeTruthy();

    fireEvent.click(pill);

    expect(screen.getByRole("dialog", { name: "当前计划" })).toBeTruthy();
    expect(screen.getByText("Execution order changed after inspection")).toBeTruthy();
    expect(within(screen.getByRole("dialog")).getByText("Inspect sources")).toBeTruthy();
    expect(screen.getByText("Apply fixes")).toBeTruthy();
    expect(screen.getByText("Verify")).toBeTruthy();
    act(() => useAppStore.setState({ plan: {
      threadId: "conv-1", turnId: "turn-1", plan: [
        { step: "Inspect sources", status: "completed" },
        { step: "Apply fixes", status: "in_progress" },
        { step: "Verify", status: "completed" },
      ],
    } }));
    expect(screen.getByRole("button", { name: "已完成 2 / 3 项 · Apply fixes" })).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
    act(() => useAppStore.setState({
      messages: [{ ...useAppStore.getState().messages[0], turnId: "turn-2" }],
      plan: { threadId: "conv-1", turnId: "turn-2", plan: [{ step: "New work", status: "in_progress" }] },
    }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("rejects a plan snapshot owned by another turn", () => {
    useAppStore.setState({
      conversationId: "conv-1",
      isStreaming: true,
      messages: [{
        id: "assistant-2",
        turnId: "turn-2",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      plan: {
        threadId: "conv-1",
        turnId: "turn-1",
        plan: [{ step: "Old work", status: "in_progress" }],
      },
    });

    const { container } = render(<TurnPlanProgress />);

    expect(container.firstChild).toBeNull();
  });
});
