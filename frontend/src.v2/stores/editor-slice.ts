import type { StateCreator } from "zustand";
import type { AppStore, EditorSlice, EditorTab } from "./types";
import {
  loadPersistedEditorTabs,
  normalizePanelSlots,
  persistPanelSlots,
  persistEditorTabs,
} from "./shared-helpers";

export const createEditorSlice: StateCreator<AppStore, [], [], EditorSlice> = (set, get) => {
  const initialEditorTabs = loadPersistedEditorTabs("");
  return {
    editorTabs: initialEditorTabs,
    activeTabPath: initialEditorTabs[0]?.path ?? null,
    openEditorTab: (path) =>
      set((s) => {
        const existing = s.editorTabs.find((t) => t.path === path);
        if (existing) return { activeTabPath: path };
        const tab: EditorTab = {
          path,
          content: "",
          original: "",
          loading: true,
          error: null,
          largeFile: false,
          loadWarning: null,
          sizeBytes: undefined,
        };
        const next = [...s.editorTabs, tab];
        persistEditorTabs(next, s.workingDirectory);
        return { editorTabs: next, activeTabPath: path };
      }),
    closeEditorTab: (path) =>
      set((s) => {
        const idx = s.editorTabs.findIndex((t) => t.path === path);
        if (idx === -1) return {};
        const next = s.editorTabs.filter((t) => t.path !== path);
        persistEditorTabs(next, s.workingDirectory);
        let activeTabPath = s.activeTabPath;
        if (activeTabPath === path) {
          activeTabPath = next[Math.max(0, idx - 1)]?.path ?? null;
        }
        const activeEditorPath = s.activeEditorPath === path ? activeTabPath : s.activeEditorPath;
        if (next.length > 0) return { editorTabs: next, activeTabPath, activeEditorPath };
        const panelSlots = normalizePanelSlots(
          s.panelSlots.filter((slot) => slot.kind !== "editor"),
        );
        persistPanelSlots(panelSlots);
        return {
          editorTabs: next,
          activeTabPath,
          activeEditorPath,
          panelSlots,
        };
      }),
    closeOtherEditorTabs: (path) =>
      set((s) => {
        const next = s.editorTabs.filter((t) => t.path === path);
        persistEditorTabs(next, s.workingDirectory);
        return {
          editorTabs: next,
          activeTabPath: path,
          activeEditorPath: s.activeEditorPath && s.activeEditorPath !== path ? path : s.activeEditorPath,
        };
      }),
    closeAllEditorTabs: () =>
      set((s) => {
        persistEditorTabs([], s.workingDirectory);
        const panelSlots = normalizePanelSlots(
          s.panelSlots.filter((slot) => slot.kind !== "editor"),
        );
        persistPanelSlots(panelSlots);
        return {
          editorTabs: [],
          activeTabPath: null,
          activeEditorPath: null,
          panelSlots,
        };
      }),
    setActiveTab: (path) => set({ activeTabPath: path }),
    updateTabContent: (path, content) =>
      set((s) => ({
        editorTabs: s.editorTabs.map((t) => (t.path === path ? { ...t, content } : t)),
      })),
    markTabLoaded: (path, content, error, contentHash, meta) =>
      set((s) => {
        const loadedTabs = s.editorTabs.map((t) =>
          t.path === path
            ? {
                ...t,
                content,
                original: content,
                contentHash,
                loading: false,
                error: error ?? null,
                largeFile: Boolean(meta?.largeFile),
                loadWarning: meta?.loadWarning ?? null,
                sizeBytes: meta?.sizeBytes,
              }
            : t,
        );
        if (!error) {
          persistEditorTabs(loadedTabs, s.workingDirectory);
          return { editorTabs: loadedTabs };
        }
        const editorTabs = loadedTabs.filter((tab) => tab.path !== path);
        persistEditorTabs(editorTabs, s.workingDirectory);
        return {
          editorTabs,
          activeTabPath: s.activeTabPath === path ? editorTabs[0]?.path ?? null : s.activeTabPath,
          activeEditorPath: s.activeEditorPath === path ? editorTabs[0]?.path ?? null : s.activeEditorPath,
        };
      }),
    markTabSaved: (path, contentHash) =>
      set((s) => ({
        editorTabs: s.editorTabs.map((t) =>
          t.path === path
            ? { ...t, original: t.content, contentHash, externalChanged: false, error: null }
            : t,
        ),
      })),
    markTabExternalChanged: (path) =>
      set((s) => {
        const normalized = path.replace(/\\/g, "/");
        return {
          editorTabs: s.editorTabs.map((t) => {
            const tp = t.path.replace(/\\/g, "/");
            return tp === normalized || tp.endsWith(`/${normalized}`)
              ? { ...t, externalChanged: true }
              : t;
          }),
        };
      }),
    reloadTab: (path, content, contentHash) =>
      set((s) => ({
        editorTabs: s.editorTabs.map((t) =>
          t.path === path
            ? { ...t, content, original: content, contentHash, externalChanged: false, loading: false, error: null }
            : t,
        ),
      })),
    insertIntoActiveEditor: (text) => {
      const state = get();
      const path = state.activeTabPath;
      if (!path) return false;
      const tab = state.editorTabs.find((t) => t.path === path);
      if (!tab || tab.loading || tab.error || tab.largeFile) return false;
      set((s) => ({
        editorTabs: s.editorTabs.map((t) =>
          t.path === path
            ? { ...t, content: t.content ? `${t.content}\n${text}` : text }
            : t,
        ),
        activeEditorPath: path,
      }));
      return true;
    },
  };
};
