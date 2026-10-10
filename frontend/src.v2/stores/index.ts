import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { AppStore } from "./types";

import { createUISlice } from "./ui-slice";
import { createWorkspaceSlice } from "./workspace-slice";
import { createChatSlice } from "./chat-slice";
import { createComposerSlice } from "./composer-slice";
import { createAgentSlice } from "./agent-slice";
import { createApprovalSlice } from "./approval-slice";
import { createControlPlaneSlice } from "./control-plane-slice";
import { createInspectorSlice } from "./inspector-slice";
import { createEditorSlice } from "./editor-slice";
import { applyWorkbenchPreferences } from "../lib/workbench-preferences";
import { applyCodeTextScale, applyReducedMotion, applyTheme, applyTextScale, persistEditorTabs, restoreWorkspaceEditorDrafts, reconcileRestoredEditorTabs, editorWorkspaceKey, editorWorkspaceRecoveryLocation } from "./shared-helpers";
import { flushEditorDrafts, hasPendingEditorDrafts, cachedEditorDrafts } from "./editor-drafts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { pushToast } from "../overlays/ToastContainer";

export const useAppStore = create<AppStore>()(
  subscribeWithSelector((...a) => ({
    ...createUISlice(...a),
    ...createWorkspaceSlice(...a),
    ...createChatSlice(...a),
    ...createComposerSlice(...a),
    ...createAgentSlice(...a),
    ...createApprovalSlice(...a),
    ...createControlPlaneSlice(...a),
    ...createInspectorSlice(...a),
    ...createEditorSlice(...a),
  })),
);

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

const restoringWorkspaces = new Set<string>();
useAppStore.subscribe(
  state => [state.workingDirectory, state.editorTabs] as const,
  ([workspace, tabs]) => {
    if ((!tabs.some(tab => tab.recoveryPending) && cachedEditorDrafts(editorWorkspaceKey(workspace))) || restoringWorkspaces.has(workspace)) return;
    restoringWorkspaces.add(workspace);
    const initial = useAppStore.getState();
    void restoreWorkspaceEditorDrafts(workspace, initial).then(restored => {
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
  },
  { fireImmediately: true, equalityFn: (left, right) => left.every((value, index) => value === right[index]) },
);

export const syncSystemTheme = () => {
  if (useAppStore.getState().themeMode !== "system") return;
  const resolvedTheme = applyTheme("system");
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
  left: ConversationTranscriptPruneSnapshot,
  right: ConversationTranscriptPruneSnapshot,
) => (
  left.conversationId === right.conversationId
  && left.conversationMessages === right.conversationMessages
  && left.conversationStreaming === right.conversationStreaming
  && left.conversations === right.conversations
);

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
  window.addEventListener("beforeunload", event => {
    if (!hasPendingEditorDrafts()) return;
    event.preventDefault();
    event.returnValue = "";
    const desktop = window.__MINICODE_RUNTIME__?.desktop;
    if (desktop) void flushEditorDrafts().then(() => desktop.windowControls.close()).catch(error => {
      console.error("Window remains open because editor recovery did not commit", error);
    });
  });
  (window as typeof window & { __zustandStore?: typeof useAppStore }).__zustandStore = useAppStore;
  const initialResolvedTheme = applyTheme(useAppStore.getState().themeMode);
  if (useAppStore.getState().resolvedTheme !== initialResolvedTheme) {
    useAppStore.setState({ resolvedTheme: initialResolvedTheme });
  }
  applyTextScale(useAppStore.getState().textScale);
  applyCodeTextScale(useAppStore.getState().codeTextScale);
  applyWorkbenchPreferences(useAppStore.getState().workbenchPreferences);
  applyReducedMotion(useAppStore.getState().reducedMotion);
  const colorSchemeMedia = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: light)")
    : null;
  colorSchemeMedia?.addEventListener("change", syncSystemTheme);
}
