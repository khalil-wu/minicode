import type { StateCreator } from "zustand";
import type { AppStore, InspectorSlice } from "./types";

export const createInspectorSlice: StateCreator<AppStore, [], [], InspectorSlice> = (set, get) => ({
  inspectorEntries: [],
  inspectorFocus: null,
  addInspectorEntry: (entry) => {
    const rawOwner = entry.conversationId ?? entry.payload.conversation_id ?? entry.payload.conversationId;
    const conversationId = typeof rawOwner === "string" ? rawOwner.trim() || undefined : undefined;
    const ownedEntry = conversationId ? { ...entry, conversationId } : entry;
    set((s) => ({
      inspectorEntries: [
        ...s.inspectorEntries.filter((candidate) => !(
          candidate.conversationId === conversationId
          && candidate.targetKind === entry.targetKind && candidate.targetId === entry.targetId
        )).slice(-49),
        ownedEntry,
      ],
    }));
  },
  setInspectorFocus: (focus) => set({ inspectorFocus: focus
    ? { ...focus, conversationId: focus.conversationId ?? get().conversationId ?? undefined }
    : null }),
  clearInspector: () => set({ inspectorEntries: [], inspectorFocus: null }),
});
