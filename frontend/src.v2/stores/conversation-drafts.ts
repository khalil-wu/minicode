import { useAppStore } from "./index";
import type { AppStore, ConversationDraftContext, ConversationDraftOptions } from "./types";
import { composerDraftOf, composerWorkspaceKey, emptyComposerDraft, flushComposerDrafts } from "./composer-drafts";
import { draftWindowOwnerId } from "./draft-window-owner";
import { emptyConversationWorkbenchState } from "./ui-slice";
import { cacheMessagesForConversation, conversationResetPayload, editorStateForWorkspace, cacheEditorStateForWorkspace,
  conversationWorkspacePath, LS, newConversationId, readLS, uniqueMessageId, writeLS } from "./shared-helpers";
import { desktop, type DesktopPreferences } from "../desktop/runtime";
import { fetchDraftModelSelection } from "../protocol/api";
import { modelSelectionPatch } from "../lib/model-selection";
import { normalizeWorkspaceRoot, workspaceRootsEqual } from "../lib/workspace-path";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { foregroundNavigationIntent, isSupersededCommandProjection, rememberLocalConversationDraftSelection, rememberSentCommandProjection } from "../protocol/command-projection";
import { getWebSocket } from "../hooks/useWebSocket";
import type { ServerEvent } from "../protocol/events";
import { closeAutomations } from "../lib/automations-navigation";
import { pushToast } from "../overlays/ToastContainer";
import { normalizeSlashCommands } from "../lib/catalog-normalizers";
import { clearDiffCommentScope, getDiffCommentScope, transferDiffComments } from "../lib/diff-comment-storage";
import { draftBrowserOwnerId, readBrowserWorkbench, transferDraftBrowserWorkbench } from "./browser-workbench";
import { transferDraftBrowserRequests } from "../chat/openWebInBrowser";
import { releaseRetainedInputPreviews } from "../composer/retainedComposerInputs";

const selectionRequests = new Map<string, { key: string; controller: AbortController; promise: Promise<boolean>;
  followInvalidation: (fresh: Promise<boolean>) => void }>();
const materializations = new Map<string, Promise<string | null>>();
const preferenceRequests = new Map<string, Promise<DesktopPreferences | null>>();
const retiredDrafts = new Set<string>();
export const activeDraftStorageKey = () => `minicode.conversationDraft.active:${draftWindowOwnerId()}`;
export const savedActiveConversationDraftId = () => {
  const navigation = window.__MINICODE_RUNTIME__?.initialNavigation;
  return navigation && !navigation.restoreLastConversation ? null : readLS(activeDraftStorageKey()) || null;
};
export const conversationDraftTargetKey = (root: string, section?: string) => JSON.stringify([normalizeWorkspaceRoot(root), section ?? null]);
export const selectPendingDraftUserMessage = (state: Pick<AppStore, "activeConversationDraftId" | "conversationId" | "conversationDrafts">) => {
  if (state.activeConversationDraftId) return state.conversationDrafts[state.activeConversationDraftId]?.pendingUserMessage;
  return state.conversationId ? Object.values(state.conversationDrafts)
    .find(draft => draft.conversationId === state.conversationId && draft.pendingUserMessage)?.pendingUserMessage : undefined;
};
export const isConversationDraftMaterializing = (
  state: Pick<AppStore, "conversationId" | "activeConversationDraftId" | "conversationDrafts">,
  conversationId = state.conversationId,
): boolean => conversationId
  ? Object.values(state.conversationDrafts).some(draft => draft.materializing && (draft.conversationId ?? draft.requestedConversationId) === conversationId)
  : Boolean(state.activeConversationDraftId && state.conversationDrafts[state.activeConversationDraftId]?.materializing);

