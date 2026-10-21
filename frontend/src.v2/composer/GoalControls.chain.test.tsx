/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FooterRow } from "./FooterRow";
import { GoalBar } from "./GoalBar";
import { GoalPanel } from "../panels/GoalPanel";
import { useAppStore } from "../stores";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { showConfirm } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { prepareGoalEditor, readGoalEditor, submitComposerGoal } from "./goal-state";
import { translate } from "../i18n";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import type { CommandResultEvent } from "../protocol/events";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: vi.fn(() => ({
  matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
})) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => null }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: vi.fn(),
  commandResultSucceeded: (result: CommandResultEvent) => result.level !== "error" }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => null, runtime: () => null, openExternal: vi.fn() }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(async () => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("./uploads", () => ({ uploadComposerFiles: vi.fn() }));

let serial = 0;
let owner = "";
const tr = (key: Parameters<typeof translate>[1], values?: Parameters<typeof translate>[2]) => translate("zh-CN", key, values);
const reply = (id: string, text: string, status = "active"): CommandResultEvent => ({ type: "command.result", command: "goal",
  level: "success", message: "Updated", data: { conversation_id: id, revision: 10,
    goal: text ? { text, status, id: `goal-${id}`, updated_at: new Date().toISOString() } : {} } });

beforeEach(() => {
  owner = `goal-controls-${++serial}`;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins: [] }), { status: 200 })));
  useAppStore.setState({ conversationId: owner, workingDirectory: "C:/goal-workspace", draft: "Keep the original draft", goalMode: false,
    attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [], allowedRemoteImageDomains: [],
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, pendingConversationSwitchId: null, activeGoal: null,
    agentMode: "build", permissionMode: "auto", pendingAgentModeChanges: {}, pendingPermissionModeChanges: {}, latestAgentModeCommandIds: {},
    isConnected: true, isStreaming: false, prMonitor: null, prStatusIssue: null, availableModels: [], currentModel: "fixture",
    runtimeCapabilities: null, rightStackTab: "tasks", rightPanelOpen: false, conversations: [] });
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async command => {
    if (command.type !== "conversation.goal.set") return { type: "command.result", command: command.type, level: "success", message: "Updated", data: {} };
    const state = useAppStore.getState();
    const text = command.action === "clear" ? "" : String(command.text || state.activeGoal?.text || "Goal");
    return reply(String(command.conversation_id), text, command.action === "pause" ? "paused" : "active");
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it("opens goal mode in the original composer and keeps all attachment and quote data", () => {
  const attachment = { id: "kept", name: "notes.txt", type: "text/plain", size: 0, status: "ready" as const };
  const quote = { id: "quoted", role: "assistant" as const, content: "Keep this quote" };
  useAppStore.setState({ attachments: [attachment], quotedMessage: quote, agentMode: "plan" });
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: /^目标/ }));
  expect(useAppStore.getState()).toMatchObject({ goalMode: true, agentMode: "build", draft: "Keep the original draft", attachments: [attachment], quotedMessage: quote });
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "清除目标" }));
  expect(useAppStore.getState()).toMatchObject({ goalMode: false, draft: "Keep the original draft", attachments: [attachment], quotedMessage: quote });
});

it("selects plan mode instead of submitting a pending goal and keeps its original content", async () => {
  const attachment = { id: "kept", name: "notes.txt", type: "text/plain", size: 0, status: "ready" as const };
  const quote = { id: "quoted", role: "assistant" as const, content: "Keep this quote" };
  useAppStore.setState({ goalMode: true, attachments: [attachment], quotedMessage: quote });
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: /^计划模式/ }));
  expect(useAppStore.getState()).toMatchObject({ goalMode: false, agentMode: "plan", draft: "Keep the original draft", attachments: [attachment], quotedMessage: quote });
  expect(screen.queryByRole("button", { name: "清除目标" })).toBeNull();
  expect(screen.getByRole("button", { name: "关闭计划模式" })).toBeTruthy();
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.goal.set" }), "goal");
  await waitFor(() => expect(useAppStore.getState().pendingAgentModeChanges).toEqual({}));
});

it("clears the saved goal through its original owner when plan is selected and preserves a different task on late receipt", async () => {
  let finishClear!: (result: CommandResultEvent) => void;
  const original = owner;
  const saved = { text: "Original saved goal", status: "active" };
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(command => command.type === "conversation.goal.set"
    ? new Promise(resolve => { finishClear = resolve; })
    : Promise.resolve({ type: "command.result", command: command.type, level: "success", message: "Updated", data: {} }));
  useAppStore.setState({ activeGoal: saved, goalMode: true });
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: /^计划模式/ }));
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.goal.set", conversation_id: original, action: "clear" }), "goal");
  expect(useAppStore.getState()).toMatchObject({ activeGoal: saved, goalMode: false, agentMode: "plan", draft: "Keep the original draft" });
  act(() => useAppStore.setState({ conversationId: "another-goal-owner", draft: "New owner draft", agentMode: "build", activeGoal: { text: "Another task goal", status: "active" } }));
  await act(async () => finishClear(reply(original, "")));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "another-goal-owner", activeGoal: { text: "Another task goal" }, draft: "New owner draft", agentMode: "build" });
});

