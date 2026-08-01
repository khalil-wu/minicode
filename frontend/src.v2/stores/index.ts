import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { AppStore } from "./types";

import { createUISlice } from "./ui-slice";
import { createWorkspaceSlice } from "./workspace-slice";
import { createChatSlice } from "./chat-slice";
import { createComposerSlice } from "./composer-slice";
import { createAgentSlice } from "./agent-slice";
import { createApprovalSlice } from "./approval-slice";
import { createInspectorSlice } from "./inspector-slice";
import { createEditorSlice } from "./editor-slice";
import { applyTheme, applyTextScale } from "./shared-helpers";

export const useAppStore = create<AppStore>()(
  subscribeWithSelector((...a) => ({
    ...createUISlice(...a),
    ...createWorkspaceSlice(...a),
    ...createChatSlice(...a),
    ...createComposerSlice(...a),
    ...createAgentSlice(...a),
    ...createApprovalSlice(...a),
    ...createInspectorSlice(...a),
    ...createEditorSlice(...a),
  })),
);

// The transcript database remains authoritative. Keep only a small LRU of
// settled inactive conversations in renderer memory; active and streaming
// conversations are always pinned and rehydrate through conversation.switch.
const MAX_INACTIVE_CONVERSATION_TRANSCRIPTS = 8;
const conversationTranscriptAccess = new Map<string, number>();
let pruningConversationTranscripts = false;

useAppStore.subscribe((state, previous) => {
  if (pruningConversationTranscripts) return;
  const now = Date.now();
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

  const evict = inactive
    .sort((left, right) => {
      const leftAccess = conversationTranscriptAccess.get(left) ?? 0;
      const rightAccess = conversationTranscriptAccess.get(right) ?? 0;
      if (leftAccess !== rightAccess) return leftAccess - rightAccess;
      const leftUpdated = Date.parse(state.conversations.find((item) => item.id === left)?.updatedAt || "") || 0;
      const rightUpdated = Date.parse(state.conversations.find((item) => item.id === right)?.updatedAt || "") || 0;
      return leftUpdated - rightUpdated;
    })
    .slice(0, inactive.length - MAX_INACTIVE_CONVERSATION_TRANSCRIPTS);
  if (!evict.length) return;

  const next = { ...state.conversationMessages };
  for (const id of evict) {
    delete next[id];
    conversationTranscriptAccess.delete(id);
  }
  pruningConversationTranscripts = true;
  try {
    useAppStore.setState({ conversationMessages: next });
  } finally {
    pruningConversationTranscripts = false;
  }
});

if (typeof window !== "undefined") {
  (window as typeof window & { __zustandStore?: typeof useAppStore }).__zustandStore = useAppStore;
  applyTheme(useAppStore.getState().themeMode);
  applyTextScale(useAppStore.getState().textScale);
  const colorSchemeMedia = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: light)")
    : null;
  colorSchemeMedia?.addEventListener("change", () => {
    if (useAppStore.getState().themeMode === "system") applyTheme("system");
  });
}
