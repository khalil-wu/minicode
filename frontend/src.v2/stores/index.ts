import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { AppStore } from "./types";
import { changedTranscriptOwner, conversationTranscriptMembership } from "./conversation-transcript-cache";
import { activeDraftStorageKey, conversationDraftTargetKey, draftSelectionView, newConversationDraftContext } from "./conversation-drafts";

import { createUISlice } from "./ui-slice";
import { createWorkspaceSlice } from "./workspace-slice";
import { createChatSlice } from "./chat-slice";
import { createComposerSlice } from "./composer-slice";
import { createAgentSlice } from "./agent-slice";
import { createApprovalSlice } from "./approval-slice";
import { createControlPlaneSlice } from "./control-plane-slice";
import { createInspectorSlice } from "./inspector-slice";
import { createEditorSlice } from "./editor-slice";
import { applyWorkbenchPreferences, migrateWorkbenchPreferences, dictationPreferencesOf, usesDesktopDictationPreferences, type WorkbenchPreferences } from "../lib/workbench-preferences";
import { getDiffCommentScope, transferDiffComments } from "../lib/diff-comment-storage";
import { applyCodeTextScale, applyReducedMotion, applyPointerCursors, applyDiffMarkerStyle, applyTheme, applyTextScale, initialTheme, readAppearancePreferences, reducedMotionFor, LS, readLS, writeLS, persistEditorTabs, restoreWorkspaceEditorDrafts, reconcileRestoredEditorTabs, editorWorkspaceKey, editorWorkspaceRecoveryLocation } from "./shared-helpers";
import { isAppearanceStorageKey, readAppearanceStorage } from "../lib/appearance-storage";
import { safeJsonParse } from "../lib/safe-parse";
import { flushEditorDrafts, hasPendingEditorDrafts, cachedEditorDrafts } from "./editor-drafts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { dictationController } from "../composer/dictation-controller";
import { pushToast } from "../overlays/ToastContainer";
import { emptyConversationWorkbenchState } from "./ui-slice";
import { mergeRetainedComposerMessages, reconcileRecoveredInputOutcomes, retainedInputMessage } from "../composer/retainedComposerInputs";
import {
  loadComposerDrafts, collectComposerDrafts, scheduleComposerDraftPersist, watchComposerDraftStorage,
  selectComposerDraftInputs, composerDraftInputsEqual, composerWorkspaceKey,
  hasComposerDraft, hasPendingComposerDrafts, flushComposerDrafts, composerDraftOf,
} from "./composer-drafts";

export const useAppStore = create<AppStore>()(
  subscribeWithSelector((...a) => {
    const state = {
      ...createUISlice(...a),
      ...createWorkspaceSlice(...a),
      ...createChatSlice(...a),
      ...createComposerSlice(...a),
      ...createAgentSlice(...a),
      ...createApprovalSlice(...a),
      ...createControlPlaneSlice(...a),
      ...createInspectorSlice(...a),
      ...createEditorSlice(...a),
    };
    return { ...state, contextCardCollapsed: state.rightPanelOpen || !state.contextCardPinned };
  }),
);

useAppStore.subscribe(
  state => [state.rightPanelOpen, state.compactPanel] as const,
  ([open, compactPanel], [wasOpen, previousCompactPanel]) => {
    if ((open && !wasOpen) || (compactPanel === "right" && previousCompactPanel !== "right")) {
      useAppStore.getState().setContextCardCollapsed(true);
    }
  },
  { equalityFn: (left, right) => left[0] === right[0] && left[1] === right[1] },
);

