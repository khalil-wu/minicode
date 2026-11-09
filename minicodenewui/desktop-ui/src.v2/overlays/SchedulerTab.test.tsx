/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SchedulerTab } from "./SchedulerTab";
import { openAutomations } from "../lib/automations-navigation";

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

const { sendClientCommand, sendClientCommandAwaitResult, toast } = vi.hoisted(() => ({
  sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("./ToastContainer", () => ({ pushToast: toast }));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand,
  sendClientCommandAwaitResult,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

describe("SchedulerTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    openAutomations();
    sendClientCommandAwaitResult.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "info", message: "", data: {} }));
    useAppStore.setState({
      conversationId: "conv_current",
      workingDirectory: "C:/A",
      isConnected: true,
      settingsOpen: false,
      scheduledTasks: [],
      scheduledTaskRuns: [],
    });
  });

  afterEach(() => { cleanup(); vi.useRealTimers(); });

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

  it("creates explicitly, then auto-saves the captured heartbeat after 600ms without switching the main owner", async () => {
    vi.useFakeTimers();
    const editor = { id: "draft:captured-A", owner: { id: "captured-A", title: "Captured A", workspaceRoot: "C:/captured-A", permissionMode: "bypass" as const }, open: true };
    sendClientCommandAwaitResult.mockImplementation(async (command: { type: string; name: string; prompt: string }) => {
      const task = { id: "saved-A", name: command.name,
        prompt: command.prompt, schedule: "0 * * * *", permission_mode: "auto", enabled: true,
        kind: "heartbeat", conversation_id: "captured-A", workspace_root: "C:/captured-A" };
      return { type: "command.result", command: command.type, level: "success", message: "", data: { task } };
    });
    render(<SchedulerTab editor={editor} />);
    expect(screen.getByLabelText("任务名称")).toHaveProperty("value", "Captured A");
    expect(screen.queryByLabelText("对话模式")).toBeNull();
    expect(screen.queryByText("暂无定时任务")).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "Continue captured A" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    const taskB = { id: "task-B", name: "B task", prompt: "B only", schedule: "0 * * * *", permission_mode: "auto", enabled: true, conversation_id: "new-main-B" };
    act(() => useAppStore.setState({ conversationId: "new-main-B", workingDirectory: "C:/B", draft: "Keep B's main draft", scheduledTasks: [taskB] }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "创建定时任务" })); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "scheduler.add", name: "Captured A", prompt: "Continue captured A", conversation_id: "captured-A",
      owner_conversation_id: "captured-A", workspace_root: "C:/captured-A", isolation: "workspace", permission_mode: "auto",
    }), "scheduler.add");
    expect(screen.queryByRole("button", { name: "保存修改" })).toBeNull();
    expect(screen.getByRole("button", { name: "打开设置" })).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "Revise the saved heartbeat" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(599); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "scheduler.update", task_id: "saved-A", prompt: "Revise the saved heartbeat", conversation_id: "captured-A",
      owner_conversation_id: "captured-A", workspace_root: "C:/captured-A",
    }), "scheduler.update");
    expect(sendClientCommandAwaitResult.mock.calls.filter(([command]) => command.type === "scheduler.add")).toHaveLength(1);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "new-main-B", workingDirectory: "C:/B", draft: "Keep B's main draft" });
    expect(useAppStore.getState().scheduledTasks).toEqual([taskB]);
    expect(toast).not.toHaveBeenCalled();
  });

  it("keeps a failed paused-task patch without a retry loop and retries only after an explicit action or draft change", async () => {
    vi.useFakeTimers();
    const task = { id: "paused-owner", name: "Paused check", prompt: "Original paused prompt", schedule: "20 8 * * 1-5",
      timezone: "Asia/Shanghai", isolation: "workspace" as const, permission_mode: "confirm", enabled: false,
      conversation_id: "paused-chat", workspace_root: "C:/paused" };
    sendClientCommandAwaitResult.mockResolvedValue({ type: "command.result", command: "scheduler.update", level: "error", message: "original scheduler write failure" });
    render(<SchedulerTab editor={{ id: task.id, owner: { id: "paused-chat", title: "Paused chat", workspaceRoot: "C:/paused", permissionMode: "auto" }, task, open: true }} />);
    expect(screen.getByLabelText("运行时间")).toHaveProperty("value", "08:20");
    expect(screen.queryByRole("button", { name: "保存修改" })).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "Unsaved paused edits" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "scheduler.update", task_id: "paused-owner", prompt: "Unsaved paused edits", schedule: "20 8 * * 1-5",
      permission_mode: "confirm", conversation_id: "paused-chat", owner_conversation_id: "paused-chat", workspace_root: "C:/paused",
    }), "scheduler.update");
    expect(screen.getByPlaceholderText("要运行的提示词")).toHaveProperty("value", "Unsaved paused edits");
    expect(screen.getByRole("button", { name: "重试保存" }).title).toBe("original scheduler write failure");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重试保存" })); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(2);
    sendClientCommandAwaitResult.mockResolvedValue({ type: "command.result", command: "scheduler.update", level: "success",
      data: { task: { ...task, prompt: "User revised the failed patch" } } });
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "User revised the failed patch" } });
    expect(screen.queryByRole("button", { name: "重试保存" })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("button", { name: "重试保存" })).toBeNull();
    expect(toast.mock.calls.every(([, tone]) => tone === "error")).toBe(true);
  });

  it("keeps normalized-equivalent titles and invalid interval drafts without automatic updates", async () => {
    vi.useFakeTimers();
    const task = { id: "valid-draft", name: "Check", prompt: "Original", schedule: "0 * * * *", timezone: "UTC",
      isolation: "workspace" as const, permission_mode: "auto", enabled: true, conversation_id: "draft-owner" };
    render(<SchedulerTab editor={{ id: task.id, owner: { id: "draft-owner", title: "Draft", workspaceRoot: "C:/draft", permissionMode: "auto" }, task, open: true }} />);
    fireEvent.change(screen.getByLabelText("任务名称"), { target: { value: "  Check  " } });
    await act(async () => { await vi.advanceTimersByTimeAsync(900); });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("重复间隔"), { target: { value: "" } });
    fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "Keep this invalid interval draft" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(900); });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(screen.getByLabelText("重复间隔")).toHaveProperty("value", "");
    fireEvent.change(screen.getByLabelText("任务名称"), { target: { value: " " } });
    fireEvent.change(screen.getByLabelText("重复间隔"), { target: { value: "1" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(900); });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(screen.getByLabelText("任务名称")).toHaveProperty("value", " ");
  });
});
