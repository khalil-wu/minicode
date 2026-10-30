/* @vitest-environment jsdom */
import { File as NativeFile } from "node:buffer";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "./index";
import { discardConversationDraftsForOwner, invalidateConversationDraftSelection } from "./conversation-drafts";
import { emptyConversationWorkbenchState } from "./ui-slice";
import type { AppStore } from "./types";
import { collectComposerDrafts, deleteConversationComposerDrafts, emptyComposerDraft, flushComposerDrafts,
  loadComposerDrafts, persistComposerDrafts, resetComposerDraftStorageForTests } from "./composer-drafts";
import { fetchDraftModelSelection, type DraftModelSelection } from "../protocol/api";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { handleSessionEvent } from "../chat/sessionEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleChatStreamEvent } from "../chat/chatStreamEvents";
import { handleControlPlaneProjectionEvent } from "../chat/controlPlaneEvents";
import { enterComposerGoalMode } from "../composer/goal-state";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";
import { latestProjectionCommandIds, rememberSentCommandProjection } from "../protocol/command-projection";
import { sendChatMessage, resetPendingUserMessagesForTests } from "../chat/sendChatMessage";
import { LS, writeLS } from "./shared-helpers";
import { getDiffCommentScope, readDiffCommentState, setDiffComments } from "../lib/diff-comment-storage";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import type { ClientCommand, CommandResultEvent, ServerEvent } from "../protocol/events";
import { draftBrowserOwnerId, readBrowserWorkbench, writeBrowserWorkbench } from "./browser-workbench";

const wire = vi.hoisted(() => ({ rpc: vi.fn(), sent: [] as ClientCommand[], order: [] as string[], toast: vi.fn(),
  listeners: new Set<(data: unknown) => void>(), stops: [] as ReturnType<typeof vi.fn>[] }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), fetchDraftModelSelection: vi.fn() }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: wire.rpc }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({
  sessionId: "local-draft-window", send: (command: ClientCommand) => { wire.sent.push(command); wire.order.push(command.type); return true; },
  subscribe: (listener: (data: unknown) => void) => { wire.listeners.add(listener); const stop = vi.fn(() => { wire.listeners.delete(listener); }); wire.stops.push(stop); return stop; },
}) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));
const buffers = { textStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() } } as never;
const root = "C:/same-project";
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const selection = (workspaceRoot: string, model = "", effort = "", configCwd = workspaceRoot): DraftModelSelection => ({
  provider: "custom", current_model: model || "fixture", model: model || "fixture", available_models: ["fixture", "other"], models_source: "configured",
  available_model_options: ["fixture", "other"].map(model => ({ model, reasoning_effort_levels: ["low", "medium", "high"], default_reasoning_effort: "medium" })),
  effective_reasoning_effort: effort || "medium", model_selection_mode: model ? "model" : "default",
  default_model_selection: { provider: "custom", model: "fixture", reasoning_effort: "medium", conversation_id: "", working_directory: workspaceRoot },
  available_permission_modes: ["confirm", "auto", "bypass"], feature_flags: {}, composer_commands: [],
  config_version: "fixture-config", config_cwd: configCwd,
});
const ok = (command: string, data: Record<string, unknown> = {}): CommandResultEvent => ({ type: "command.result", command, level: "success", message: "", data });
const receive = (event: ServerEvent) => {
  if (event.type === "command.result") handleCommandResultEvent(event);
  else if (!handleSessionEvent(event, buffers) && !handleControlPlaneProjectionEvent(event)) {
    handleChatStreamEvent(event, event.conversation_id || undefined, buffers);
  }
  for (const listener of [...wire.listeners]) listener(event);
};
const switched = (id: string, command?: ClientCommand, cwd = root, content = "", hydrating = false) => receive({
  type: "conversation.switched", conversation_id: id, client_command_id: command?.client_command_id, client_command_type: command?.type,
  is_hydrating: hydrating, context_pending: hydrating,
  conversation: { id, title: id, workspace_root: cwd, execution_cwd: cwd, revision: 2,
    messages: content ? [{ id: `message-${id}`, role: "user", content, timestamp: 2000 }] : [] },
  session: { active_conversation_id: id, workspace_root: cwd, active_stream_conversation_ids: [] },
} as ServerEvent);
const defaultRpc = async (command: ClientCommand, expected: string): Promise<CommandResultEvent> => {
  wire.order.push(command.type);
  if (command.type === "conversation.switch") throw new Error("Switch success is a canonical notification, not command.result");
  if (command.type === "conversation.create") return ok(expected, { conversation_id: command.conversation_id, execution_cwd: root, created: true, activated: false });
  return ok(expected, { conversation_id: "conversation_id" in command ? command.conversation_id : undefined });
};
const defaultSend = (command: ClientCommand) => {
  if (command.type === "conversation.switch") {
    wire.order.push(command.type);
    switched(command.conversation_id, command, root, "", true);
    switched(command.conversation_id, command);
  }
  return true;
};
const currentDraft = () => useAppStore.getState().startConversationDraft({ workspaceRoot: root, bindWorkspace: true });
const commandOfType = (type: string) => [...vi.mocked(sendClientCommandAwaitResult).mock.calls, ...vi.mocked(sendClientCommand).mock.calls]
  .find(([command]) => command.type === type)![0];