export const newConversationDraftContext = (state: AppStore, workspaceRoot: string, bindWorkspace: boolean,
  options: ConversationDraftOptions = {}): ConversationDraftContext => ({
  id: uniqueMessageId("draft"), workspaceRoot, bindWorkspace, sidebarSectionId: options.sidebarSectionId,
  appMode: options.appMode ?? "cowork", permissionMode: state.permissionMode, permissionEdited: false,
  modelSelection: { mode: "default" }, selectionLoading: true, selectionError: null,
  preferencesLoading: Boolean(desktop()),
});

export const draftSelectionView = (context: ConversationDraftContext): Partial<AppStore> => ({
  ...modelSelectionPatch(context.modelProjection),
  permissionMode: context.permissionMode,
  ...(context.modelProjection ? { availableModels: context.modelProjection.available_models,
    availableModelLabels: {}, modelsSource: context.modelProjection.models_source,
    slashCommands: normalizeSlashCommands(context.modelProjection.composer_commands),
    runtimeCapabilities: { provider_capabilities: context.modelProjection,
      feature_flags: context.modelProjection.feature_flags, composer_commands: context.modelProjection.composer_commands,
      permission: { mode: context.permissionMode, available_modes: context.modelProjection.available_permission_modes } } } : {
    currentModel: "", currentProvider: "", currentProviderId: "", currentProviderBaseUrl: "", currentWireApi: "", effortLevel: "off",
    modelSelectionMode: context.modelSelection.mode, defaultModelSelection: null,
    availableModels: [], availableModelOptions: [], availableModelLabels: {}, modelsSource: "", slashCommands: [], runtimeCapabilities: null,
  }),
});

export const refreshConversationDraftSelection = async (requestedId?: string, actualCwd?: string): Promise<boolean> => {
  const id = requestedId ?? useAppStore.getState().activeConversationDraftId;
  if (!id) return false;
  let context = useAppStore.getState().conversationDrafts[id];
  if (!context || context.adopted) return false;
  if (!context.bindWorkspace && !context.conversationId && desktop()) {
    try { await loadConversationDraftPreferences(id); } catch { return false; }
    context = useAppStore.getState().conversationDrafts[id];
    if (!context || context.adopted) return false;
  }
  const configCwd = actualCwd ?? context.executionCwd ?? (context.bindWorkspace ? context.workspaceRoot : context.projectlessBase ?? "");
  const key = JSON.stringify([context.workspaceRoot, configCwd, context.modelSelection]);
  const previous = selectionRequests.get(id);
  if (previous?.key === key) return previous.promise;
  previous?.controller.abort();
  const controller = new AbortController();
  useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
    [id]: { ...state.conversationDrafts[id], selectionLoading: true, selectionError: null } } }));
  let resolveSelection!: (result: boolean | PromiseLike<boolean>) => void;
  let rejectSelection!: (reason: unknown) => void;
  const promise = new Promise<boolean>((resolve, reject) => { resolveSelection = resolve; rejectSelection = reject; });
  selectionRequests.set(id, { key, controller, promise, followInvalidation: resolveSelection });
  const pending = (async () => { try {
    const projection = await fetchDraftModelSelection(context.workspaceRoot,
      context.modelSelection.mode === "model" ? context.modelSelection.model : "", context.modelSelection.effort, controller.signal, configCwd);
    if (selectionRequests.get(id)?.controller !== controller) return false;
    useAppStore.setState(state => {
      const latest = state.conversationDrafts[id];
      if (!latest || latest.adopted) return state;
      const next = { ...latest, modelProjection: projection, selectionLoading: false, selectionError: null };
      return { conversationDrafts: { ...state.conversationDrafts, [id]: next },
        ...(state.activeConversationDraftId === id ? draftSelectionView(next) : {}) };
    });
    return true;
  } catch (error) {
    if (controller.signal.aborted) return false;
    const message = error instanceof Error ? error.message : String(error);
    useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
      [id]: { ...state.conversationDrafts[id], selectionLoading: false, selectionError: message } } }));
    if (useAppStore.getState().activeConversationDraftId === id) pushToast(message, "error");
    return false;
  } finally {
    if (selectionRequests.get(id)?.controller === controller) selectionRequests.delete(id);
  } })();
  void pending.then(resolveSelection, rejectSelection);
  return promise;
};

