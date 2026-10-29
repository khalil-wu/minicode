/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { emptyComposerDraft, composerWorkspaceKey } from "./composer-drafts";
import { clearEditorWorkspaceBufferCacheForTests } from "./shared-helpers";
import { handleSessionEvent } from "../chat/sessionEvents";
import { sendChatMessage, resetPendingUserMessagesForTests } from "../chat/sendChatMessage";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { latestProjectionCommandIds } from "../protocol/command-projection";
import { isWorkspaceConversation, groupByWorkspace } from "../shell/ConversationsTab";
import type { CommandResultEvent } from "../protocol/events";

const native = vi.hoisted(() => ({ get: vi.fn(), trust: vi.fn(), wire: [] as Record<string, unknown>[], listeners: new Set<(data: unknown) => void>() }));
vi.mock("../desktop/runtime", async importOriginal => ({
  ...await importOriginal<typeof import("../desktop/runtime")>(),
  desktop: () => ({ preferences: { get: native.get }, trustWorkspace: native.trust }),
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ send: (command: Record<string, unknown>) => { native.wire.push(command); return true; }, sessionId: "default-task",
  subscribe: (listener: (data: unknown) => void) => { native.listeners.add(listener); return () => native.listeners.delete(listener); },
}) }));
vi.mock("../protocol/ws-outbox", () => ({ createClientCommandId: vi.fn(() => "task-chain-command"), sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: vi.fn(), commandResultSucceeded: (result: {level:string}) => result.level === "success", sendConversationDeleteCommand: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), fetchDraftModelSelection: vi.fn(async (root: string) => ({
  provider: "custom", current_model: "fixture", model: "fixture", available_models: ["fixture"], models_source: "configured", config_version: "fixture", config_cwd: root, effective_reasoning_effort: "off",
  default_model_selection: { provider: "custom", model: "fixture", reasoning_effort: "off", conversation_id: "", working_directory: root },
  available_permission_modes: ["confirm", "auto", "bypass"], feature_flags: {}, composer_commands: [],
})) }));
const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
const taskDirectory = "C:/TaskBase/2026-10-13/new-chat";
const activate = (id: string, cwd = taskDirectory, commandId?: string, commandType?: string, hydrating = false) => {
  const event = { type: "conversation.switched", conversation_id: id, is_hydrating: hydrating, context_pending: hydrating,
  client_command_id: commandId, client_command_type: commandType,
  conversation: { id, title: id, updated_at: "2026-10-13", workspace_root: "", execution_cwd: cwd,
    projectless_output_dir: `${cwd}/outputs`, projectless_browser_root: "C:/TaskBase", messages: [] },
  session: { active_conversation_id: id, workspace_root: cwd, permission_mode: "confirm" },
  } as never;
  handleSessionEvent(event, buffers); for (const listener of [...native.listeners]) listener(event);
};
const ok = (): CommandResultEvent => ({ type: "command.result", command: "conversation.create", level: "success", message: "", data: {} });

beforeEach(() => {
  vi.clearAllMocks();
  latestProjectionCommandIds.clear();
  clearEditorWorkspaceBufferCacheForTests();
  resetPendingUserMessagesForTests();
  native.wire.length = 0;
  native.listeners.clear();
  native.get.mockResolvedValue({ defaultTaskDirectory: "C:/TaskBase", defaultPermissionMode: "auto" });
  native.trust.mockResolvedValue(taskDirectory);
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (command, expected) => {
    if (command.type === "conversation.switch") throw new Error("Switch success is a canonical notification, not command.result");
    return { ...ok(), command: expected, data: { conversation_id: "conversation_id" in command ? command.conversation_id : undefined, execution_cwd: taskDirectory } };
  });
  vi.mocked(sendClientCommand).mockImplementation(command => { if (command.type === "conversation.switch") {
    activate(command.conversation_id, taskDirectory, command.client_command_id, command.type, true);
    activate(command.conversation_id, taskDirectory, command.client_command_id, command.type, false);
  } return true; });
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: null, conversations: [], conversationMessages: {},
    conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, conversationAgentStates: {},
    messages: [], sideChats: {}, workingDirectory: "", appMode: "cowork", isConnected: true, isStreaming: false,
    pendingConversationCreateId: null, pendingConversationSwitchId: null,
    editorTabs: [], activeTabPath: null, activeEditorPath: null, permissionMode: "bypass", runtimeSession: null });
});

