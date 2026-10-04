/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    sendClientCommandAwaitResult.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "info", message: "", data: {} }));
    useAppStore.setState({
      conversationId: "conv_current",
      workingDirectory: "C:/A",
      isConnected: true,
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
      workspace_root: "C:/A",
    }, "scheduler.add"));
    await waitFor(() => expect((screen.getByPlaceholderText("任务名称") as HTMLInputElement).value).toBe(""));
  });

  it("keeps task fields when creation fails", async () => {
    sendClientCommandAwaitResult.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: command.type === "scheduler.add" ? "error" : "info", message: command.type === "scheduler.add" ? "invalid cron" : "", data: {} }));
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

  it("edits the same task and preserves its original conversation and permission", async () => {
    useAppStore.setState({ scheduledTasks: [{ id: "task_edit", name: "原任务", prompt: "原提示词", schedule: "0 9 * * 1-5", timezone: "Asia/Shanghai", isolation: "workspace", permission_mode: "confirm", enabled: true, conversation_id: "conv_original" }] });
    render(<SchedulerTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 原任务" }));
    fireEvent.change(screen.getByLabelText("运行时间"), { target: { value: "10:20" } });
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "新的提示词" } });
    fireEvent.click(screen.getByRole("button", { name: /保存修改/ }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "scheduler.update", task_id: "task_edit", prompt: "新的提示词", schedule: "20 10 * * 1-5", timezone: "Asia/Shanghai",
      permission_mode: "confirm", conversation_id: "conv_original", owner_conversation_id: "conv_current", workspace_root: "C:/A",
    }), "scheduler.update"));
  });

  it("keeps separate draft fields when switching away from and back to a project task", () => {
    render(<SchedulerTab />);
    fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "A的草稿" } });
    act(() => useAppStore.setState({ conversationId: "conv_B", workingDirectory: "C:/B" }));
    expect(screen.getByPlaceholderText("任务名称")).toHaveProperty("value", "");
    fireEvent.change(screen.getByPlaceholderText("任务名称"), { target: { value: "B的草稿" } });
    act(() => useAppStore.setState({ conversationId: "conv_current", workingDirectory: "C:/A" }));
    expect(screen.getByPlaceholderText("任务名称")).toHaveProperty("value", "A的草稿");
  });

  it("loads older task-specific history even when absent from the recent snapshot", async () => {
    useAppStore.setState({ scheduledTasks: [{ id: "task_old", name: "旧任务", prompt: "检查", schedule: "0 * * * *", permission_mode: "auto", enabled: true }] });
    sendClientCommandAwaitResult.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "success", message: "", data: command.type === "scheduler.history" ? { runs: [{ id: "old_run", task_id: "task_old", status: "completed", scheduled_at: "2026-01-01T00:00:00Z", result_summary: "更早的结果" }], next_offset: 1, has_more: false } : {} }));
    render(<SchedulerTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 旧任务" }));
    fireEvent.click(screen.getByRole("button", { name: "显示更多运行记录" }));
    expect(await screen.findByText("更早的结果")).toBeTruthy();
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "scheduler.history", task_id: "task_old", limit: 50, offset: 0, workspace_root: "C:/A" }), "scheduler.history");
    expect(screen.queryByRole("button", { name: "显示更多运行记录" })).toBeNull();
  });

  it("renders next, last and historical runs in the same explicitly named task timezone", () => {
    const zone = "Pacific/Honolulu";
    const last = "2026-10-04T09:00:00Z";
    const next = "2026-10-05T09:00:00Z";
    useAppStore.setState({ scheduledTasks: [{ id: "zoned", name: "异地计划", prompt: "检查", schedule: "0 9 * * *", permission_mode: "auto", enabled: true, timezone: zone, last_run_at: last, next_run_at: next }],
      scheduledTaskRuns: [{ id: "zoned-run", task_id: "zoned", status: "completed", scheduled_at: last, started_at: last }] });
    const view = render(<SchedulerTab />);
    const formatted = (timestamp: string) => new Date(timestamp).toLocaleString(undefined, { timeZone: zone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    expect(view.container.querySelector(".scheduler-task-next")?.textContent).toContain(`${formatted(next)} · ${zone}`);
    expect(view.container.querySelector(".scheduler-task-last")?.textContent).toContain(`${formatted(last)} · ${zone}`);
    expect(view.container.querySelector(".scheduler-run-date")?.textContent).toContain(`${formatted(last)} · ${zone}`);
  });
});