export const invalidateConversationDraftSelection = (requestedId?: string): Promise<boolean> => {
  const id = requestedId ?? useAppStore.getState().activeConversationDraftId;
  if (!id) return Promise.resolve(false);
  const previous = selectionRequests.get(id);
  selectionRequests.delete(id);
  const fresh = refreshConversationDraftSelection(id);
  previous?.followInvalidation(fresh);
  previous?.controller.abort();
  return fresh;
};

export const editConversationDraftModel = async (selection: ConversationDraftContext["modelSelection"]): Promise<boolean> => {
  const state = useAppStore.getState(), id = state.activeConversationDraftId;
  if (!id) return false;
  const previous = state.conversationDrafts[id];
  useAppStore.setState(current => ({ conversationDrafts: { ...current.conversationDrafts,
    [id]: { ...previous, modelSelection: selection } } }));
  if (await refreshConversationDraftSelection(id)) return true;
  useAppStore.setState(current => {
    if (current.conversationDrafts[id]?.modelSelection !== selection) return current;
    const restored = { ...previous, selectionLoading: false, selectionError: current.conversationDrafts[id].selectionError };
    return { conversationDrafts: { ...current.conversationDrafts, [id]: restored },
      ...(current.activeConversationDraftId === id ? draftSelectionView(restored) : {}) };
  });
  return false;
};

export const selectConversationDraft = (id: string, options: { snapshotCurrent?: boolean } = {}) => {
  const before = useAppStore.getState(), context = before.conversationDrafts[id];
  if (!context) return;
  if (context.adopted && context.conversationId) { before.requestConversationSwitch(context.conversationId); return; }
  if (options.snapshotCurrent !== false) {
    if (before.conversationId) before.snapshotAgentState(before.conversationId);
    before.snapshotWorkbenchState();
  }
  if (!workspaceRootsEqual(before.workingDirectory, context.workspaceRoot)) {
    cacheEditorStateForWorkspace(before.workingDirectory, before.editorTabs, before.activeTabPath, before.activeEditorPath);
  }
  const state = useAppStore.getState();
  rememberLocalConversationDraftSelection(id);
  closeAutomations();
  useAppStore.setState({ ...conversationResetPayload(), ...cacheMessagesForConversation(state, state.conversationId),
    ...emptyConversationWorkbenchState(), ...state.workspaceComposerDrafts[id],
    ...readBrowserWorkbench(draftBrowserOwnerId(id))?.layout,
    ...(workspaceRootsEqual(before.workingDirectory, context.workspaceRoot)
      ? { editorTabs: before.editorTabs, activeTabPath: before.activeTabPath, activeEditorPath: before.activeEditorPath }
      : editorStateForWorkspace(context.workspaceRoot)), ...draftSelectionView(context),
    activeConversationDraftId: id, conversationId: null, messages: [], isStreaming: false, isPaused: false,
    plan: null, todos: [], subagents: [], agentProgress: [], activeGoal: null,
    workingDirectory: context.workspaceRoot, workspaceGit: null, previewOwnerConversationId: null,
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    settingsOpen: false, projectsOpen: false, skillsMarketplaceOpen: false, availableSkills: [],
    mentionResults: [], slashPanelOpen: false, mentionPanelOpen: false, actionChip: null,
  });
  useAppStore.getState().setAppMode(context.appMode);
  writeLS(activeDraftStorageKey(), id);
  writeLS(LS.conversation.activeId, "");
  requestAnimationFrame(() => {
    if (useAppStore.getState().activeConversationDraftId === id) window.dispatchEvent(new Event("composer:focus"));
  });
  void refreshConversationDraftSelection(id);
  void loadConversationDraftPreferences(id).catch(error => pushToast(error instanceof Error ? error.message : String(error), "error"));
};

