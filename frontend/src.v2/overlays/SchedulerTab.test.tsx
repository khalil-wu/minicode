/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SchedulerTab } from "./SchedulerTab";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
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

const { sendClientCommand, sendClientCommandAwaitResult } = vi.hoisted(() => ({
  sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand,
  sendClientCommandAwaitResult,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

describe("SchedulerTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sendClientCommandAwaitResult.mockResolvedValue({ type: "command.result", command: "scheduler.add", level: "info", message: "", data: {} });
    useAppStore.setState({
      conversationId: "conv_current",
      scheduledTasks: [],
      scheduledTaskRuns: [],
    });
  });

  afterEach(() => cleanup());

  it("creates a timezone-aware isolated heartbeat task", async () => {
    render(<SchedulerTab />);

    fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "每日检查" } });
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "检查构建" } });
    fireEvent.change(screen.getByLabelText("运行频率"), { target: { value: "daily" } });
    fireEvent.change(screen.getByLabelText("运行时间"), { target: { value: "09:30" } });
    fireEvent.change(screen.getByLabelText("时区"), { target: { value: "Asia/Shanghai" } });
    fireEvent.change(screen.getByLabelText("对话模式"), { target: { value: "heartbeat" } });
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "scheduler.add",
      name: "每日检查",
      prompt: "检查构建",
      schedule: "30 9 * * *",
      timezone: "Asia/Shanghai",
      isolation: "worktree",
      permission_mode: "auto",
      conversation_id: "conv_current",
      owner_conversation_id: "conv_current",
      workspace_root: undefined,
    }, "scheduler.add"));
    await waitFor(() => expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe(""));
  });

  it("keeps task fields when creation fails", async () => {
    sendClientCommandAwaitResult.mockResolvedValueOnce({ type: "command.result", command: "scheduler.add", level: "error", message: "invalid cron", data: {} });
    render(<SchedulerTab />);

    fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "失败任务" } });
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "保留输入" } });
    fireEvent.click(screen.getByRole("button", { name: /添加/ }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalled());
    expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe("失败任务");
    expect((screen.getByPlaceholderText("要运行的提示词") as HTMLTextAreaElement).value).toBe("保留输入");
  });

  it("opens the conversation produced by a scheduled run", () => {
    const requestConversationSwitch = vi.fn();
    useAppStore.setState({
      requestConversationSwitch,
      scheduledTasks: [{
        id: "task_1",
        name: "构建检查",
        prompt: "test",
        schedule: "0 * * * *",
        permission_mode: "auto_approve",
        enabled: true,
      }],
      scheduledTaskRuns: [{
        id: "run_1",
        task_id: "task_1",
        scheduled_at: new Date().toISOString(),
        status: "completed",
        conversation_id: "conv_run",
        result_summary: "构建通过",
      }],
    });

    render(<SchedulerTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开运行对话" }));

    expect(requestConversationSwitch).toHaveBeenCalledWith("conv_run");
  });
});
