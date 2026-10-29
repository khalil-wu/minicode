/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "./index";
import { composerWorkspaceKey, emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests } from "./composer-drafts";
import { emptyConversationWorkbenchState } from "./ui-slice";
import { handleSessionEvent } from "../chat/sessionEvents";
import { sendChatMessage, resetPendingUserMessagesForTests } from "../chat/sendChatMessage";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { latestProjectionCommandIds } from "../protocol/command-projection";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";

const wire = vi.hoisted(() => ({ messages: [] as Record<string, unknown>[], modes: new Map<string, "build" | "plan">(), listeners: new Set<(data: unknown) => void>() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({
  send: (message: Record<string, unknown>) => { wire.messages.push(message); return true; }, sessionId: "mode-chain",
  subscribe: (listener: (data: unknown) => void) => { wire.listeners.add(listener); return () => wire.listeners.delete(listener); },
}) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: vi.fn(),
  commandResultSucceeded: (result: { level: string }) => !["error", "failed"].includes(result.level), sendConversationDeleteCommand: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  fetchDraftModelSelection: vi.fn(async (root: string) => ({ provider: "custom", current_model: "fixture", model: "fixture",
    available_models: ["fixture"], models_source: "configured", effective_reasoning_effort: "off", available_permission_modes: ["confirm", "auto", "bypass"],
    config_version: "fixture", config_cwd: root, feature_flags: {}, composer_commands: [],
    default_model_selection: { provider: "custom", model: "fixture", reasoning_effort: "off", conversation_id: "", working_directory: root } })),
}));
const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
const meta = (id: string, agentMode: "build" | "plan", revision = 1) => ({ id, title: id, updatedAt: "2026-10-12", agentMode, revision, workspaceRoot: "C:/project" });
const complete = (command: string, revision?: number, level = "success"): CommandResultEvent => ({
  type: "command.result", command: command as CommandResultEvent["command"], level: level as CommandResultEvent["level"], message: "", data: { revision },
});
const delayed = () => {
  let resolve!: (result: CommandResultEvent) => void;
  vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  return (result: CommandResultEvent) => resolve(result);
};
const switchTo = (id: string, agentMode: "build" | "plan", revision = 1, command?: ClientCommand, hydrating = false) => {
  const event = {
  type: "conversation.switched", conversation_id: id,
  is_hydrating: hydrating, context_pending: hydrating,
  client_command_id: command?.client_command_id, client_command_type: command?.type,
  conversation: { id, title: id, agent_mode: agentMode, revision, workspace_root: "C:/project", messages: [] },
  } as never;
  handleSessionEvent(event, buffers); for (const listener of [...wire.listeners]) listener(event);
};

beforeEach(async () => {
  await waitComposerDraftRecovery();
  await resetComposerDraftStorageForTests();
  vi.clearAllMocks();
  vi.mocked(sendClientCommandAwaitResult).mockReset();
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (command, expected) => {
    if (command.type === "conversation.create") wire.modes.set(command.conversation_id!, command.agent_mode === "plan" ? "plan" : "build");
    if (command.type === "conversation.switch") throw new Error("Switch success is a canonical notification, not command.result");
    return { ...complete(expected), data: { conversation_id: "conversation_id" in command ? command.conversation_id : undefined, execution_cwd: "C:/project" } };
  });
  vi.mocked(sendClientCommand).mockImplementation(command => { if (command.type === "conversation.switch") {
    switchTo(command.conversation_id, wire.modes.get(command.conversation_id)!, 1, command, true);
    switchTo(command.conversation_id, wire.modes.get(command.conversation_id)!, 1, command, false);
  } return true; });
  wire.messages.length = 0;
  wire.modes.clear();
  wire.listeners.clear();
  latestProjectionCommandIds.clear();
  resetPendingUserMessagesForTests();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "owner", conversations: [meta("owner", "build"), meta("other", "build")],
    conversationInventoryInstanceId: null, conversationInventoryRevision: 0, workingDirectory: "C:/project", appMode: "cowork",
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    workspaceComposerDrafts: {}, conversationWorkbenchStates: {}, pendingAgentModeChanges: {}, latestAgentModeCommandIds: {},
    pendingPermissionModeChanges: {}, permissionMode: "confirm", composerDraftsReady: true, runtimeSession: null,
    messages: [], conversationMessages: {}, conversationStreaming: {}, conversationAgentStates: {}, sideChats: {},
    isConnected: true, isStreaming: false, pendingApproval: null, pendingDiffReview: null, pendingAskUser: null,
    draft: "Retained text", editorTabs: [], activeTabPath: null, activeEditorPath: null,
    quotedMessage: { id: "quote", role: "assistant", content: "Retained quote" },
    attachments: [{ id: "attachment", name: "note.txt", type: "text/plain", size: 4, status: "ready" }],
  });
});
afterEach(() => { useAppStore.setState({ isConnected: false }); });

it("reopens each conversation's durable mode while preserving its composer and independent permissions", () => {
  switchTo("owner", "plan", 2);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "plan", permissionMode: "confirm", draft: "Retained text", attachments: [{ id: "attachment" }] });
  switchTo("other", "build", 2);
  expect(useAppStore.getState().agentMode).toBe("build");
  switchTo("owner", "plan", 2);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "plan", draft: "Retained text", quotedMessage: { id: "quote" } });
});