const loadConversationDraftPreferences = (id: string): Promise<DesktopPreferences | null> => {
  const active = preferenceRequests.get(id);
  if (active) return active;
  const native = desktop();
  if (!native) return Promise.resolve(null);
  const request = native.preferences.get().then(preferences => {
    useAppStore.setState(current => {
      const latest = current.conversationDrafts[id];
      if (!latest || latest.adopted) return current;
      const next = { ...latest, preferencesLoading: false,
        ...(!latest.bindWorkspace && !latest.conversationId ? { projectlessBase: preferences.defaultTaskDirectory } : {}),
        ...(!latest.permissionEdited && !latest.conversationId ? { permissionMode: preferences.defaultPermissionMode } : {}) };
      return { conversationDrafts: { ...current.conversationDrafts, [id]: next },
        ...(current.activeConversationDraftId === id ? { permissionMode: next.permissionMode } : {}) };
    });
    return preferences;
  }).catch(error => {
    useAppStore.setState(current => current.conversationDrafts[id] ? ({ conversationDrafts: { ...current.conversationDrafts,
      [id]: { ...current.conversationDrafts[id], preferencesLoading: false,
        selectionError: error instanceof Error ? error.message : String(error) } } }) : current);
    throw error;
  }).finally(() => { if (preferenceRequests.get(id) === request) preferenceRequests.delete(id); });
  preferenceRequests.set(id, request);
  return request;
};

export const startConversationDraft = (options: ConversationDraftOptions = {}): string => {
  const state = useAppStore.getState();
  const currentIsProjectless = state.conversations.some(item => item.id === state.conversationId
    && item.executionCwd && !item.workspaceRoot && !item.worktreePath);
  const bindWorkspace = Boolean(options.workspaceRoot) || (options.bindWorkspace === true && !currentIsProjectless)
    || (options.bindWorkspace === undefined && state.appMode === "code" && Boolean(state.workingDirectory) && !currentIsProjectless);
  const workspaceRoot = bindWorkspace ? options.workspaceRoot ?? state.workingDirectory : "";
  const key = conversationDraftTargetKey(workspaceRoot, options.sidebarSectionId);
  const savedId = state.homeConversationDraftIds[key];
  const saved = savedId ? state.conversationDrafts[savedId] : undefined;
  if (saved && !saved.conversationId && !materializations.has(saved.id)) { selectConversationDraft(saved.id); return saved.id; }
  if (state.conversationId) state.snapshotAgentState(state.conversationId);
  state.snapshotWorkbenchState();
  const context = newConversationDraftContext(state, workspaceRoot, bindWorkspace, options);
  transferDiffComments(getDiffCommentScope(null, workspaceRoot), getDiffCommentScope(context.id, workspaceRoot));
  useAppStore.setState(current => {
    const bodies = { ...current.workspaceComposerDrafts };
    const legacyKey = composerWorkspaceKey(workspaceRoot);
    const body = bodies[legacyKey] ?? (!current.conversationId && !current.activeConversationDraftId
      && workspaceRootsEqual(current.workingDirectory, workspaceRoot) ? composerDraftOf(current) : emptyComposerDraft());
    delete bodies[legacyKey];
    bodies[context.id] = body;
    return { conversationDrafts: { ...current.conversationDrafts, [context.id]: context },
      workspaceComposerDrafts: bodies, homeConversationDraftIds: { ...current.homeConversationDraftIds, [key]: context.id } };
  });
  selectConversationDraft(context.id, { snapshotCurrent: false });
  return context.id;
};