it("shows the actual saved-goal clear failure instead of pretending the goal was removed", async () => {
  const saved = { text: "Saved goal", status: "active" };
  useAppStore.setState({ activeGoal: saved });
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async command => command.type === "conversation.goal.set"
    ? { type: "command.result", command: "goal", level: "error", message: "Actual goal clear rejected", data: {} }
    : { type: "command.result", command: command.type, level: "success", message: "Updated", data: {} });
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: /^计划模式/ }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Actual goal clear rejected", "error"));
  expect(useAppStore.getState()).toMatchObject({ activeGoal: saved, draft: "Keep the original draft", goalMode: false, agentMode: "plan" });
});

it("submits the captured goal owner and leaves a newly edited draft and all attachments intact", async () => {
  let complete!: (result: CommandResultEvent) => void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  const attachment = { id: "kept", name: "notes.txt", type: "text/plain", size: 0, status: "ready" as const };
  useAppStore.setState({ goalMode: true, attachments: [attachment] });
  const pending = submitComposerGoal(tr);
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: owner, text: "Keep the original draft" }), "goal");
  const originalOwner = owner;
  useAppStore.getState().snapshotWorkbenchState();
  useAppStore.setState({ conversationId: "other-goal-owner", draft: "New draft after navigation", goalMode: false,
    attachments: [{ ...attachment, id: "other" }], activeGoal: null });
  complete(reply(originalOwner, "Keep the original draft"));
  await pending;
  expect(useAppStore.getState()).toMatchObject({ conversationId: "other-goal-owner", draft: "New draft after navigation", attachments: [{ id: "other" }] });
  expect(useAppStore.getState().conversationWorkbenchStates[originalOwner]).toMatchObject({ draft: "", goalMode: false, attachments: [attachment] });
});

it("requires the source replacement confirmation and retains the composer on rejection", async () => {
  useAppStore.setState({ goalMode: true, activeGoal: { text: "The saved goal", status: "active" } });
  vi.mocked(showConfirm).mockResolvedValueOnce(false);
  await submitComposerGoal(tr);
  expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "替换当前目标？", confirmLabel: "替换目标" }));
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ draft: "Keep the original draft", goalMode: true });
});

it.each(["budget_limited", "complete"])("does not offer a nonexistent resume control for %s", status => {
  useAppStore.setState({ activeGoal: { text: "Actual goal", status } });
  render(<GoalBar />);
  expect(screen.queryByRole("button", { name: "继续目标" })).toBeNull();
  expect(screen.getByRole("button", { name: "清除目标" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "编辑目标" })).toBeTruthy();
});

it("edits in the actual goal panel, reverts locally, and saves through the owner-bound API", async () => {
  const goal = { text: "Saved objective", status: "active", updatedAt: new Date().toISOString() };
  useAppStore.setState({ activeGoal: goal });
  prepareGoalEditor(owner, goal);
  const close = vi.fn();
  render(<GoalPanel active onClose={close} />);
  const editor = screen.getByRole("textbox", { name: "目标" });
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(editor, { target: { value: "Unsaved objective" } });
  fireEvent.click(screen.getByRole("button", { name: "还原目标" }));
  expect((editor as HTMLTextAreaElement).value).toBe("Saved objective");
  fireEvent.change(editor, { target: { value: "Updated objective" } });
  fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
  await waitFor(() => expect(readGoalEditor(owner)).toMatchObject({ savedObjective: "Updated objective", busy: null }));
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: owner, action: "set", text: "Updated objective" }), "goal");
  expect(useAppStore.getState().activeGoal?.text).toBe("Updated objective");
  expect(close).not.toHaveBeenCalled();
});

it("keeps an unsaved goal editor draft when the source objective changes and its tab closes", async () => {
  const goal = { text: "Saved objective", status: "active" };
  useAppStore.setState({ activeGoal: goal });
  prepareGoalEditor(owner, goal);
  const close = vi.fn();
  render(<GoalPanel active onClose={close} />);
  fireEvent.change(screen.getByRole("textbox", { name: "目标" }), { target: { value: "My unsaved goal edit" } });
  act(() => useAppStore.setState({ activeGoal: { text: "External objective", status: "active" } }));
  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(readGoalEditor(owner)?.draft).toBe("My unsaved goal edit");
});

it("allows reopening an already completed goal and closes only when an open goal becomes complete", () => {
  const goal = { text: "Saved completed objective", status: "complete" };
  useAppStore.setState({ activeGoal: goal });
  prepareGoalEditor(owner, goal);
  const close = vi.fn();
  render(<GoalPanel active onClose={close} />);
  expect((screen.getByRole("textbox", { name: "目标" }) as HTMLTextAreaElement).value).toBe(goal.text);
  expect(close).not.toHaveBeenCalled();
  act(() => useAppStore.setState({ activeGoal: { ...goal, status: "active" } }));
  expect(close).not.toHaveBeenCalled();
  act(() => useAppStore.setState({ activeGoal: goal }));
  expect(close).toHaveBeenCalledOnce();
});

it("restores goal mode with the correct hidden task draft", () => {
  useAppStore.setState({ goalMode: true, draft: "Goal A", attachments: [{ id: "kept", name: "notes.txt", type: "text/plain", size: 0, status: "ready" }] });
  useAppStore.getState().snapshotWorkbenchState();
  useAppStore.setState({ conversationId: "other-goal-owner", ...emptyConversationWorkbenchState(), draft: "Ordinary B" });
  useAppStore.getState().restoreWorkbenchState(owner);
  expect(useAppStore.getState()).toMatchObject({ goalMode: true, draft: "Goal A", attachments: [{ id: "kept" }] });
});
