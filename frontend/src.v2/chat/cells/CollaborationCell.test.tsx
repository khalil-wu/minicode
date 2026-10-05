/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CollaborationCell } from "./CollaborationCell";
import { useAppStore } from "../../stores";
import type { CollaborationCellState } from "./cellTypes";

describe("CollaborationCell agent navigation", () => {
  beforeEach(() => useAppStore.setState({
    conversationId: "collaboration-owner",
    focusedSubagentId: null,
    rightStackTab: "tasks",
    subagents: [{ id: "worker-a", agentPath: "/root/a", teammateName: "Ada", role: "explore", status: "running", objective: "审阅布局" }],
  }));
  afterEach(cleanup);
  const cell: CollaborationCellState = {
    id: "delegation", kind: "collaboration", action: "delegated", status: "success",
    entries: [{ agentId: "worker-a", agentLabel: "布局任务", content: "检查页面布局" }],
    collapsed: false, createdAt: 1,
  };

  it("keeps delegation success separate from the child's live status and opens that child", () => {
    render(<CollaborationCell cell={cell} conversationId="collaboration-owner" />);
    expect(screen.getByText("已委派 · 1 个子任务")).toBeTruthy();
    expect(screen.getByText("1 运行中")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
  });

  it("resolves a named message recipient to the same child", () => {
    render(<CollaborationCell cell={{ ...cell, action: "sent_message", entries: [{ agentId: "Ada", agentLabel: "Ada", content: "检查深色主题" }] }} conversationId="collaboration-owner" />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(screen.getByText("已发送消息 · 1 个子任务")).toBeTruthy();
  });

  it("does not invent a clickable child from a prompt or an unknown id", () => {
    render(<CollaborationCell cell={{ ...cell, entries: [{ agentId: "missing", agentLabel: "尚未返回任务身份" }] }} conversationId="collaboration-owner" />);
    expect(screen.queryByRole("button", { name: /打开子智能体/ })).toBeNull();
  });

  it("keeps long delegated instructions folded even when a failed task needs attention", () => {
    const instructions = "核对页面布局、代码编辑器与预览，再检查字体、间距和操作入口。".repeat(20);
    const { container } = render(<CollaborationCell cell={{ ...cell, status: "failed", collapsed: true,
      error: "子任务启动失败", entries: [{ agentId: "worker-a", agentLabel: "布局任务", content: instructions }],
    }} conversationId="collaboration-owner" />);
    expect(screen.getByRole("alert").textContent).toBe("子任务启动失败");
    const disclosure = container.querySelector<HTMLDetailsElement>(".collaboration-task-instructions")!;
    expect(disclosure.open).toBe(false);
    expect(disclosure.textContent).toContain(instructions);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
  });
});