/** A confirmed server deletion also retires its pending local binding. */
export const discardConversationDraftsForOwner = (owner: string) => {
  const before = useAppStore.getState();
  const drafts = Object.values(before.conversationDrafts)
    .filter(context => (context.conversationId ?? context.requestedConversationId) === owner);
  if (!drafts.length) return;
  const ids = new Set(drafts.map(context => context.id));
  for (const input of Object.values(before.retainedComposerInputs)) {
    if (input.owner.kind === "draft" && ids.has(input.owner.id)) releaseRetainedInputPreviews(input);
  }
  const active = drafts.find(context => context.id === before.activeConversationDraftId);
  for (const draft of drafts) {
    if (materializations.has(draft.id)) retiredDrafts.add(draft.id);
    selectionRequests.get(draft.id)?.controller.abort();
    selectionRequests.delete(draft.id);
    clearDiffCommentScope(getDiffCommentScope(draft.id, draft.workspaceRoot));
  }
  useAppStore.setState(state => ({
    retainedComposerInputs: Object.fromEntries(Object.entries(state.retainedComposerInputs)
      .filter(([, input]) => input.owner.kind !== "draft" || !ids.has(input.owner.id))),
    conversationDrafts: Object.fromEntries(Object.entries(state.conversationDrafts).filter(([id]) => !ids.has(id))),
    workspaceComposerDrafts: Object.fromEntries(Object.entries(state.workspaceComposerDrafts).filter(([id]) => !ids.has(id))),
    homeConversationDraftIds: Object.fromEntries(Object.entries(state.homeConversationDraftIds).filter(([, id]) => !ids.has(id))),
    ...(active ? { ...emptyComposerDraft(), activeConversationDraftId: null, pendingConversationSwitchId: null } : {}),
  }));
  if (active) useAppStore.getState().startConversationDraft({ workspaceRoot: active.workspaceRoot || undefined,
    bindWorkspace: active.bindWorkspace, appMode: active.appMode, sidebarSectionId: active.sidebarSectionId });
};

/** Move one draft body after a real resource exists; never create a sidebar fact. */
export const adoptConversationDraftContent = (draftId: string, conversationId: string) => {
  const source = useAppStore.getState().conversationDrafts[draftId];
  if (source && !source.adopted) transferDiffComments(getDiffCommentScope(draftId, source.workspaceRoot), getDiffCommentScope(conversationId, source.workspaceRoot));
  useAppStore.setState(state => {
    const context = state.conversationDrafts[draftId];
    if (!context || context.adopted) return state;
    const body = state.activeConversationDraftId === draftId ? composerDraftOf(state) : state.workspaceComposerDrafts[draftId];
    const remaining = { ...state.workspaceComposerDrafts };
    delete remaining[draftId];
    const home = { ...state.homeConversationDraftIds };
    const key = conversationDraftTargetKey(context.workspaceRoot, context.sidebarSectionId);
    if (home[key] === draftId) delete home[key];
    return { workspaceComposerDrafts: remaining, homeConversationDraftIds: home,
      retainedComposerInputs: Object.fromEntries(Object.entries(state.retainedComposerInputs).map(([id, input]) => [id,
        input.owner.kind === "draft" && input.owner.id === draftId ? { ...input,
          owner: { kind: "conversation" as const, id: conversationId, workspaceRoot: context.executionCwd ?? context.workspaceRoot } } : input])),
      conversationDrafts: { ...state.conversationDrafts, [draftId]: { ...context, conversationId, adopted: true } },
      conversationWorkbenchStates: { ...state.conversationWorkbenchStates, [conversationId]: {
        ...emptyConversationWorkbenchState(), ...state.conversationWorkbenchStates[conversationId], ...body,
        ...readBrowserWorkbench(conversationId)?.layout,
      } } };
  });
};

