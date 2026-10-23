// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ScheduledTaskEntry } from "../stores/types";
import type { ClientCommand, CommandResultEvent, ServerEvent } from "../protocol/events";
import { registerWebSocketSender, resolveClientCommandResult, rejectAllPendingCommandResults } from "../protocol/ws-outbox";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
import { ChatActionsButton } from "../shell/ChatActionsButton";
import { closeThreadAutomationEditor, loadThreadAutomations, openThreadAutomationEditor, renameThreadAutomationEditor,
  removeThreadAutomationEditor, useThreadAutomationEditors } from "./automations-navigation";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));

const executionRoot = "C:/worktrees/A";
const task: ScheduledTaskEntry = { id: "owned-heartbeat-A", name: "Follow A", prompt: "Inspect A", schedule: "0 * * * *",
  enabled: false, permission_mode: "confirm", conversation_id: "A", kind: "heartbeat", workspace_root: executionRoot };
const otherTask: ScheduledTaskEntry = { ...task, id: "owned-heartbeat-B", conversation_id: "B", workspace_root: "C:/B" };
const owner = { id: "A", title: "Chat A", workspaceRoot: executionRoot, permissionMode: "confirm" as const };
let requests: ClientCommand[];
const deliver = (request: ClientCommand) => {
  const scope = { conversation_id: "A", workspace_root: executionRoot };
  handlePeripheralEvent({ type: "scheduler.list", tasks: [task], runs: [], ...scope } as ServerEvent);
  resolveClientCommandResult({ type: "command.result", command: "scheduler.list", level: "success", message: "",
    client_command_id: request.client_command_id, data: { tasks: [task], runs: [], ...scope } } as CommandResultEvent);
};

beforeEach(() => {
  requests = [];
  registerWebSocketSender(command => { requests.push(command); return true; });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  window.__MINICODE_RUNTIME__ = undefined;
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, workingDirectory: "C:/visible", appMode: "cowork",
    conversations: [{ id: "A", title: "Chat A", workspaceRoot: "C:/project/A", executionCwd: executionRoot, updatedAt: "today" },
      { id: "B", title: "Chat B", workspaceRoot: "C:/B", updatedAt: "today" }],
    messages: [], conversationMessages: {}, isConnected: true, isStreaming: false, scheduledTasks: [otherTask],
    draft: "Retain the visible draft", rightPanelOpen: false, runtimeSession: null,
    pendingAskUser: null, askUserQueue: [], pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [],
  });
});
afterEach(() => {
  cleanup(); registerWebSocketSender(null); rejectAllPendingCommandResults("Test ended");
  closeThreadAutomationEditor(task.id); vi.unstubAllGlobals();
});

it("reopens the actual heartbeat when its execution directory differs from the unchanged visible workspace", async () => {
  render(<ChatActionsButton />);
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({ type: "scheduler.list", owner_conversation_id: "A", workspace_root: executionRoot });
  await act(async () => deliver(requests[0]));
  fireEvent.click(await screen.findByRole("menuitem", { name: "编辑定时任务…", exact: true }));
  expect(screen.queryByRole("menuitem", { name: "添加定时任务…", exact: true })).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "A", workingDirectory: "C:/visible",
    rightStackTab: `automation:${task.id}`, draft: "Retain the visible draft", scheduledTasks: [otherTask] });
});

it("returns the captured list ACK after switching to B without replacing B's global inventory or draft", async () => {
  const pending = loadThreadAutomations(owner);
  useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", draft: "Retain B draft" });
  deliver(requests[0]);
  expect(await pending).toEqual([task]);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", workingDirectory: "C:/B",
    draft: "Retain B draft", scheduledTasks: [otherTask] });
});

it("reopens the same task from its new target chat while retaining its local draft title and management root", () => {
  const crossTask = { ...task, id: "cross-target-task", conversation_id: "B" };
  const State = () => <output data-testid="automation-state">{JSON.stringify(useThreadAutomationEditors().filter(editor => editor.id === crossTask.id))}</output>;
  render(<State />);
  act(() => {
    openThreadAutomationEditor(owner, crossTask);
    renameThreadAutomationEditor(crossTask.id, "Retain this unsaved task title");
    closeThreadAutomationEditor(crossTask.id);
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", draft: "Retain B main draft" });
    openThreadAutomationEditor({ id: "B", title: "Chat B", workspaceRoot: "C:/B", permissionMode: "confirm" }, crossTask);
  });
  const editors = JSON.parse(screen.getByTestId("automation-state").textContent!);
  expect(editors).toHaveLength(1);
  expect(editors[0]).toMatchObject({ id: crossTask.id, open: true, title: "Retain this unsaved task title",
    owner: { id: "B", workspaceRoot: "C:/B" }, task: { id: crossTask.id, workspace_root: executionRoot, conversation_id: "B", name: task.name } });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", workingDirectory: "C:/B", draft: "Retain B main draft",
    rightStackTab: `automation:${crossTask.id}` });
  act(() => removeThreadAutomationEditor(crossTask.id));
});