const trackStateStop = () => {
  const subscribe = useAppStore.subscribe;
  const stopped = vi.fn();
  vi.spyOn(useAppStore, "subscribe").mockImplementation(((listener: (state: AppStore, previous: AppStore) => void) => {
    const stop = subscribe(listener);
    return () => { stopped(); stop(); };
  }) as typeof useAppStore.subscribe);
  return stopped;
};
const expectWaiterReleased = (stopState: ReturnType<typeof vi.fn>) => {
  expect(wire.listeners.size).toBe(0);
  expect(wire.stops).toHaveLength(1); expect(wire.stops[0]).toHaveBeenCalledTimes(1);
  expect(stopState).toHaveBeenCalledTimes(1);
};

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.stubGlobal("File", NativeFile);
  vi.clearAllMocks(); wire.sent = []; wire.order = []; latestProjectionCommandIds.clear(); resetPendingUserMessagesForTests();
  wire.rpc.mockReset(); vi.mocked(sendClientCommand).mockReset();
  wire.listeners.clear(); wire.stops = [];
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
  localStorage.clear();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: null, activeConversationDraftId: null,
    conversationDrafts: {}, homeConversationDraftIds: {}, conversations: [], conversationMessages: {}, conversationStreaming: {},
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, conversationAgentStates: {}, sideChats: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null, conversationInventoryInstanceId: null,
    conversationInventoryRevision: 0, messages: [], workingDirectory: root, appMode: "cowork", isConnected: true, isStreaming: false,
    composerDraftsReady: true, runtimeSession: null, runtimeCapabilities: null, currentModel: "fixture", currentProvider: "custom",
    permissionMode: "confirm", pendingPermissionModeChanges: {}, pendingApproval: null, approvalQueue: [], pendingAskUser: null,
    askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], editorTabs: [], activeTabPath: null, activeEditorPath: null });
  vi.mocked(fetchDraftModelSelection).mockImplementation(async (workspace, model, effort, _signal, configCwd) => selection(workspace, model, effort, configCwd));
  wire.rpc.mockImplementation(defaultRpc);
  vi.mocked(sendClientCommand).mockImplementation(defaultSend);
});
afterEach(async () => {
  useAppStore.setState({ isConnected: false });
  expect(wire.listeners.size).toBe(0);
  expect(wire.stops.every(stop => stop.mock.calls.length === 1)).toBe(true);
  resetPendingUserMessagesForTests();
  await flushComposerDrafts(); await resetComposerDraftStorageForTests();
  vi.unstubAllGlobals(); delete window.__MINICODE_RUNTIME__;
  vi.restoreAllMocks();
});

it("opens and reuses a local draft with zero create, while the real owner's messages, input and run stay owned", async () => {
  useAppStore.setState({ conversationId: "old", conversations: [{ id: "old", title: "old", workspaceRoot: root }],
    draft: "old unsent input", messages: [{ id: "old-user", role: "user", content: "running task", artifacts: [], timestamp: 1 }],
    isStreaming: true, runtimeSession: { active_conversation_id: "old", active_stream_conversation_ids: ["old"] } });
  const id = currentDraft();
  await useAppStore.getState().refreshConversationDraftSelection(id);
  useAppStore.getState().setDraft("new local input");
  expect(currentDraft()).toBe(id);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: id, conversationId: null, messages: [], draft: "new local input",
    conversationStreaming: { old: true }, conversationMessages: { old: [{ id: "old-user" }] }, runtimeSession: { active_conversation_id: "old" } });
  expect(useAppStore.getState().conversationWorkbenchStates.old.draft).toBe("old unsent input");
  expect(useAppStore.getState().conversations.map(item => item.id)).toEqual(["old"]);
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
});

it("moves an authored ownerless body once without recreating its legacy cwd alias during selection", async () => {
  useAppStore.getState().setDraft("authored before local draft normalization");
  const id = currentDraft();
  expect(useAppStore.getState().draft).toBe("authored before local draft normalization");
  expect(Object.keys(useAppStore.getState().workspaceComposerDrafts)).toEqual([id]);
  await flushComposerDrafts();
  expect((await loadComposerDrafts()).map(record => [record.owner.kind, record.owner.id, record.draft.draft]))
    .toEqual([["draft", id, "authored before local draft normalization"]]);
  const owner = await useAppStore.getState().materializeConversationDraft(id);
  expect(useAppStore.getState().workspaceComposerDrafts).toEqual({});
  expect(useAppStore.getState().conversationWorkbenchStates[owner!].draft).toBe("authored before local draft normalization");
});

it("deduplicates resource creation and confirms explicit owner settings before the first actual user_message", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("first prompt");
  await useAppStore.getState().refreshConversationDraftSelection(id);
  await useAppStore.getState().setPermissionMode("auto");
  const gate = deferred<CommandResultEvent>();
  wire.rpc.mockImplementationOnce((command: ClientCommand) => { wire.order.push(command.type); return gate.promise; });
  const sent = sendChatMessage({ displayContent: "first prompt" });
  await vi.waitFor(() => expect(commandOfType("conversation.create")).toBeTruthy());
  const create = commandOfType("conversation.create");
  expect(create).toMatchObject({ activate: false, permission_mode: "auto" });
  expect(wire.sent).toEqual([]);
  const duplicate = useAppStore.getState().materializeConversationDraft(id);
  gate.resolve(ok("conversation.create", { conversation_id: create.conversation_id, execution_cwd: root }));
  expect(await sent, JSON.stringify(wire.toast.mock.calls)).toBe(true); expect(await duplicate).toBe(create.conversation_id);
  expect(wire.order).toEqual(["conversation.create", "llm.model.set", "conversation.switch", "user_message"]);
  expect(wire.sent[0]).toMatchObject({ type: "user_message", conversation_id: create.conversation_id, content: "first prompt" });
  expect(useAppStore.getState().workspaceComposerDrafts[id]).toBeUndefined();
  expect(useAppStore.getState().conversationWorkbenchStates[create.conversation_id!].draft).toBe("first prompt");
});

it("shares the projectless preference read with model selection before creating its owner", async () => {
  const get = vi.fn(async () => ({ defaultTaskDirectory: "C:/tasks", defaultPermissionMode: "confirm", defaultWorkspaceFullView: false }));
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: { preferences: { get } } } as never;
  const id = useAppStore.getState().startConversationDraft({ bindWorkspace: false });
  await useAppStore.getState().refreshConversationDraftSelection(id);
  const readsBeforeSubmit = get.mock.calls.length;
  let readsAtCreate = 0;
  wire.rpc.mockImplementation(async (command: ClientCommand, expected: string) => {
    if (command.type === "conversation.create") readsAtCreate = get.mock.calls.length;
    return defaultRpc(command, expected);
  });
  expect(await useAppStore.getState().materializeConversationDraft(id)).toBeTruthy();
  // Selection and creation consume the same preference snapshot for this send.
  expect(readsAtCreate - readsBeforeSubmit).toBe(1);
  expect(wire.order).toEqual(["conversation.create", "llm.model.set", "conversation.switch"]);
});