const activateMaterializedDraft = (id: string, owner: string, requestedIntent: string | undefined): Promise<boolean> => {
  const navigation = useAppStore.getState();
  if (navigation.activeConversationDraftId !== id
    || foregroundNavigationIntent() !== requestedIntent
    || (navigation.pendingConversationSwitchId && navigation.pendingConversationSwitchId !== owner)) return Promise.resolve(true);
  const socket = getWebSocket();
  if (!socket || !navigation.isConnected) return Promise.resolve(false);
  const command = { type: "conversation.switch" as const, conversation_id: owner, client_command_id: uniqueMessageId("cmd"),
    ...(navigation.conversationDrafts[id]?.pendingUserMessage ? { defer_runtime_capabilities: true } : {}) };
  rememberSentCommandProjection(command, command.client_command_id, null);
  return new Promise(resolve => {
    let settled = false;
    const finish = (activated: boolean) => {
      if (settled) return;
      settled = true;
      stopEvents(); stopState();
      const state = useAppStore.getState();
      if (!activated && foregroundNavigationIntent() === command.client_command_id && state.pendingConversationSwitchId === owner) {
        useAppStore.setState({ pendingConversationSwitchId: null });
      }
      resolve(activated);
    };
    // A successful switch has no command.result. The dispatcher delivers the
    // final canonical notification after applying its owner projection.
    const stopEvents = socket.subscribe(data => {
      const event = data as ServerEvent;
      if (event.type === "conversation.switched" && event.client_command_id === command.client_command_id
        && event.client_command_type === command.type && event.conversation_id === owner && event.is_hydrating === false
        && !isSupersededCommandProjection(event) && foregroundNavigationIntent() === command.client_command_id
        && useAppStore.getState().conversationId === owner) finish(true);
      else if (event.type === "command.result" && event.client_command_id === command.client_command_id && event.command === command.type) finish(false);
      else if (event.type === "error" && ((event.client_command_id === command.client_command_id && event.client_command_type === command.type)
        || (event.conversation_id === owner && event.error_type === "context"
          && (!event.client_command_id || event.client_command_id === command.client_command_id)))) finish(false);
    });
    const stopState = useAppStore.subscribe(state => {
      if (retiredDrafts.has(id) || !state.isConnected) finish(false);
      else if (foregroundNavigationIntent() !== command.client_command_id
        || (state.pendingConversationSwitchId && state.pendingConversationSwitchId !== owner)
        || (state.conversationId !== owner && state.activeConversationDraftId !== id)) finish(true);
      else if (state.conversationId !== owner && state.pendingConversationSwitchId !== owner) finish(false);
    });
    useAppStore.setState({ pendingConversationSwitchId: owner });
    if (!sendClientCommand(command)) finish(false);
  });
};

