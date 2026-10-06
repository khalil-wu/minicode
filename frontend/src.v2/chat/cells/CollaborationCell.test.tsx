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
    expect(screen.queryByText("开始工作")).toBeNull();
    expect(screen.queryByRole("button", { name: /子任务详情/ })).toBeNull();
    expect(screen.getByRole("button", { name: "打开子智能体：Ada" }).textContent).toBe("Ada");
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
  });

  it("keeps only the real glyph and name clickable without an instruction disclosure", () => {
    const { container } = render(<CollaborationCell cell={{ ...cell, collapsed: true }} conversationId="collaboration-owner" />);
    expect(screen.queryByText("检查页面布局")).toBeNull();
    expect(screen.queryByText("委派指令")).toBeNull();
    expect(container.querySelector("details")).toBeNull();
    expect(container.querySelector(".collaboration-cell-details")).toBeNull();
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
  });

  it("resolves a named message recipient to the same child", () => {
    render(<CollaborationCell cell={{ ...cell, action: "sent_message", entries: [{ agentId: "Ada", agentLabel: "Ada", content: "检查深色主题" }] }} conversationId="collaboration-owner" />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(screen.queryByText("已发送消息 · 1 个子任务")).toBeNull();
    expect(screen.queryByText("检查深色主题")).toBeNull();
    expect(screen.getByRole("button", { name: "打开子智能体：Ada" }).textContent).toBe("Ada");
  });

  it.each(["sent_message", "closed"] as const)("keeps %s as one identity row without exposing follow-up instructions or duplicate summaries", (action) => {
    const instructions = "CLI已实现，使用临时文件继续测试，并确认所有金额边界。".repeat(8);
    const { container } = render(<CollaborationCell cell={{ ...cell, action, collapsed: false,
      entries: [{ agentId: "worker-a", agentLabel: "Ada", content: instructions }],
    }} conversationId="collaboration-owner" />);
    expect(container.querySelectorAll(".collaboration-delegated-row")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "打开子智能体：Ada" }).textContent).toBe("Ada");
    expect(container.querySelector(".collaboration-cell-details")).toBeNull();
    expect(container.querySelector(".collaboration-cell-summary")).toBeNull();
    expect(container.textContent).not.toContain(instructions);
    expect(container.querySelector("details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
  });

  it("retains a real send failure while keeping the worker instructions out of the parent", () => {
    const { container } = render(<CollaborationCell cell={{ ...cell, action: "sent_message", status: "failed", error: "消息发送失败，请重新连接",
      entries: [{ agentId: "worker-a", agentLabel: "Ada", content: "Secret worker instructions" }],
    }} conversationId="collaboration-owner" />);
    expect(container.querySelector("details")?.open).toBe(false);
    fireEvent.click(screen.getByText("错误详情"));
    expect(screen.getByRole("alert").textContent).toBe("消息发送失败，请重新连接");
    expect(screen.queryByRole("status")).toBeNull();
    expect(container.textContent).not.toContain("Secret worker instructions");
    expect(screen.getByRole("button", { name: "打开子智能体：Ada" })).toBeTruthy();
  });

  it("does not invent a clickable child from a prompt or an unknown id", () => {
    render(<CollaborationCell cell={{ ...cell, entries: [{ agentId: "missing", agentLabel: "尚未返回任务身份" }] }} conversationId="collaboration-owner" />);
    expect(screen.queryByRole("button", { name: /打开子智能体/ })).toBeNull();
    expect(screen.queryByText("尚未返回任务身份")).toBeNull();
    expect(screen.queryByText(/子任务 \d/)).toBeNull();
  });

  it("never includes long delegated instructions when a real failure needs attention", () => {
    const instructions = "核对页面布局、代码编辑器与预览，再检查字体、间距和操作入口。".repeat(20);
    const { container } = render(<CollaborationCell cell={{ ...cell, status: "failed", collapsed: true,
      error: "子任务启动失败", entries: [{ agentId: "worker-a", agentLabel: "布局任务", content: instructions }],
    }} conversationId="collaboration-owner" />);
    expect(container.querySelector("details")?.open).toBe(false);
    fireEvent.click(screen.getByText("错误详情"));
    expect(screen.getByRole("alert").textContent).toBe("子任务启动失败");
    expect(screen.queryByText("委派失败")).toBeNull();
    expect(container.querySelector("details")?.open).toBe(true);
    expect(container.textContent).not.toContain(instructions);
    expect(screen.queryByText("委派指令")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
  });

  it("does not turn an unstarted request without an agent id into a named or clickable agent", () => {
    const { container } = render(<CollaborationCell cell={{ ...cell, status: "running", collapsed: false,
      entries: [{ agentId: "", agentLabel: "parser", content: "Implement parser.py and then test it" }],
    }} conversationId="collaboration-owner" />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector(".mc-agent-avatar")).toBeNull();
    expect(screen.queryByText("parser")).toBeNull();
    expect(container.textContent).not.toContain("Implement parser.py");
    expect(useAppStore.getState().focusedSubagentId).toBeNull();
  });

  it("pairs each parallel worker's own glyph and name on one row without prompt or start labels", () => {
    useAppStore.setState((state) => ({ subagents: [...state.subagents,
      { id: "worker-b", agentPath: "/root/b", teammateName: "Lin", role: "subagent", status: "running", objective: "实现解析器" }],
    }));
    const { container } = render(<CollaborationCell cell={{ ...cell, entries: [
      ...cell.entries, { agentId: "worker-b", agentLabel: "解析器任务", content: "Implement all parser details" },
      { agentId: "worker-a", agentLabel: "布局任务", content: "Repeated command receipt" },
    ] }} conversationId="collaboration-owner" />);
    const rows = container.querySelectorAll(".collaboration-delegated-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toBe("Ada");
    expect(rows[1].textContent).toBe("Lin");
    for (const row of rows) expect(row.querySelectorAll(".mc-agent-avatar-art")).toHaveLength(1);
    expect(screen.queryByText("开始工作")).toBeNull();
    expect(screen.queryByText("Implement all parser details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Lin" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-b");
  });

  it("keeps another conversation's real worker visible without opening it in the active conversation", () => {
    useAppStore.setState({ conversationAgentStates: { archived: { subagents: [{ id: "worker-a", agentPath: "/root/a", teammateName: "Ada", role: "explore", status: "done" }], todos: [], agentProgress: [], plan: null } } });
    render(<CollaborationCell cell={cell} conversationId="archived" />);
    expect(screen.getByText("Ada")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /打开子智能体/ })).toBeNull();
    expect(useAppStore.getState().focusedSubagentId).toBeNull();
  });

  it("renders a real completion as a quiet identity, name and status row", () => {
    const { container } = render(<CollaborationCell cell={{ ...cell, action: "completed", collapsed: true,
      entries: [{ agentId: "worker-a", agentLabel: "Ada", agentIdentity: "/root/a" }],
    }} conversationId="collaboration-owner" />);
    expect(screen.getByText("Ada")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
    expect(screen.queryByText("查看运行详情")).toBeNull();
    expect(screen.queryByText("委派指令")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Ada" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-a");
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
  });

  it("does not call a cancelled child completed or invent navigation for a missing child", () => {
    render(<CollaborationCell cell={{ ...cell, action: "completed", status: "cancelled",
      entries: [{ agentId: "retired", agentLabel: "Verify prediction", agentIdentity: "/root/verify_prediction" }],
    }} conversationId="collaboration-owner" />);
    expect(screen.getByText("Verify prediction")).toBeTruthy();
    expect(screen.getByText("已停止")).toBeTruthy();
    expect(screen.queryByText("已完成")).toBeNull();
    expect(screen.queryByRole("button", { name: /打开子智能体/ })).toBeNull();
  });
});