it.each([true, false])("keeps the selected draft capabilities when its owner is adopted (project=%s)", async bindWorkspace => {
  vi.mocked(fetchDraftModelSelection).mockImplementation(async (workspace, model, effort, _signal, configCwd) => ({
    ...selection(workspace, model, effort, configCwd), feature_flags: { goals: { enabled: true } },
    composer_commands: [{ name: "draft-command", command: "draft-command", description: "Draft action", type: "local" }],
  }));
  const id = bindWorkspace ? currentDraft() : useAppStore.getState().startConversationDraft({ bindWorkspace: false });
  await useAppStore.getState().refreshConversationDraftSelection(id);
  useAppStore.setState({ scheduledTasks: [{ id: "old-owner-task" }] as never, scheduledTaskRuns: [{ id: "old-owner-run" }] as never });
  expect(useAppStore.getState().slashCommands.map(command => command.name)).toContain("draft-command");
  const owner = await useAppStore.getState().materializeConversationDraft(id);
  expect(useAppStore.getState().conversationId).toBe(owner);
  expect(capabilityFeatureEnabled(useAppStore.getState().runtimeCapabilities, "goals")).toBe(true);
  expect(useAppStore.getState().slashCommands.map(command => command.name)).toContain("draft-command");
  if (!bindWorkspace) expect(useAppStore.getState()).toMatchObject({ scheduledTasks: [], scheduledTaskRuns: [] });
});

it.each(["default", "model"] as const)("applies %s model selection atomically without a second effort override", async mode => {
  const id = currentDraft();
  await useAppStore.getState().refreshConversationDraftSelection(id);
  if (mode === "model") {
    expect(await useAppStore.getState().setModelSelection({ mode: "model", model: "other" })).toBe(true);
    expect(await useAppStore.getState().setEffortLevel("high")).toBe(true);
  }
  const catalogReads = vi.mocked(fetchDraftModelSelection).mock.calls.length;
  expect(await useAppStore.getState().materializeConversationDraft(id)).toBeTruthy();
  expect(vi.mocked(fetchDraftModelSelection).mock.calls.length - catalogReads).toBe(mode === "model" ? 2 : 0);
  const command = commandOfType("llm.model.set");
  expect(command).toMatchObject({ selection_mode: mode });
  if (mode === "model") expect(command).toMatchObject({ model: "other", reasoning_effort: "high" });
  else {
    expect(command).not.toHaveProperty("model");
    expect(command).not.toHaveProperty("reasoning_effort");
  }
  expect(wire.order).not.toContain("llm.config.set");
});

it("joins a real invalidation while a ready default draft waits for its creation preferences", async () => {
  const id = currentDraft();
  await useAppStore.getState().refreshConversationDraftSelection(id);
  useAppStore.getState().setDraft("submitted body");
  const preferences = deferred<{ defaultTaskDirectory: string; defaultPermissionMode: string; defaultWorkspaceFullView: boolean }>();
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: {
    preferences: { get: () => preferences.promise },
  } } as never;
  const fresh = deferred<DraftModelSelection>();
  vi.mocked(fetchDraftModelSelection).mockImplementationOnce(() => fresh.promise);
  const reads = vi.mocked(fetchDraftModelSelection).mock.calls.length;
  const materialized = useAppStore.getState().materializeConversationDraft(id);
  handleCommandCatalogEvent({ type: "skills/changed" } as ServerEvent);
  useAppStore.getState().setDraft("new input while preparation waits");
  preferences.resolve({ defaultTaskDirectory: "C:/tasks", defaultPermissionMode: "confirm", defaultWorkspaceFullView: false });
  await preferences.promise;
  await flushComposerDrafts();
  expect(useAppStore.getState().conversationDrafts[id].requestedConversationId).toBeUndefined();
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  fresh.resolve({ ...selection(root), config_version: "latest-settings" });
  const owner = await materialized;
  expect(owner).toBeTruthy();
  expect(vi.mocked(fetchDraftModelSelection).mock.calls.length - reads).toBe(1);
  expect(useAppStore.getState().conversationDrafts[id].modelProjection?.config_version).toBe("latest-settings");
  expect(commandOfType("llm.model.set")).toMatchObject({ conversation_id: owner, selection_mode: "default" });
  expect(wire.order).toEqual(["conversation.create", "llm.model.set", "conversation.switch"]);
  expect(useAppStore.getState()).toMatchObject({ conversationId: owner, draft: "new input while preparation waits" });
});

it("refreshes a failed footer projection before materializing instead of reusing its older success", async () => {
  const id = currentDraft();
  await useAppStore.getState().refreshConversationDraftSelection(id);
  const original = useAppStore.getState().conversationDrafts[id].modelProjection;
  vi.mocked(fetchDraftModelSelection).mockRejectedValueOnce(new Error("actual catalog refusal"));
  expect(await useAppStore.getState().refreshConversationDraftSelection(id)).toBe(false);
  expect(useAppStore.getState().conversationDrafts[id]).toMatchObject({
    modelProjection: original, selectionLoading: false, selectionError: "actual catalog refusal",
  });
  const reads = vi.mocked(fetchDraftModelSelection).mock.calls.length;
  expect(await useAppStore.getState().materializeConversationDraft(id)).toBeTruthy();
  expect(vi.mocked(fetchDraftModelSelection).mock.calls.length - reads).toBe(1);
  expect(commandOfType("llm.model.set")).toMatchObject({ selection_mode: "default" });
  expect(useAppStore.getState().conversationDrafts[id].selectionError).toBeNull();
});

