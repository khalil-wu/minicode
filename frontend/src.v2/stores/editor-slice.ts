import type { StateCreator } from "zustand";
import type { AppStore, EditorSlice, EditorTab } from "./types";
import {
  ensureCodePanelSlots,
  loadPersistedEditorTabs,
  normalizeEditorPath,
  normalizePanelSlots,
  persistPanelSlots,
  persistEditorTabs,
} from "./shared-helpers";

const panelSlotsAfterClosingLastEditor = (state: AppStore) => {
  if (state.appMode !== "code") {
    return normalizePanelSlots(state.panelSlots.filter((slot) => slot.kind !== "editor"));
  }
  return normalizePanelSlots(
    ensureCodePanelSlots(state.panelSlots).map((slot) => ({
      ...slot,
      label: slot.kind === "editor" ? "File" : slot.label,
      focused: slot.kind === "chat",
      maximized: false,
    })),
  );
};

export const createEditorSlice: StateCreator<AppStore, [], [], EditorSlice> = (set, get) => {
  const initialEditorTabs = loadPersistedEditorTabs("");
  return {
    editorTabs: initialEditorTabs,
    activeTabPath: initialEditorTabs[0]?.path ?? null,
    openEditorTab: (path) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const existing = s.editorTabs.find((t) => t.path === normalizedPath);
        if (existing) return { activeTabPath: normalizedPath };
        const tab: EditorTab = {
          path: normalizedPath,
          content: "",
          original: "",
          loading: true,
          error: null,
          largeFile: false,
          loadWarning: null,
          sizeBytes: undefined,
          readOnly: false,
        };
        const next = [...s.editorTabs, tab];
        persistEditorTabs(next, s.workingDirectory);
        return { editorTabs: next, activeTabPath: normalizedPath };
      }),
    closeEditorTab: (path) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const idx = s.editorTabs.findIndex((t) => t.path === normalizedPath);
        if (idx === -1) return {};
        const next = s.editorTabs.filter((t) => t.path !== normalizedPath);
        persistEditorTabs(next, s.workingDirectory);
        let activeTabPath = s.activeTabPath;
        if (activeTabPath === normalizedPath) {
          activeTabPath = next[Math.max(0, idx - 1)]?.path ?? null;
        }
        const activeEditorPath = s.activeEditorPath === normalizedPath ? activeTabPath : s.activeEditorPath;
        if (next.length > 0) return { editorTabs: next, activeTabPath, activeEditorPath };
        const panelSlots = panelSlotsAfterClosingLastEditor(s);
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
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const next = s.editorTabs.filter((t) => t.path === normalizedPath);
        persistEditorTabs(next, s.workingDirectory);
        return {
          editorTabs: next,
          activeTabPath: normalizedPath,
          activeEditorPath: s.activeEditorPath && s.activeEditorPath !== normalizedPath ? normalizedPath : s.activeEditorPath,
        };
      }),
    closeAllEditorTabs: () =>
      set((s) => {
        persistEditorTabs([], s.workingDirectory);
        const panelSlots = panelSlotsAfterClosingLastEditor(s);
        persistPanelSlots(panelSlots);
        return {
          editorTabs: [],
          activeTabPath: null,
          activeEditorPath: null,
          panelSlots,
        };
      }),
    setActiveTab: (path) => set((s) => ({ activeTabPath: normalizeEditorPath(path, s.workingDirectory) })),
    updateTabContent: (path, content) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        return {
          editorTabs: s.editorTabs.map((t) =>
            t.path === normalizedPath && !t.readOnly ? { ...t, content } : t
          ),
        };
      }),
    markTabLoaded: (path, content, error, contentHash, meta) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const loadedTabs = s.editorTabs.map((t) =>
          t.path === normalizedPath
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
                readOnly: Boolean(meta?.readOnly),
              }
            : t,
        );
        persistEditorTabs(loadedTabs, s.workingDirectory);
        return { editorTabs: loadedTabs };
      }),
    markTabSaved: (path, savedContent, contentHash) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        return {
          editorTabs: s.editorTabs.map((t) =>
            t.path === normalizedPath
              ? { ...t, original: savedContent, contentHash, externalChanged: false, error: null }
              : t,
          ),
        };
      }),
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
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        return {
          editorTabs: s.editorTabs.map((t) =>
            t.path === normalizedPath
              ? { ...t, content, original: content, contentHash, externalChanged: false, loading: false, error: null }
              : t,
          ),
        };
      }),
    insertIntoActiveEditor: (text) => {
      const state = get();
      const path = state.activeTabPath;
      if (!path) return false;
      const tab = state.editorTabs.find((t) => t.path === path);
      if (!tab || tab.loading || tab.error || tab.largeFile || tab.readOnly) return false;
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