let composerSnapshotRevision = 0;
watchComposerDraftStorage(status => {
  // Storage status is republished on every scheduled write; only a real
  // change may notify the store's subscribers.
  useAppStore.setState(state => (
    state.composerDraftStoragePending === status.pending && state.composerDraftRevision === status.revision
      && state.composerDraftStorageError === status.error
      ? state
      : { composerDraftStoragePending: status.pending, composerDraftRevision: status.revision, composerDraftStorageError: status.error }
  ));
  if (status.error) pushToast(`无法保存输入草稿：${status.error}。请保留窗口和未发送内容。`, "error");
});
const composerRecovery = loadComposerDrafts().then(records => {
  const legacyCommentMigrations: { root: string; draftId: string }[] = [];
  useAppStore.setState(state => {
    const workspaceComposerDrafts = { ...state.workspaceComposerDrafts };
    const conversationWorkbenchStates = { ...state.conversationWorkbenchStates };
    const sideChats = { ...state.sideChats };
    const conversationDrafts = { ...state.conversationDrafts };
    const homeConversationDraftIds = { ...state.homeConversationDraftIds };
    const retainedComposerInputs = { ...state.retainedComposerInputs };
    for (const record of records) {
      if (record.retainedInput) {
        retainedComposerInputs[record.retainedInput.message.id] = { ...record.retainedInput, draft: record.draft, recovered: true,
          owner: { ...record.owner, kind: record.owner.kind as "conversation" | "draft" },
          message: retainedInputMessage(record.retainedInput.message, record.draft, record.retainedInput.unconfirmed) };
        continue;
      }
      if (record.owner.kind === "draft") {
        const context = conversationDrafts[record.owner.id]
          ?? { ...record.draftContext!, materializing: false, selectionLoading: true, preferencesLoading: false };
        conversationDrafts[context.id] = context;
        const body = state.workspaceComposerDrafts[record.owner.id] ?? record.draft;
        workspaceComposerDrafts[context.id] = { ...body, attachments: body.attachments.map(item => ({ ...item, draftId: context.id })) };
        const key = conversationDraftTargetKey(context.workspaceRoot, context.sidebarSectionId);
        if (!homeConversationDraftIds[key] || state.activeConversationDraftId === context.id) homeConversationDraftIds[key] = context.id;
      } else if (record.owner.kind === "workspace") {
        const context = newConversationDraftContext(state, record.owner.workspaceRoot, Boolean(record.owner.workspaceRoot));
        legacyCommentMigrations.push({ root: context.workspaceRoot, draftId: context.id });
        conversationDrafts[context.id] = context;
        const body = state.workspaceComposerDrafts[record.owner.id] ?? record.draft;
        workspaceComposerDrafts[context.id] = { ...body, attachments: body.attachments.map(item => ({ ...item, draftId: context.id })) };
        delete workspaceComposerDrafts[record.owner.id];
        homeConversationDraftIds[conversationDraftTargetKey(context.workspaceRoot)] = context.id;
      }
      else if (record.owner.kind === "conversation") {
        conversationWorkbenchStates[record.owner.id] = {
          ...emptyConversationWorkbenchState(), ...conversationWorkbenchStates[record.owner.id], ...record.draft,
        };
        if (record.draftContext) conversationDrafts[record.draftContext.id] = {
          ...record.draftContext, materializing: false, selectionLoading: false, preferencesLoading: false,
        };
      }
      else sideChats[record.owner.id] = {
        ...(sideChats[record.owner.id] ?? { id: record.owner.id, messages: [], isStreaming: false }),
        workspaceRoot: record.owner.workspaceRoot, ...record.side,
        draft: record.draft.draft, richDraft: record.draft.richDraft, attachments: record.draft.attachments,
      };
    }
    let activeConversationDraftId = state.activeConversationDraftId;
    if (activeConversationDraftId && !conversationDrafts[activeConversationDraftId]) activeConversationDraftId = null;
    if (!state.conversationId && !activeConversationDraftId && !readLS(LS.conversation.activeId)
      && !window.__MINICODE_RUNTIME__?.initialNavigation?.conversationId) {
      activeConversationDraftId = homeConversationDraftIds[conversationDraftTargetKey(state.workingDirectory)] ?? null;
      if (!activeConversationDraftId) {
        const context = newConversationDraftContext(state, state.workingDirectory, Boolean(state.workingDirectory), { appMode: state.appMode });
        activeConversationDraftId = context.id;
        conversationDrafts[context.id] = context;
        homeConversationDraftIds[conversationDraftTargetKey(context.workspaceRoot)] = context.id;
        workspaceComposerDrafts[context.id] = composerDraftOf(state);
      }
    }
    const context = activeConversationDraftId ? conversationDrafts[activeConversationDraftId] : undefined;
    const active = state.conversationId ? conversationWorkbenchStates[state.conversationId]
      : context?.adopted && context.conversationId ? conversationWorkbenchStates[context.conversationId]
        : context ? workspaceComposerDrafts[context.id] : workspaceComposerDrafts[composerWorkspaceKey(state.workingDirectory)];
    const recoveredInputs = { ...state, retainedComposerInputs, conversationWorkbenchStates,
      retainedInputEdit: !hasComposerDraft(state) && active ? active.retainedInputEdit : state.retainedInputEdit };
    return { workspaceComposerDrafts, conversationWorkbenchStates, conversationDrafts, homeConversationDraftIds,
      activeConversationDraftId, sideChats, retainedComposerInputs, composerDraftsReady: true,
      messages: state.conversationId ? mergeRetainedComposerMessages(recoveredInputs, state.conversationId, state.messages) : state.messages,
      conversationMessages: Object.fromEntries(Object.entries(state.conversationMessages)
        .map(([owner, messages]) => [owner, mergeRetainedComposerMessages(recoveredInputs, owner, messages)])),
      ...(context ? { workingDirectory: context.workspaceRoot, ...draftSelectionView(context) } : {}),
      ...(!hasComposerDraft(state) && active ? {
        agentMode: active.agentMode ?? "build",
        goalMode: active.goalMode ?? false,
        draft: active.draft, richDraft: active.richDraft, attachments: active.attachments, quotedMessage: active.quotedMessage,
        queuedEdit: active.queuedEdit,
        retainedInputEdit: active.retainedInputEdit,
        selectedMentions: active.selectedMentions, selectedSkills: active.selectedSkills,
        allowedRemoteImageDomains: active.allowedRemoteImageDomains,
      } : {}),
    };
  });
  reconcileRecoveredInputOutcomes();
  for (const migration of legacyCommentMigrations) transferDiffComments(getDiffCommentScope(null, migration.root), getDiffCommentScope(migration.draftId, migration.root));
  const recovered = useAppStore.getState();
  if (recovered.activeConversationDraftId) {
    writeLS(activeDraftStorageKey(), recovered.activeConversationDraftId);
    const recoveredId = recovered.activeConversationDraftId;
    const context = recovered.conversationDrafts[recoveredId];
    if (context.adopted && context.conversationId) {
      if (recovered.isConnected) recovered.requestConversationSwitch(context.conversationId);
      else {
        const stop = useAppStore.subscribe(state => {
          if (state.activeConversationDraftId !== recoveredId) { stop(); return; }
          if (!state.isConnected) return;
          stop();
          state.requestConversationSwitch(context.conversationId!);
        });
      }
    } else void recovered.refreshConversationDraftSelection(recoveredId);
  }
}).catch(error => {
  const message = error instanceof Error ? error.message : String(error);
  useAppStore.setState({ composerDraftStorageError: message });
  pushToast(`无法读取输入草稿：${message}。原有恢复数据未被覆盖。`, "error");
  throw error;
});
// The close/update boundary observes this same recovery promise. Reporting a
// read failure above does not turn an unknown old draft into an empty record.
void composerRecovery.catch(error => console.error("Composer draft recovery failed", error));
export const waitComposerDraftRecovery = () => composerRecovery;
useAppStore.subscribe(selectComposerDraftInputs, () => {
  if (!useAppStore.getState().composerDraftsReady) return;
  // Collect at write time so a typing burst persists its latest draft once.
  scheduleComposerDraftPersist(() => collectComposerDrafts(useAppStore.getState(), ++composerSnapshotRevision));
}, { equalityFn: composerDraftInputsEqual });