it("moves browser ownership after real creation/configuration and before switching or sending the first message", async () => {
  const transfer = vi.fn(async () => { wire.order.push("browser.transfer"); return 1; });
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: {
    preferences: { get: async () => ({ defaultTaskDirectory: "C:/tasks", defaultPermissionMode: "confirm", defaultWorkspaceFullView: false }) },
    embeddedBrowser: { transferOwner: transfer },
  } } as never;
  const id = currentDraft(); useAppStore.getState().setDraft("send with browser");
  const fromOwner = draftBrowserOwnerId(id);
  writeBrowserWorkbench(fromOwner, { activeId: "live-page", tabs: [{ id: "live-page", title: "Existing page", url: "https://example.com/", draftUrl: "unsent address" }],
    layout: { rightPanelOpen: true, rightPanelExpanded: false, rightStackTab: "browser", rightStackTabLocked: false } });
  expect(await sendChatMessage({ displayContent: "send with browser" })).toBe(true);
  const owner = commandOfType("conversation.create").conversation_id!;
  expect(transfer).toHaveBeenCalledExactlyOnceWith({ fromOwner, toOwner: owner });
  expect(wire.order).toEqual(["conversation.create", "llm.model.set", "browser.transfer", "conversation.switch", "user_message"]);
  expect(readBrowserWorkbench(fromOwner)).toBeNull();
  expect(readBrowserWorkbench(owner)?.tabs[0]).toMatchObject({ id: "live-page", draftUrl: "unsent address" });
  expect(useAppStore.getState().conversationWorkbenchStates[owner]).toMatchObject({ rightPanelOpen: true, rightStackTab: "browser" });
});

it("continues a submitted A under its real owner after opening same-cwd draft B, without switching or clearing B", async () => {
  const a = currentDraft(); useAppStore.getState().setDraft("A prompt");
  const gate = deferred<CommandResultEvent>();
  wire.rpc.mockImplementationOnce((command: ClientCommand) => { wire.order.push(command.type); return gate.promise; });
  const sent = sendChatMessage({ displayContent: "A prompt" });
  await vi.waitFor(() => expect(commandOfType("conversation.create")).toBeTruthy());
  const create = commandOfType("conversation.create");
  const b = currentDraft(); expect(b).not.toBe(a); useAppStore.getState().setDraft("B remains editable");
  gate.resolve(ok("conversation.create", { conversation_id: create.conversation_id, execution_cwd: root }));
  expect(await sent).toBe(true);
  expect(wire.order).not.toContain("conversation.switch");
  expect(wire.sent[0]).toMatchObject({ conversation_id: create.conversation_id });
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: b, conversationId: null, draft: "B remains editable", messages: [] });
  expect(useAppStore.getState().conversationWorkbenchStates[create.conversation_id!].draft).toBe("A prompt");
});

it("retains the real binding and body after configuration refusal and retries without a second create", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("keep this prompt");
  let refuse = true;
  wire.rpc.mockImplementation(async (command: ClientCommand, expected: string) => {
    if (command.type === "llm.model.set" && refuse) { wire.order.push(command.type); return { ...ok(expected), level: "error", message: "actual config refusal" }; }
    return defaultRpc(command, expected);
  });
  expect(await useAppStore.getState().materializeConversationDraft(id)).toBeNull();
  const owner = useAppStore.getState().conversationDrafts[id].conversationId;
  expect(owner).toBeTruthy(); expect(useAppStore.getState().workspaceComposerDrafts[id]).toBeDefined();
  expect(wire.order).not.toContain("conversation.switch"); expect(wire.sent).toEqual([]);
  refuse = false;
  await useAppStore.getState().setPermissionMode("auto");
  expect(await useAppStore.getState().materializeConversationDraft(id)).toBe(owner);
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.filter(([command]) => command.type === "conversation.create")).toHaveLength(1);
  expect(commandOfType("conversation.permission_mode.set")).toMatchObject({ conversation_id: owner, mode: "auto" });
  expect(useAppStore.getState()).toMatchObject({ conversationId: owner, draft: "keep this prompt" });
});

it.each(["build", "plan"] as const)("freezes %s mode across creation and canonical hydration, then unlocks explicit owner edits", async initialMode => {
  vi.mocked(fetchDraftModelSelection).mockImplementation(async (workspace, model, effort, _signal, configCwd) => ({
    ...selection(workspace, model, effort, configCwd), feature_flags: { goals: { enabled: true } },
  }));
  const id = currentDraft(); await useAppStore.getState().setAgentMode(initialMode);
  useAppStore.getState().setDraft("submitted before mode change");
  const createGate = deferred<CommandResultEvent>();
  wire.rpc.mockImplementation((command: ClientCommand, expected: string) => {
    if (command.type === "conversation.create") { wire.order.push(command.type); return createGate.promise; }
    return defaultRpc(command, expected);
  });
  vi.mocked(sendClientCommand).mockImplementation(command => {
    if (command.type === "conversation.switch") { wire.order.push(command.type); switched(command.conversation_id, command, root, "", true); }
    return true;
  });
  const firstSend = sendChatMessage({ displayContent: "submitted before mode change", agentMode: initialMode });
  await vi.waitFor(() => expect(commandOfType("conversation.create")).toBeTruthy());
  const create = commandOfType("conversation.create"), nextMode = initialMode === "build" ? "plan" : "build";
  expect(create).toMatchObject({ agent_mode: initialMode, activate: false });
  expect(await useAppStore.getState().setAgentMode(nextMode)).toBe(false);
  expect(capabilityFeatureEnabled(useAppStore.getState().runtimeCapabilities, "goals")).toBe(true);
  expect(enterComposerGoalMode()).toBe(false);
  expect(useAppStore.getState()).toMatchObject({ agentMode: initialMode, goalMode: false });
  useAppStore.getState().setDraft("new input remains owned");
  createGate.resolve(ok("conversation.create", { conversation_id: create.conversation_id, execution_cwd: root }));
  await vi.waitFor(() => expect(useAppStore.getState().conversationId).toBe(create.conversation_id));
  expect(useAppStore.getState().activeConversationDraftId).toBeNull();
  expect(useAppStore.getState().conversationDrafts[id].materializing).toBe(true);
  handleRuntimeEvent({ type: "runtime.capabilities", conversation_id: create.conversation_id, workspace_root: root,
    capabilities: { feature_flags: { goals: { enabled: true } } } } as ServerEvent);
  expect(capabilityFeatureEnabled(useAppStore.getState().runtimeCapabilities, "goals")).toBe(true);
  expect(await useAppStore.getState().setAgentMode(nextMode)).toBe(false);
  expect(enterComposerGoalMode()).toBe(false);
  expect(wire.order).not.toContain("conversation.agent_mode.set");
  switched(create.conversation_id!, commandOfType("conversation.switch"));
  expect(await firstSend).toBe(true);
  expect(wire.sent[0]).toMatchObject({ conversation_id: create.conversation_id, agent_mode: initialMode });
  expect(useAppStore.getState()).toMatchObject({ agentMode: initialMode, goalMode: false, draft: "new input remains owned" });
  expect(await useAppStore.getState().setAgentMode(nextMode)).toBe(true);
  expect(commandOfType("conversation.agent_mode.set")).toMatchObject({ conversation_id: create.conversation_id, agent_mode: nextMode });
  expect(useAppStore.getState().agentMode).toBe(nextMode);
});

