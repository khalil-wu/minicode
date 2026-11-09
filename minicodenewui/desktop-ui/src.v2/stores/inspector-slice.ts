import type { StateCreator } from "zustand";
import type { AppStore, InspectorSlice } from "./types";

export const createInspectorSlice: StateCreator<AppStore, [], [], InspectorSlice> = (set, get) => ({
  inspectorEntries: [],
  inspectorFocus: null,
  addInspectorEntry: (entry) => get().addInspectorEntries([entry]),
  addInspectorEntries: (entries) => {
    if (!entries.length) return;
    set((s) => ({
      inspectorEntries: entries.reduce((current, entry) => {
        const rawOwner = entry.conversationId ?? entry.payload.conversation_id ?? entry.payload.conversationId;
        const conversationId = typeof rawOwner === "string" ? rawOwner.trim() || undefined : undefined;
        const ownedEntry = conversationId ? { ...entry, conversationId } : entry;
        return [
          ...current.filter((candidate) => !(
            candidate.conversationId === conversationId
            && candidate.targetKind === entry.targetKind && candidate.targetId === entry.targetId
          )).slice(-49),
          ownedEntry,
        ];
      }, s.inspectorEntries),
    }));
  },
  setInspectorFocus: (focus) => set({ inspectorFocus: focus
    ? { ...focus, conversationId: focus.conversationId ?? get().conversationId ?? undefined }
    : null }),
  clearInspector: () => set({ inspectorEntries: [], inspectorFocus: null }),
});