useAppStore.subscribe(
  (state) => [state.workingDirectory, state.editorTabs, state.activeTabPath, state.activeEditorPath] as const,
  ([workingDirectory, editorTabs, activeTabPath, activeEditorPath], previous) => {
    // A cold workspace's initially unknown empty UI is not a Close All action.
    if (workingDirectory !== previous[0] && !cachedEditorDrafts(editorWorkspaceKey(workingDirectory))
      && !editorTabs.some(tab => !tab.recoveryPending && tab.content !== tab.original)) return;
    persistEditorTabs(editorTabs, workingDirectory, { activeTabPath, activeEditorPath });
  },
  { equalityFn: (left, right) => left.every((value, index) => value === right[index]) },
);

const restoringWorkspaces = new Map<string, Promise<void>>();
export async function waitEditorDraftRecovery() {
  while (restoringWorkspaces.size) await Promise.all(restoringWorkspaces.values());
}
useAppStore.subscribe(
  state => [state.workingDirectory, state.editorTabs] as const,
  ([workspace, tabs]) => {
    if ((!tabs.some(tab => tab.recoveryPending) && cachedEditorDrafts(editorWorkspaceKey(workspace))) || restoringWorkspaces.has(workspace)) return;
    const initial = useAppStore.getState();
    const restoring = restoreWorkspaceEditorDrafts(workspace, initial).then(restored => {
      const location = editorWorkspaceRecoveryLocation(workspace);
      useAppStore.setState(state => workspaceRootsEqual(state.workingDirectory, workspace)
        ? { editorTabs: reconcileRestoredEditorTabs(state.editorTabs, restored, workspace),
            activeTabPath: state.activeTabPath === initial.activeTabPath ? location?.activeTabPath ?? restored[0]?.path ?? null : state.activeTabPath,
            activeEditorPath: state.activeEditorPath === initial.activeEditorPath ? location?.activeEditorPath ?? null : state.activeEditorPath } : {});
    }).catch(error => {
      console.error("Editor draft recovery load failed", error);
      useAppStore.setState(state => workspaceRootsEqual(state.workingDirectory, workspace)
        ? { editorTabs: state.editorTabs.map(tab => tab.recoveryPending && tab.draftRestorePending
            ? { ...tab, loading: false } : tab) } : {});
      pushToast(`无法读取编辑器恢复草稿：${error instanceof Error ? error.message : String(error)}。`, "error");
    }).finally(() => restoringWorkspaces.delete(workspace));
    restoringWorkspaces.set(workspace, restoring);
  },
  { fireImmediately: true, equalityFn: (left, right) => left.every((value, index) => value === right[index]) },
);