it("does not let a pending first send supersede a later explicit workspace activation", async () => {
  currentDraft(); useAppStore.getState().setDraft("submitted before workspace change");
  const gate = deferred<CommandResultEvent>();
  wire.rpc.mockImplementationOnce((command: ClientCommand) => { wire.order.push(command.type); return gate.promise; });
  const sent = sendChatMessage({ displayContent: "submitted before workspace change" });
  await vi.waitFor(() => expect(commandOfType("conversation.create")).toBeTruthy());
  const create = commandOfType("conversation.create");
  const workspace = { type: "workspace.set" as const, path: "C:/new-project", client_command_id: "explicit-later-workspace" };
  rememberSentCommandProjection(workspace, workspace.client_command_id, null);
  rememberSentCommandProjection({ type: "session.restore", client_command_id: "later-transport-restore" }, "later-transport-restore", null);
  gate.resolve(ok("conversation.create", { conversation_id: create.conversation_id, execution_cwd: root }));
  expect(await sent).toBe(true); expect(wire.order).not.toContain("conversation.switch");
  switched("workspace-owner", workspace, "C:/new-project");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "workspace-owner", activeConversationDraftId: null, workingDirectory: "C:/new-project" });
});

it("merges old owner snapshots in the background while a draft keeps its cwd, model, text and selection", async () => {
  const id = currentDraft(); await useAppStore.getState().refreshConversationDraftSelection(id);
  useAppStore.getState().setDraft("keep selected draft");
  useAppStore.setState({ sideChats: { side: { id: "side", draft: "", messages: [], isStreaming: false, model: "old" } } });
  handleSessionEvent({ type: "session.synced", active_conversation_id: "old", current_model: "wrong-model", workspace_root: "C:/old",
    active_conversation: { id: "old", title: "old", workspace_root: "C:/old", messages: [{ id: "history", role: "user", content: "background history" }] },
    session: { active_conversation_id: "old", workspace_root: "C:/old", permission_mode: "bypass", active_stream_conversation_ids: ["old"] } } as ServerEvent, buffers);
  handleSessionEvent({ type: "llm.model.updated", conversation_id: "side", current_model: "side-new" } as ServerEvent, buffers);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: id, conversationId: null, workingDirectory: root,
    currentModel: "fixture", permissionMode: "confirm", draft: "keep selected draft", messages: [],
    sideChats: { side: { model: "side-new" } }, conversationMessages: { old: [{ id: "history" }] }, conversationStreaming: { old: true } });
  writeLS(LS.conversation.activeId, "old-persisted-owner");
  const { conversationIdForSessionRestore } = await vi.importActual<typeof import("../hooks/useWebSocket")>("../hooks/useWebSocket");
  expect(conversationIdForSessionRestore(useAppStore.getState())).toBe("");
});

it("hydrates superseded A facts without replacing B's authoritative runtime session", () => {
  useAppStore.setState({ conversationId: "B", conversations: [{ id: "B", title: "B", workspaceRoot: root }],
    runtimeSession: { active_conversation_id: "B", workspace_root: root, active_stream_conversation_ids: ["B"] } });
  rememberSentCommandProjection({ type: "conversation.switch", conversation_id: "B", client_command_id: "new-B" }, "new-B", "B");
  switched("A", { type: "conversation.switch", conversation_id: "A", client_command_id: "old-A" }, "C:/A", "canonical A history");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", workingDirectory: root, runtimeSession: { active_conversation_id: "B" },
    conversationMessages: { A: [{ content: "canonical A history" }] } });
});

it("rejects a superseded restore snapshot even if its owner matches a newer pending navigation", () => {
  useAppStore.setState({ conversationId: "B", pendingConversationSwitchId: "A", currentModel: "B-model",
    conversations: [{ id: "B", title: "B", workspaceRoot: root }],
    runtimeSession: { active_conversation_id: "B", workspace_root: root, active_stream_conversation_ids: ["B"] } });
  rememberSentCommandProjection({ type: "conversation.switch", conversation_id: "A", client_command_id: "new-A-navigation" }, "new-A-navigation", "B");
  handleSessionEvent({ type: "session.restored", active_conversation_id: "A", current_model: "stale-A-model",
    client_command_id: "old-restore", client_command_type: "session.restore", conversation_switched_follows: false,
    active_conversation: { id: "A", title: "A", workspace_root: "C:/A", messages: [{ id: "A-message", role: "user", content: "A history" }] },
    session: { active_conversation_id: "A", workspace_root: "C:/A", active_stream_conversation_ids: [] },
  } as ServerEvent, buffers);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", pendingConversationSwitchId: "A", currentModel: "B-model",
    runtimeSession: { active_conversation_id: "B", active_stream_conversation_ids: ["B"] }, conversationMessages: { A: [{ id: "A-message" }] } });
});

it("does not treat an empty owner and no pending navigation as authority to clear a newer reader's runtime facts", () => {
  useAppStore.setState({ conversationId: "B", pendingConversationSwitchId: null, currentModel: "B-model",
    conversations: [{ id: "B", title: "B", workspaceRoot: root }],
    runtimeSession: { active_conversation_id: "B", workspace_root: root, active_stream_conversation_ids: ["B"] } });
  rememberSentCommandProjection({ type: "conversation.switch", conversation_id: "B", client_command_id: "new-B-navigation" }, "new-B-navigation", "B");
  handleSessionEvent({ type: "session.restored", active_conversation_id: null, client_command_id: "old-empty-restore",
    client_command_type: "session.restore", conversation_switched_follows: false,
    session: { active_conversation_id: null, workspace_root: "", active_stream_conversation_ids: [] },
  } as ServerEvent, buffers);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", pendingConversationSwitchId: null, currentModel: "B-model",
    runtimeSession: { active_conversation_id: "B", active_stream_conversation_ids: ["B"] } });
});