export const materializeConversationDraft = (id: string): Promise<string | null> => {
  const running = materializations.get(id);
  if (running) return running;
  const pending = (async () => {
    const atStart = useAppStore.getState();
    const requestedIntent = foregroundNavigationIntent();
    const initial = atStart.conversationDrafts[id];
    if (!initial) return null;
    useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
      [id]: { ...state.conversationDrafts[id], materializing: true } } }));
    if (initial.adopted) {
      if (!await activateMaterializedDraft(id, initial.conversationId!, requestedIntent)) return null;
      return retiredDrafts.has(id) ? null : initial.conversationId!;
    }
    const body = atStart.activeConversationDraftId === id ? composerDraftOf(atStart) : atStart.workspaceComposerDrafts[id];
    const [preferences, selectionReady] = await Promise.all([
      loadConversationDraftPreferences(id), refreshConversationDraftSelection(id),
    ]);
    if (!selectionReady) return null;
    if (retiredDrafts.has(id)) return null;
    let context = useAppStore.getState().conversationDrafts[id];
    const owner = context.conversationId ?? context.requestedConversationId ?? newConversationId();
    const creating = !context.conversationId;
    if (!context.conversationId) {
      const commandId = context.creationCommandId ?? uniqueMessageId("cmd");
      useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
        [id]: { ...state.conversationDrafts[id], requestedConversationId: owner, creationCommandId: commandId } } }));
      await flushComposerDrafts();
      if (retiredDrafts.has(id)) return null;
      const created = await useAppStore.getState().createConversation({ bindWorkspace: context.bindWorkspace,
        workspaceRoot: context.workspaceRoot, appMode: context.appMode, sidebarSectionId: context.sidebarSectionId,
        conversationId: owner, clientCommandId: commandId, activate: false, permissionMode: context.permissionMode,
        preferences,
        agentMode: body.agentMode ?? "build", initialPrompt: body.draft, projectlessBase: context.projectlessBase,
        onCreated: result => {
          if (retiredDrafts.has(id)) return;
          useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
            [id]: { ...state.conversationDrafts[id], conversationId: owner,
              executionCwd: typeof result.data?.execution_cwd === "string" ? result.data.execution_cwd : undefined } } }));
        } });
      if (!created || retiredDrafts.has(id)) return null;
    }
    context = useAppStore.getState().conversationDrafts[id];
    const executionCwd = context.executionCwd ?? conversationWorkspacePath(useAppStore.getState().conversations.find(item => item.id === owner));
    useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
      [id]: { ...state.conversationDrafts[id], executionCwd } } }));
    // Default selection is resolved by model.set against this owner's actual
    // execution cwd. The ensuing switch projects it; a second catalog HTTP
    // request here repeats the same resolution before the first message.
    if (context.modelSelection.mode === "model" && !await refreshConversationDraftSelection(id, executionCwd)) return null;
    if (retiredDrafts.has(id)) return null;
    context = useAppStore.getState().conversationDrafts[id];
    if (preferences) useAppStore.getState().initializeWorkspaceView(owner, Boolean(preferences.defaultWorkspaceFullView));
    const projection = context.modelProjection!;
    const selection = context.modelSelection;
    const model = selection.mode === "model" ? selection.model! : projection.default_model_selection?.model;
    if (selection.mode === "model" && !model) { pushToast("请选择可用模型后再发送。", "error"); return null; }
    const effort = selection.effort ?? projection.effective_reasoning_effort;
    const selected = await sendClientCommandAwaitResult({ type: "llm.model.set", conversation_id: owner,
      selection_mode: selection.mode, ...(selection.mode === "model" ? { model,
        ...(typeof effort === "string" && effort ? { reasoning_effort: effort } : {}) } : {}) }, "model.set");
    if (!commandResultSucceeded(selected)) throw new Error(selected.message);
    if (retiredDrafts.has(id)) return null;
    // Creation already committed this draft's permission mode. Only a retry
    // against an existing owner can have an edited mode to apply.
    if (!creating) {
      const permission = await sendClientCommandAwaitResult({ type: "conversation.permission_mode.set", conversation_id: owner,
        mode: context.permissionMode, source: "frontend.ui" }, "conversation.permission_mode.set");
      if (!commandResultSucceeded(permission)) throw new Error(permission.message);
    }
    if (retiredDrafts.has(id)) return null;
    await transferDraftBrowserWorkbench(id, owner);
    if (retiredDrafts.has(id)) return null;
    transferDraftBrowserRequests(draftBrowserOwnerId(id), owner);
    adoptConversationDraftContent(id, owner);
    if (!await activateMaterializedDraft(id, owner, requestedIntent)) return null;
    return retiredDrafts.has(id) ? null : owner;
  })().catch(error => {
    if (retiredDrafts.has(id)) return null;
    useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
      [id]: { ...state.conversationDrafts[id], selectionError: error instanceof Error ? error.message : String(error) } } }));
    if (useAppStore.getState().activeConversationDraftId === id) pushToast(error instanceof Error ? error.message : String(error), "error");
    return null;
  }).finally(() => {
    if (materializations.get(id) === pending) materializations.delete(id);
    retiredDrafts.delete(id);
    useAppStore.setState(state => state.conversationDrafts[id] ? ({ conversationDrafts: { ...state.conversationDrafts,
      [id]: { ...state.conversationDrafts[id], materializing: false } } }) : state);
  });
  materializations.set(id, pending);
  return pending;
};