export const syncSystemTheme = () => {
  const state = useAppStore.getState();
  if (state.themeMode !== "system") return;
  const resolvedTheme = applyTheme("system", state);
  if (useAppStore.getState().resolvedTheme !== resolvedTheme) {
    useAppStore.setState({ resolvedTheme });
  }
};

// The transcript database remains authoritative. Keep only a small LRU of
// settled inactive conversations in renderer memory; active and streaming
// conversations are always pinned and rehydrate through conversation.switch.
const MAX_INACTIVE_CONVERSATION_TRANSCRIPTS = 8;
const conversationTranscriptAccess = new Map<string, number>();
let pruningConversationTranscripts = false;

type ConversationTranscriptPruneSnapshot = Pick<
  AppStore,
  "conversationId" | "conversationMessages" | "conversationStreaming" | "conversations"
>;

const selectConversationTranscriptPruneSnapshot = (
  state: AppStore,
): ConversationTranscriptPruneSnapshot => ({
  conversationId: state.conversationId,
  conversationMessages: state.conversationMessages,
  conversationStreaming: state.conversationStreaming,
  conversations: state.conversations,
});

const conversationTranscriptPruneSnapshotEqual = (
  previous: ConversationTranscriptPruneSnapshot,
  next: ConversationTranscriptPruneSnapshot,
) => {
  if (previous.conversationId !== next.conversationId || previous.conversationStreaming !== next.conversationStreaming
    || previous.conversations !== next.conversations) return false;
  if (previous.conversationMessages === next.conversationMessages) return true;
  const updatedOwner = changedTranscriptOwner(next.conversationMessages);
  // Active/running transcripts are pinned. Their text does not change the LRU
  // membership; record their final access when selection or run state changes.
  return updatedOwner !== undefined && (updatedOwner === next.conversationId || Boolean(next.conversationStreaming[updatedOwner]))
    && conversationTranscriptMembership(previous.conversationMessages) === conversationTranscriptMembership(next.conversationMessages);
};

useAppStore.subscribe(
  selectConversationTranscriptPruneSnapshot,
  (state, previous) => {
    if (pruningConversationTranscripts) return;
    const now = Date.now();
    for (const id of conversationTranscriptAccess.keys()) {
      if (id !== state.conversationId && !(id in state.conversationMessages)) conversationTranscriptAccess.delete(id);
    }
    if (state.conversationId) conversationTranscriptAccess.set(state.conversationId, now);
    for (const [id, messages] of Object.entries(state.conversationMessages)) {
      if (previous.conversationMessages[id] !== messages) {
        conversationTranscriptAccess.set(id, now);
      }
    }

    const inactive = Object.keys(state.conversationMessages).filter(
      (id) => id !== state.conversationId && !state.conversationStreaming[id],
    );
    if (inactive.length <= MAX_INACTIVE_CONVERSATION_TRANSCRIPTS) return;

    const conversationUpdatedAt = new Map(
      state.conversations.map((conversation) => [
        conversation.id,
        Date.parse(conversation.updatedAt || "") || 0,
      ]),
    );
    const evict = inactive
      .sort((left, right) => {
        const leftAccess = conversationTranscriptAccess.get(left) ?? 0;
        const rightAccess = conversationTranscriptAccess.get(right) ?? 0;
        if (leftAccess !== rightAccess) return leftAccess - rightAccess;
        return (conversationUpdatedAt.get(left) ?? 0) - (conversationUpdatedAt.get(right) ?? 0);
      })
      .slice(0, inactive.length - MAX_INACTIVE_CONVERSATION_TRANSCRIPTS);
    if (!evict.length) return;

    const next = { ...state.conversationMessages };
    const historyPages = { ...useAppStore.getState().conversationHistoryPages };
    for (const id of evict) {
      delete next[id];
      delete historyPages[id];
      conversationTranscriptAccess.delete(id);
    }
    pruningConversationTranscripts = true;
    try {
      useAppStore.setState({ conversationMessages: next, conversationHistoryPages: historyPages });
    } finally {
      pruningConversationTranscripts = false;
    }
  },
  { equalityFn: conversationTranscriptPruneSnapshotEqual },
);