it("shares a pending metadata read across materialization and reconnect-style refresh", async () => {
  const read = deferred<DraftModelSelection>();
  vi.mocked(fetchDraftModelSelection).mockImplementationOnce(() => read.promise);
  const id = currentDraft(); const materialized = useAppStore.getState().materializeConversationDraft(id);
  const refreshed = useAppStore.getState().refreshConversationDraftSelection(id);
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(1));
  const signal = vi.mocked(fetchDraftModelSelection).mock.calls[0][3]; expect(signal?.aborted).toBe(false);
  read.resolve(selection(root));
  expect(await refreshed).toBe(true); expect(await materialized).toBeTruthy();
  expect(signal?.aborted).toBe(false);
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.filter(([command]) => command.type === "conversation.create")).toHaveLength(1);
});

it.each(["http-commit", "server-event"])("invalidates pending draft capabilities after a real settings change (%s)", async source => {
  const oldRead = deferred<DraftModelSelection>(), freshRead = deferred<DraftModelSelection>();
  vi.mocked(fetchDraftModelSelection).mockImplementationOnce(() => oldRead.promise).mockImplementationOnce(() => freshRead.promise);
  const id = currentDraft();
  useAppStore.getState().setDraft("preserve local body");
  const oldWaiter = useAppStore.getState().refreshConversationDraftSelection(id);
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(1));
  const oldSignal = vi.mocked(fetchDraftModelSelection).mock.calls[0][3];
  let freshWaiter: Promise<boolean> | undefined;
  if (source === "http-commit") freshWaiter = invalidateConversationDraftSelection(id);
  else handleCommandCatalogEvent({ type: "skills/changed" } as ServerEvent);
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(2));
  expect(oldSignal?.aborted).toBe(true);
  freshRead.resolve({ ...selection(root), config_version: "after-commit", feature_flags: { skills: true } });
  if (freshWaiter) expect(await freshWaiter).toBe(true);
  await vi.waitFor(() => expect(useAppStore.getState().conversationDrafts[id].modelProjection?.config_version).toBe("after-commit"));
  oldRead.resolve({ ...selection(root), config_version: "before-commit", feature_flags: { skills: false } });
  expect(await oldWaiter).toBe(true);
  expect(useAppStore.getState().conversationDrafts[id].modelProjection?.config_version).toBe("after-commit");
  expect(useAppStore.getState().runtimeCapabilities?.feature_flags?.skills).toBe(true);
  expect(useAppStore.getState().draft).toBe("preserve local body");
  expect(useAppStore.getState().conversationId).toBeNull();
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
});

it.each(["abort", "late"])("continues an in-flight model selection through settings invalidation (%s old response)", async behavior => {
  const id = currentDraft();
  await useAppStore.getState().refreshConversationDraftSelection(id);
  let finishOld!: (value: DraftModelSelection) => void;
  const fresh = deferred<DraftModelSelection>();
  vi.mocked(fetchDraftModelSelection).mockImplementationOnce((_workspace, _model, _effort, signal) => new Promise((resolve, reject) => {
    finishOld = resolve;
    if (behavior === "abort") signal!.addEventListener("abort", () => reject(new DOMException("Superseded settings read", "AbortError")), { once: true });
  })).mockImplementationOnce(() => fresh.promise);
  const edited = useAppStore.getState().setModelSelection({ mode: "model", model: "other" });
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(2));
  const refreshed = invalidateConversationDraftSelection(id);
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(3));
  fresh.resolve({ ...selection(root, "other"), config_version: "latest-settings" });
  expect(await refreshed).toBe(true);
  expect(await edited).toBe(true);
  expect(useAppStore.getState().conversationDrafts[id].modelSelection).toMatchObject({ mode: "model", model: "other" });
  expect(useAppStore.getState().currentModel).toBe("other");
  finishOld({ ...selection(root, "other"), config_version: "old-settings" });
  await Promise.resolve(); await Promise.resolve();
  expect(useAppStore.getState().conversationDrafts[id].modelProjection?.config_version).toBe("latest-settings");
  expect(useAppStore.getState().currentModel).toBe("other");
});

it.each(["abort", "late"])("continues a first send through settings invalidation without duplicating creation or receipt (%s old response)", async behavior => {
  let finishOld!: (value: DraftModelSelection) => void;
  const fresh = deferred<DraftModelSelection>();
  vi.mocked(fetchDraftModelSelection).mockImplementationOnce((_workspace, _model, _effort, signal) => new Promise((resolve, reject) => {
    finishOld = resolve;
    if (behavior === "abort") signal!.addEventListener("abort", () => reject(new DOMException("Superseded settings read", "AbortError")), { once: true });
  })).mockImplementationOnce(() => fresh.promise);
  const id = currentDraft();
  useAppStore.getState().setDraft("first send after settings commit");
  const sent = sendChatMessage({ displayContent: "first send after settings commit" });
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(1));
  handleCommandCatalogEvent({ type: "skills/changed" } as ServerEvent);
  await vi.waitFor(() => expect(fetchDraftModelSelection).toHaveBeenCalledTimes(2));
  fresh.resolve({ ...selection(root), config_version: "latest-settings" });
  expect(await sent).toBe(true);
  const owner = useAppStore.getState().conversationId;
  expect(owner).toBeTruthy();
  expect(wire.sent).toHaveLength(1);
  expect(wire.sent[0]).toMatchObject({ type: "user_message", conversation_id: owner, content: "first send after settings commit" });
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.filter(([command]) => command.type === "conversation.create")).toHaveLength(1);
  finishOld({ ...selection(root), config_version: "old-settings" });
  await Promise.resolve(); await Promise.resolve();
  expect(useAppStore.getState().conversationId).toBe(owner);
  expect(wire.sent).toHaveLength(1);
  expect(useAppStore.getState().conversationDrafts[id].adopted).toBe(true);
});

