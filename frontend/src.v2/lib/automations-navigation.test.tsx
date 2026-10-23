/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SchedulerTab } from "../overlays/SchedulerTab";
import { openAutomations } from "./automations-navigation";

const { send } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { send: vi.fn() };
});
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(), sendClientCommandAwaitResult: send,
  commandResultSucceeded: (result: { level?: string }) => result.level !== "error",
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  openAutomations();
  useAppStore.setState({ conversationId: "settings-B", workingDirectory: "C:/settings-B", draft: "B main draft",
    isConnected: true, settingsOpen: false, scheduledTasks: [], scheduledTaskRuns: [] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("opens the captured task's settings, uses that list ACK instead of B's inventory, and clears the intent on ordinary navigation", async () => {
  vi.useFakeTimers();
  const owner = { id: "settings-A", title: "Thread A", workspaceRoot: "C:/settings-A", permissionMode: "auto" as const };
  let taskA = { id: "task-A", name: "A task", prompt: "A prompt", schedule: "0 * * * *", permission_mode: "auto",
    enabled: true, conversation_id: owner.id, workspace_root: owner.workspaceRoot };
  const siblingA = { ...taskA, id: "sibling-A", name: "A sibling", conversation_id: "A sibling thread" };
  const taskB = { ...taskA, id: "task-B", name: "B only", conversation_id: "settings-B", workspace_root: "C:/settings-B" };
  const runA = { id: "run-A", task_id: taskA.id, status: "completed", scheduled_at: "2026-10-23T00:00:00Z", conversation_id: owner.id };
  const runB = { ...runA, id: "run-B", task_id: taskB.id, conversation_id: "settings-B" };
  useAppStore.setState({ scheduledTasks: [taskB], scheduledTaskRuns: [runB] });
  send.mockImplementation(async (command: Record<string, unknown>) => {
    if (command.type === "scheduler.update") taskA = { ...taskA, prompt: command.prompt as string };
    return { type: "command.result", level: "success", data: command.type === "scheduler.list"
      ? { tasks: command.owner_conversation_id === owner.id ? [taskA, siblingA] : [taskB], runs: command.owner_conversation_id === owner.id ? [runA] : [runB] }
      : { task: taskA } };
  });
  const side = render(<SchedulerTab editor={{ id: taskA.id, owner, task: taskA, open: true }} />);
  fireEvent.click(screen.getByRole("button", { name: "打开设置" }));
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "scheduler",
    conversationId: "settings-B", workingDirectory: "C:/settings-B", draft: "B main draft" });
  side.unmount();
  await act(async () => { render(<SchedulerTab />); });
  expect(screen.getByLabelText("任务名称")).toHaveProperty("value", "A task");
  expect(screen.getByRole("button", { name: "编辑 A sibling" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "编辑 B only" })).toBeNull();
  expect(send).toHaveBeenCalledWith({ type: "scheduler.list", owner_conversation_id: owner.id, workspace_root: owner.workspaceRoot }, "scheduler.list");
  fireEvent.change(screen.getByPlaceholderText("要运行的提示词"), { target: { value: "A settings draft" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(900); });
  expect(send.mock.calls.some(([command]) => command.type === "scheduler.update")).toBe(false);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "保存修改" })); });
  expect(send).toHaveBeenCalledWith(expect.objectContaining({ type: "scheduler.update", task_id: taskA.id,
    prompt: "A settings draft", owner_conversation_id: owner.id, workspace_root: owner.workspaceRoot }), "scheduler.update");
  expect(useAppStore.getState().scheduledTasks).toEqual([taskB]);
  act(() => openAutomations());
  expect(screen.getByLabelText("任务名称")).toHaveProperty("value", "");
  expect(screen.getByRole("button", { name: "编辑 B only" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "编辑 A task" })).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "settings-B", workingDirectory: "C:/settings-B", draft: "B main draft" });
});