it("persists a client homepage mode-only draft and binds it after explicit creation/configuration/canonical switch", async () => {
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: null, conversations: [] });
  const draftId = useAppStore.getState().startConversationDraft({ bindWorkspace: true, workspaceRoot: "C:/project" });
  expect(await useAppStore.getState().setAgentMode("plan")).toBe(true);
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  await flushComposerDrafts();
  await resetComposerDraftStorageForTests(true);
  expect((await loadComposerDrafts()).find(record => record.owner.kind === "draft" && record.owner.id === draftId)?.draft).toMatchObject({ agentMode: "plan", draft: "" });
  const finish = delayed();
  const creation = useAppStore.getState().ensureComposerConversation();
  await vi.waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.create", agent_mode: "plan", activate: false }), "conversation.create"));
  const create = vi.mocked(sendClientCommandAwaitResult).mock.calls.find(([command]) => command.type === "conversation.create")![0];
  const id = create.conversation_id!;
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: draftId, conversationId: null, pendingConversationCreateId: null });
  wire.modes.set(id, "plan");
  finish({ ...complete("conversation.create"), data: { conversation_id: id, execution_cwd: "C:/project", created: true, activated: false } });
  expect(await creation).toBe(id);
  expect(useAppStore.getState().agentMode).toBe("plan");
  expect(useAppStore.getState().conversationId).toBe(id);
  expect(useAppStore.getState().conversationWorkbenchStates[id]).toMatchObject({ agentMode: "plan", draft: "" });
  expect(useAppStore.getState().workspaceComposerDrafts[draftId]).toBeUndefined();
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.map(([command]) => command.type)).toEqual(["conversation.create", "llm.model.set"]);
  expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: id }));
  expect(useAppStore.getState().workspaceComposerDrafts[composerWorkspaceKey("C:/project")]).toBeUndefined();
});

it("starts an ordinary new conversation in build without inheriting the old task's plan selection", async () => {
  useAppStore.setState({ agentMode: "plan" });
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce(complete("conversation.create"));
  expect(await useAppStore.getState().createConversation()).toBe(true);
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ agent_mode: "build" }), "conversation.create");
});

it("keeps independent permission acknowledgements from changing a confirmed conversation mode", async () => {
  const finishMode = delayed();
  const mode = useAppStore.getState().setAgentMode("plan");
  finishMode(complete("conversation.agent_mode.set", 2));
  expect(await mode).toBe(true);
  const finishPermission = delayed();
  const permission = useAppStore.getState().setPermissionMode("bypass");
  finishPermission(complete("conversation.permission_mode.set"));
  expect(await permission).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "plan", permissionMode: "bypass", draft: "Retained text" });
});

it("routes a late mode receipt to its hidden owner without replacing the foreground composer", async () => {
  const finish = delayed();
  const selection = useAppStore.getState().setAgentMode("plan");
  switchTo("other", "build");
  useAppStore.getState().setDraft("New owner text");
  finish(complete("conversation.agent_mode.set", 2));
  expect(await selection).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "other", agentMode: "build", draft: "New owner text" });
  expect(useAppStore.getState().conversationWorkbenchStates.owner).toMatchObject({ agentMode: "plan", draft: "Retained text" });
});

it("rejects an older mode receipt after a newer choice has already been confirmed", async () => {
  const finishFirst = delayed();
  const first = useAppStore.getState().setAgentMode("plan");
  const firstId = String(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0].client_command_id);
  const finishSecond = delayed();
  const second = useAppStore.getState().setAgentMode("build");
  finishSecond(complete("conversation.agent_mode.set", 3));
  expect(await second).toBe(true);
  finishFirst(complete("conversation.agent_mode.set", 2));
  expect(await first).toBe(false);
  useAppStore.getState().syncConversationAgentMode("plan", "owner", firstId, 2);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "build", pendingAgentModeChanges: {} });
  expect(useAppStore.getState().conversations.find(item => item.id === "owner")).toMatchObject({ agentMode: "build", revision: 3 });
});

it("restores the confirmed owner selection after a real refusal and preserves authored input", async () => {
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce(complete("conversation.agent_mode.set", undefined, "error"));
  expect(await useAppStore.getState().setAgentMode("plan")).toBe(false);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "build", permissionMode: "confirm", draft: "Retained text", quotedMessage: { id: "quote" } });
});

it("projects changes to hidden composers from the authoritative inventory before a background send", () => {
  useAppStore.setState({ conversationWorkbenchStates: { other: { ...emptyConversationWorkbenchState(), agentMode: "build", draft: "Hidden text" } } });
  handleSessionEvent({ type: "conversation.list", active_conversation_id: "owner", conversations: [
    { id: "owner", title: "owner", agent_mode: "build", revision: 1, workspace_root: "C:/project" },
    { id: "other", title: "other", agent_mode: "plan", revision: 2, workspace_root: "C:/project" },
  ] } as never, buffers);
  expect(useAppStore.getState().conversationWorkbenchStates.other).toMatchObject({ agentMode: "plan", draft: "Hidden text" });
  expect(sendChatMessage({ conversationId: "other", displayContent: "Continue hidden work" })).toBe(true);
  expect(wire.messages[0]).toMatchObject({ conversation_id: "other", agent_mode: "plan" });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "owner", agentMode: "build", draft: "Retained text" });
});

it("uses an explicitly supplied turn mode for its owner without borrowing the foreground mode", () => {
  useAppStore.setState({ agentMode: "plan" });
  expect(sendChatMessage({ conversationId: "other", agentMode: "review", displayContent: "Review hidden work" })).toBe(true);
  expect(wire.messages[0]).toMatchObject({ conversation_id: "other", agent_mode: "review" });
  expect(useAppStore.getState().agentMode).toBe("plan");
});