it("persists one body and original File bytes when a draft has a real owner but switch confirmation is pending", async () => {
  const id = currentDraft();
  const file = new File([new Uint8Array([0, 127, 255, 10])], "original.bin", { type: "application/octet-stream", lastModified: 1234 });
  useAppStore.setState({ draft: "body to recover", attachments: [{ id: "original-file", name: file.name, size: file.size,
    type: file.type, localFile: file, status: "error", draftId: id }] });
  vi.mocked(sendClientCommand).mockImplementation(command => { if (command.type === "conversation.switch") wire.order.push(command.type); return true; });
  const materialized = useAppStore.getState().materializeConversationDraft(id);
  await vi.waitFor(() => expect(commandOfType("conversation.switch")).toBeTruthy());
  await flushComposerDrafts();
  const records = await loadComposerDrafts();
  expect(records).toHaveLength(1); expect(records[0].owner.kind).toBe("conversation");
  expect(records[0].draftContext).toMatchObject({ id, adopted: true, conversationId: records[0].owner.id });
  expect(records[0].draft.draft).toBe("body to recover");
  expect(new Uint8Array(await records[0].draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 127, 255, 10]));
  const command = commandOfType("conversation.switch"); switched(command.conversation_id!, command, root, "", true); switched(command.conversation_id!, command);
  expect(await materialized).toBe(command.conversation_id);
});

it("retires a deleted owner binding and cannot resurrect its local body from a dormant-window snapshot", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("deleted owner's unsent input");
  useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
    [id]: { ...state.conversationDrafts[id], conversationId: "deleted-owner" } } }));
  const stale = collectComposerDrafts(useAppStore.getState(), 100);
  persistComposerDrafts(stale); await flushComposerDrafts();
  discardConversationDraftsForOwner("deleted-owner"); await deleteConversationComposerDrafts("deleted-owner");
  persistComposerDrafts(stale); await flushComposerDrafts();
  expect((await loadComposerDrafts()).some(record => record.draftContext?.id === id)).toBe(false);
  expect(useAppStore.getState().activeConversationDraftId).not.toBe(id);
  expect(useAppStore.getState().draft).toBe(""); expect(wire.sent).toEqual([]);
});

it("moves legacy workspace comments to the client draft and then to the confirmed resource with its body", async () => {
  const comment = { type: "comment" as const, content: [{ content_type: "text" as const, text: "keep this source range" }],
    position: { path: "a.ts", side: "left" as const, line: 3 }, localWorkspaceRoot: root };
  const legacyScope = getDiffCommentScope(null, root);
  setDiffComments(legacyScope, [comment]);
  const id = currentDraft(); useAppStore.getState().setDraft("prompt with comment");
  expect(readDiffCommentState(id).comments).toEqual([comment]);
  expect(readDiffCommentState(legacyScope).comments).toEqual([]);
  const owner = await useAppStore.getState().materializeConversationDraft(id);
  expect(readDiffCommentState(owner!).comments).toEqual([comment]);
  expect(readDiffCommentState(id).comments).toEqual([]);
});

it("retains the original client draft when opening a real conversation is rejected", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("keep failed navigation input");
  vi.mocked(sendClientCommand).mockImplementation(command => {
    if (command.type === "conversation.switch" && command.conversation_id === "missing-owner") return true;
    return defaultSend(command);
  });
  useAppStore.getState().requestConversationSwitch("missing-owner");
  expect(useAppStore.getState().activeConversationDraftId).toBe(id);
  const request = commandOfType("conversation.switch");
  receive({ type: "command.result", command: "conversation.switch", level: "error", message: "not found",
    client_command_id: request.client_command_id, client_command_type: "conversation.switch", data: { conversation_id: "missing-owner" } } as ServerEvent);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: id, pendingConversationSwitchId: null,
    conversationId: null, draft: "keep failed navigation input" });
});

it("stops a deleted draft's pending configuration instead of sending a turn or rebuilding its binding", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("delete while configuring");
  const gate = deferred<CommandResultEvent>();
  wire.rpc.mockImplementation((command: ClientCommand, expected: string) => {
    if (command.type === "llm.model.set") { wire.order.push(command.type); return gate.promise; }
    return defaultRpc(command, expected);
  });
  const materialized = useAppStore.getState().materializeConversationDraft(id);
  await vi.waitFor(() => expect(commandOfType("llm.model.set")).toBeTruthy());
  const owner = useAppStore.getState().conversationDrafts[id].conversationId!;
  handleCommandResultEvent({ type: "command.result", command: "conversation.delete", level: "success", message: "",
    data: { conversation_id: owner, deleted: true } } as ServerEvent);
  gate.resolve(ok("model.set", { conversation_id: owner }));
  expect(await materialized).toBeNull();
  expect(useAppStore.getState().conversationDrafts[id]).toBeUndefined();
  expect(wire.order).not.toContain("llm.config.set"); expect(wire.order).not.toContain("conversation.switch");
  expect(wire.sent).toEqual([]);
});

it("continues only after this switch's final canonical event, not stale same-owner or hydration-only frames, and releases both subscriptions", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("canonical receipt only");
  const stopped = trackStateStop();
  vi.mocked(sendClientCommand).mockImplementation(command => {
    if (command.type === "conversation.switch") {
      wire.order.push(command.type);
      expect(wire.listeners.size).toBe(1);
      switched(command.conversation_id, command, root, "", true);
    }
    return true;
  });
  const sent = sendChatMessage({ displayContent: "canonical receipt only" });
  await vi.waitFor(() => expect(commandOfType("conversation.switch")).toBeTruthy());
  const command = commandOfType("conversation.switch"), owner = command.conversation_id!;
  expect(useAppStore.getState()).toMatchObject({ conversationId: owner, pendingConversationSwitchId: null });
  expect(wire.sent).toEqual([]); expect(useAppStore.getState().conversationDrafts[id].materializing).toBe(true);
  switched(owner, { ...command, client_command_id: "old-same-owner-switch" }, root, "", false);
  expect(useAppStore.getState().conversationHydration[owner].isHydrating).toBe(true);
  receive({ type: "conversation.hydration.updated", conversation_id: owner, is_hydrating: false } as ServerEvent);
  await Promise.resolve();
  expect(wire.sent).toEqual([]); expect(useAppStore.getState().conversationDrafts[id].materializing).toBe(true);
  switched(owner, command);
  expect(await sent).toBe(true);
  expect(wire.sent).toHaveLength(1); expect(wire.sent[0]).toMatchObject({ type: "user_message", conversation_id: owner });
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.some(([request]) => request.type === "conversation.switch")).toBe(false);
  expectWaiterReleased(stopped);
  switched(owner, command); useAppStore.setState({ draft: "later input" });
  expect(wire.sent).toHaveLength(1); expectWaiterReleased(stopped);
});

