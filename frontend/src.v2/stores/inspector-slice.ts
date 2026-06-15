import type { StateCreator } from "zustand";
import type { AppStore, InspectorSlice } from "./types";

export const createInspectorSlice: StateCreator<AppStore, [], [], InspectorSlice> = (set, get) => ({
  inspectorEntries: [],
  inspectorFocus: null,
  addInspectorEntry: (entry) =>
    set((s) => ({
      inspectorEntries: [...s.inspectorEntries.slice(-49), entry],
    })),
  setInspectorFocus: (focus) => set({ inspectorFocus: focus }),
  clearInspector: () => set({ inspectorEntries: [], inspectorFocus: null }),
});