if (typeof window !== "undefined") {
  window.addEventListener("storage", event => {
    if (event.storageArea !== localStorage) return;
    if (event.key === null || event.key === "minicode.workbench.preferences") {
      const stored = migrateWorkbenchPreferences(safeJsonParse<Partial<WorkbenchPreferences> & { speechVocabulary?: string }>(localStorage.getItem("minicode.workbench.preferences") ?? "{}", {}));
      const workbenchPreferences = usesDesktopDictationPreferences() ? { ...stored, ...dictationPreferencesOf(useAppStore.getState().workbenchPreferences) } : stored;
      useAppStore.setState({ workbenchPreferences });
      applyWorkbenchPreferences(workbenchPreferences);
    }
    if (event.key === null || [LS.textScale, LS.codeTextScale, LS.codeFontBase, LS.reducedMotion, LS.pointerCursors, LS.diffMarkerStyle].includes(event.key)) {
      const preferences = readAppearancePreferences();
      applyTextScale(preferences.textScale); applyCodeTextScale(preferences.codeTextScale, preferences.codeFontBase);
      applyReducedMotion(preferences.reducedMotion); applyPointerCursors(preferences.pointerCursors); applyDiffMarkerStyle(preferences.diffMarkerStyle);
      useAppStore.setState(preferences);
    }
    if (!isAppearanceStorageKey(event.key)) return;
    // Read the complete latest snapshot rather than replaying an older event
    // payload after multiple windows have written their separate variants.
    const appearance = readAppearanceStorage();
    const themeMode = initialTheme();
    const resolvedTheme = applyTheme(themeMode, appearance);
    useAppStore.setState({ ...appearance, themeMode, resolvedTheme });
  });
  window.addEventListener("beforeunload", event => {
    if (!hasPendingEditorDrafts() && useAppStore.getState().composerDraftsReady && !hasPendingComposerDrafts() && !dictationController.hasPendingAudio()) return;
    const desktop = window.__MINICODE_RUNTIME__?.desktop;
    const intent = desktop?.windowControls.captureUnloadIntent();
    event.preventDefault();
    event.returnValue = "";
    if (desktop && intent) void Promise.all([composerRecovery, flushComposerDrafts(), flushEditorDrafts(), dictationController.settle()])
      .then(() => desktop.windowControls.resumeUnload(intent.id)).catch(error => {
      console.error("Window remains open because authored drafts did not commit", error);
    });
  });
  (window as typeof window & { __zustandStore?: typeof useAppStore }).__zustandStore = useAppStore;
  const initialState = useAppStore.getState();
  const initialResolvedTheme = applyTheme(initialState.themeMode, initialState);
  if (useAppStore.getState().resolvedTheme !== initialResolvedTheme) {
    useAppStore.setState({ resolvedTheme: initialResolvedTheme });
  }
  applyTextScale(useAppStore.getState().textScale);
  applyCodeTextScale(useAppStore.getState().codeTextScale, useAppStore.getState().codeFontBase);
  applyWorkbenchPreferences(useAppStore.getState().workbenchPreferences);
  applyReducedMotion(useAppStore.getState().reducedMotion);
  applyPointerCursors(useAppStore.getState().pointerCursors);
  applyDiffMarkerStyle(useAppStore.getState().diffMarkerStyle);
  const motionMedia = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  motionMedia?.addEventListener("change", () => {
    const reducedMotion = reducedMotionFor(useAppStore.getState().reducedMotionPreference);
    applyReducedMotion(reducedMotion); useAppStore.setState({ reducedMotion });
  });
  const colorSchemeMedia = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: light)")
    : null;
  colorSchemeMedia?.addEventListener("change", syncSystemTheme);
}