it("creates a projectless task from the persisted base and default permission without using the base as cwd or membership", async () => {
  expect(await useAppStore.getState().createConversation()).toBe(true);
  const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0];
  expect(command).toMatchObject({ type: "conversation.create", projectless_base: "C:/TaskBase", initial_prompt: "", permission_mode: "auto" });
  expect(command).toMatchObject({ workspace_root: undefined });
  expect(command).not.toHaveProperty("execution_cwd");
});

it("adopts authored input while creation changes the actual cwd and keeps projectless chats outside project groups", async () => {
  useAppStore.setState({ draft: "继续实现这个功能", attachments: [{ id: "draft-image", name: "draft.png", type: "image/png", size: 10, status: "ready" }] });
  let finish!: (result: CommandResultEvent) => void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const create = useAppStore.getState().ensureComposerConversation();
  await vi.waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalled());
  const id = vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0].conversation_id!;
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0]).toMatchObject({ activate: false, initial_prompt: "继续实现这个功能", permission_mode: "auto" });
  useAppStore.getState().setDraft("创建期间继续输入");
  finish({ ...ok(), data: { conversation_id: id, execution_cwd: taskDirectory } });
  expect(await create).toBe(id);
  const state = useAppStore.getState();
  expect(state).toMatchObject({ workingDirectory: taskDirectory, draft: "创建期间继续输入", attachments: [{ id: "draft-image" }] });
  const conversation = state.conversations.find(item => item.id === id)!;
  expect(conversation).toMatchObject({ executionCwd: taskDirectory, projectlessOutputDir: `${taskDirectory}/outputs`, projectlessBrowserRoot: "C:/TaskBase" });
  expect(isWorkspaceConversation(conversation)).toBe(false);
  expect([...groupByWorkspace([{ ...conversation, sessionStatus: "idle" }]).values()].map(group => group.path)).toEqual([""]);
  expect(state.workspaceComposerDrafts[composerWorkspaceKey("")]).toBeUndefined();
});

it("keeps each task's cwd and draft across switching and uses the hidden owner for background sends", () => {
  activate("first");
  useAppStore.getState().setDraft("First task draft");
  activate("second", "C:/TaskBase/2026-10-13/second-task");
  useAppStore.getState().setDraft("Second task draft");
  expect(sendChatMessage({ conversationId: "first", displayContent: "Continue first task" })).toBe(true);
  expect(native.wire[0]).toMatchObject({ conversation_id: "first", workspace_root: taskDirectory });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "second", workingDirectory: "C:/TaskBase/2026-10-13/second-task", draft: "Second task draft" });
  activate("first");
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: taskDirectory, draft: "First task draft" });
});

it("does not promote a projectless execution directory into project membership when opening another chat from Code mode", async () => {
  activate("first");
  useAppStore.setState({ appMode: "code" });
  await useAppStore.getState().createConversation({ bindWorkspace: true, appMode: "cowork" });
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0]).toMatchObject({ projectless_base: "C:/TaskBase" });
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0]).toMatchObject({ workspace_root: undefined });
});

it("retains the current task and draft when preference reading finishes after navigation", async () => {
  let resolvePreferences!: (value: unknown) => void;
  native.get.mockImplementationOnce(() => new Promise(resolve => { resolvePreferences = resolve; }));
  const create = useAppStore.getState().createConversation({ bindWorkspace: false });
  useAppStore.getState().requestConversationSwitch("second");
  activate("second", "C:/TaskBase/2026-10-13/second-task");
  useAppStore.getState().setDraft("Keep selected task draft");
  resolvePreferences({ defaultTaskDirectory: "C:/TaskBase", defaultPermissionMode: "auto" });
  expect(await create).toBe(false);
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "second", draft: "Keep selected task draft" });
});