it.each(["refusal", "disconnect", "send-failed"] as const)("keeps input and original File after %s and ignores late canonical completion without leaking the waiter", async failure => {
  const id = currentDraft();
  const file = new File([new Uint8Array([0, 127, 255, 10])], "retained.bin", { type: "application/octet-stream", lastModified: 2026 });
  useAppStore.setState({ draft: "retained rejected input", attachments: [{ id: "retained-file", name: file.name, size: file.size,
    type: file.type, localFile: file, status: "ready", draftId: id }] });
  const stopped = trackStateStop();
  vi.mocked(sendClientCommand).mockImplementation(command => {
    if (command.type === "conversation.switch") wire.order.push(command.type);
    return command.type !== "conversation.switch" || failure !== "send-failed";
  });
  const sent = sendChatMessage({ displayContent: "retained rejected input" });
  await vi.waitFor(() => expect(commandOfType("conversation.switch")).toBeTruthy());
  const command = commandOfType("conversation.switch"), owner = command.conversation_id!;
  if (failure === "refusal") receive({ type: "command.result", command: "conversation.switch", client_command_id: command.client_command_id,
    client_command_type: "conversation.switch", level: "warning", message: "Restore this archived conversation first.",
    data: { conversation_id: owner, reason: "archived" } } as ServerEvent);
  else if (failure === "disconnect") useAppStore.setState({ isConnected: false });
  expect(await sent).toBe(false); expect(wire.sent).toEqual([]);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: id, draft: "retained rejected input", pendingConversationSwitchId: null });
  expect(useAppStore.getState().conversationDrafts[id]).toMatchObject({ conversationId: owner, adopted: true, materializing: false });
  expectWaiterReleased(stopped);
  useAppStore.setState({ isConnected: true }); switched(owner, command);
  expect(wire.sent).toEqual([]); expectWaiterReleased(stopped);
  await flushComposerDrafts();
  const recovered = (await loadComposerDrafts()).find(record => record.owner.id === owner)!;
  expect(recovered.draft.draft).toBe("retained rejected input");
  expect(new Uint8Array(await recovered.draft.attachments[0].localFile!.arrayBuffer())).toEqual(new Uint8Array([0, 127, 255, 10]));
});

it("fails on the actual context hydration error and never mistakes hydration.updated(false) for a successful switch", async () => {
  const id = currentDraft(); useAppStore.getState().setDraft("keep failed hydration input");
  const stopped = trackStateStop();
  vi.mocked(sendClientCommand).mockImplementation(command => { if (command.type === "conversation.switch") {
    wire.order.push(command.type); switched(command.conversation_id, command, root, "", true);
  } return true; });
  const sent = sendChatMessage({ displayContent: "keep failed hydration input" });
  await vi.waitFor(() => expect(commandOfType("conversation.switch")).toBeTruthy());
  const command = commandOfType("conversation.switch"), owner = command.conversation_id!;
  receive({ type: "error", conversation_id: owner, error_type: "context", message: "Actual repository hydration failure",
    recoverable: true, client_command_id: command.client_command_id, client_command_type: command.type } as ServerEvent);
  receive({ type: "conversation.hydration.updated", conversation_id: owner, is_hydrating: false } as ServerEvent);
  expect(await sent).toBe(false); expect(wire.sent).toEqual([]);
  expect(useAppStore.getState().draft).toBe("keep failed hydration input");
  expect(useAppStore.getState().conversationWorkbenchStates[owner].draft).toBe("keep failed hydration input");
  expectWaiterReleased(stopped);
  switched(owner, command); expect(wire.sent).toEqual([]); expectWaiterReleased(stopped);
});

it("starts original A explicitly while B is still pending, keeps A context until B canonical, and restores B's own draft", async () => {
  currentDraft(); useAppStore.setState({ draft: "submitted A", agentMode: "plan", activeTabPath: "C:/same-project/A.ts" });
  const stopped = trackStateStop();
  vi.mocked(sendClientCommand).mockImplementation(command => {
    if (command.type === "conversation.switch") {
      wire.order.push(command.type);
      if (command.conversation_id !== "B") switched(command.conversation_id, command, root, "", true);
    }
    return true;
  });
  const sent = sendChatMessage({ displayContent: "submitted A", agentMode: "plan", primaryFile: "C:/same-project/A.ts" });
  await vi.waitFor(() => expect(commandOfType("conversation.switch")).toBeTruthy());
  const originalSwitch = commandOfType("conversation.switch"), owner = originalSwitch.conversation_id!;
  useAppStore.setState(state => ({ conversations: [...state.conversations, { id: "B", title: "B", workspaceRoot: "C:/project-B" }],
    conversationWorkbenchStates: { ...state.conversationWorkbenchStates, B: { ...emptyConversationWorkbenchState(), draft: "B owned draft", agentMode: "build" } } }));
  useAppStore.getState().requestConversationSwitch("B");
  expect(useAppStore.getState()).toMatchObject({ conversationId: owner, pendingConversationSwitchId: "B", workingDirectory: root });
  expect(await sent).toBe(true);
  expect(wire.sent).toHaveLength(1);
  expect(wire.sent[0]).toMatchObject({ conversation_id: owner, agent_mode: "plan", primary_file: "C:/same-project/A.ts", workspace_root: root });
  expect(useAppStore.getState().conversationWorkbenchStates.B.draft).toBe("B owned draft");
  expectWaiterReleased(stopped);
  const bSwitch = vi.mocked(sendClientCommand).mock.calls.find(([request]) => request.type === "conversation.switch" && request.conversation_id === "B")![0];
  switched("B", bSwitch, "C:/project-B", "", true); switched("B", bSwitch, "C:/project-B");
  useAppStore.setState({ activeTabPath: "C:/project-B/B.ts" });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "B owned draft", workingDirectory: "C:/project-B" });
  switched(owner, originalSwitch);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "B owned draft", activeTabPath: "C:/project-B/B.ts" });
  expect(wire.sent).toHaveLength(1); expectWaiterReleased(stopped);
});
