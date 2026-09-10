import type { StateCreator } from "zustand";
import type { AppStore, EditorSlice, EditorTab } from "./types";
import {
  editorPathsEqual,
  editorPathComparisonKey,
  cacheEditorStateForWorkspace,
  editorStateForWorkspace,
  ensureCodePanelSlots,
  loadPersistedEditorTabs,
  normalizeEditorPath,
  normalizePanelSlots,
  persistPanelSlots,
  persistEditorTabs,
  uniqueMessageId,
} from "./shared-helpers";
import { isPreviewableMediaPath } from "../lib/media-types";
import { workspaceRootsEqual } from "../lib/workspace-path";

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
  type WorkspaceEditorState = Pick<AppStore, "editorTabs" | "activeTabPath" | "activeEditorPath" | "editorOpenRequests">;
  const updateWorkspace = (workspaceRoot: string, update: (state: WorkspaceEditorState) => Partial<WorkspaceEditorState>) => {
    set((state) => {
      if (workspaceRootsEqual(workspaceRoot, state.workingDirectory)) return update(state);
      const cached = editorStateForWorkspace(workspaceRoot);
      const next = { ...cached, ...update(cached) };
      cacheEditorStateForWorkspace(workspaceRoot, next.editorTabs, next.activeTabPath, next.activeEditorPath);
      return {};
    });
  };
  return {
    editorTabs: initialEditorTabs,
    activeTabPath: initialEditorTabs[0]?.path ?? null,
    openEditorTab: (path, { activate = true } = {}) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const existing = s.editorTabs.find((t) => editorPathsEqual(t.path, normalizedPath, s.workingDirectory));
        const selection = activate ? {
          activeTabPath: existing?.path ?? normalizedPath,
          activeEditorPath: existing?.path ?? normalizedPath,
          activeEditorOpenRequestId: null,
        } : {};
        if (existing) {
          if (!existing.externalChanged || !isPreviewableMediaPath(normalizedPath)) {
            return selection;
          }
          return {
            ...selection,
            editorTabs: s.editorTabs.map((tab) => (
              editorPathsEqual(tab.path, existing.path, s.workingDirectory)
                ? { ...tab, externalChanged: false }
                : tab
            )),
          };
        }
        const tab: EditorTab = {
          id: uniqueMessageId("editor"),
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
        return { editorTabs: next, ...selection };
      }),
    closeEditorTab: (path) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const idx = s.editorTabs.findIndex((t) => editorPathsEqual(t.path, normalizedPath, s.workingDirectory));
        if (idx === -1) return {};
        const targetPath = s.editorTabs[idx].path;
        const editorOpenRequests = s.editorOpenRequests.filter((request) => !editorPathsEqual(request.path, targetPath, s.workingDirectory));
        const activeEditorOpenRequestId = editorOpenRequests.some((request) => request.id === s.activeEditorOpenRequestId)
          ? s.activeEditorOpenRequestId : null;
        const next = s.editorTabs.filter((t) => !editorPathsEqual(t.path, targetPath, s.workingDirectory));
        persistEditorTabs(next, s.workingDirectory);
        let activeTabPath = s.activeTabPath;
        if (editorPathsEqual(activeTabPath, targetPath, s.workingDirectory)) {
          activeTabPath = next[Math.max(0, idx - 1)]?.path ?? null;
        }
        const activeEditorPath = editorPathsEqual(s.activeEditorPath, targetPath, s.workingDirectory) ? activeTabPath : s.activeEditorPath;
        if (next.length > 0) return { editorTabs: next, activeTabPath, activeEditorPath, editorOpenRequests, activeEditorOpenRequestId };
        const panelSlots = panelSlotsAfterClosingLastEditor(s);
        persistPanelSlots(panelSlots);
        return {
          editorTabs: next,
          activeTabPath,
          activeEditorPath,
          editorOpenRequests,
          activeEditorOpenRequestId,
          panelSlots,
        };
      }),
    closeOtherEditorTabs: (path) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const selected = s.editorTabs.find((t) => editorPathsEqual(t.path, normalizedPath, s.workingDirectory));
        if (!selected) return {};
        const next = [selected];
        persistEditorTabs(next, s.workingDirectory);
        return {
          editorTabs: next,
          activeTabPath: selected.path,
          editorOpenRequests: [],
          activeEditorOpenRequestId: null,
          activeEditorPath: selected.path,
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
          editorOpenRequests: [],
          activeEditorOpenRequestId: null,
          panelSlots,
        };
      }),
    renameEditorPath: (path, newPath, workspaceRoot) => {
      const source = normalizeEditorPath(path, workspaceRoot);
      const destination = normalizeEditorPath(newPath, workspaceRoot);
      const sourceKey = editorPathComparisonKey(source, workspaceRoot);
      const renamedPath = (value: string): string => {
        const normalized = normalizeEditorPath(value, workspaceRoot);
        const key = editorPathComparisonKey(normalized, workspaceRoot);
        return key === sourceKey || key.startsWith(`${sourceKey}/`)
          ? `${destination}${normalized.slice(source.length)}`
          : value;
      };
      updateWorkspace(workspaceRoot, (state) => {
        const editorTabs = state.editorTabs.map((tab) => {
          const path = renamedPath(tab.path);
          return path === tab.path ? tab : { ...tab, path };
        });
        persistEditorTabs(editorTabs, workspaceRoot);
        return {
          editorTabs,
          activeTabPath: state.activeTabPath && renamedPath(state.activeTabPath),
          activeEditorPath: state.activeEditorPath && renamedPath(state.activeEditorPath),
          editorOpenRequests: state.editorOpenRequests.map((request) => {
            const path = renamedPath(request.path);
            return path === request.path ? request : { ...request, path };
          }),
        };
      });
      set((state) => {
        if (!workspaceRootsEqual(workspaceRoot, state.workingDirectory) || !state.activeTabPath) return {};
        const label = state.activeTabPath.split("/").at(-1)!;
        const panelSlots = state.panelSlots.map((slot) => slot.kind === "editor" ? { ...slot, label } : slot);
        persistPanelSlots(panelSlots);
        return { panelSlots };
      });
    },
    setActiveTab: (path) => set((s) => {
      const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
      const existing = s.editorTabs.find((tab) => editorPathsEqual(tab.path, normalizedPath, s.workingDirectory));
      const activePath = existing?.path ?? normalizedPath;
      return { activeTabPath: activePath, activeEditorPath: activePath, activeEditorOpenRequestId: null };
    }),
    updateTabContent: (path, content) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        return {
          editorTabs: s.editorTabs.map((t) =>
            editorPathsEqual(t.path, normalizedPath, s.workingDirectory) && !t.readOnly ? { ...t, content } : t
          ),
        };
      }),
    markTabLoaded: (path, content, error, contentHash, meta) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const loadedTabs = s.editorTabs.map((t) =>
          editorPathsEqual(t.path, normalizedPath, s.workingDirectory)
            ? {
                ...t,
                content,
                original: content,
                contentHash,
                externalChanged: false,
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
    markTabSaved: (path, savedContent, contentHash, sizeBytes, workspaceRoot = get().workingDirectory) =>
      updateWorkspace(workspaceRoot, (s) => {
        const normalizedPath = normalizeEditorPath(path, workspaceRoot);
        return {
          editorTabs: s.editorTabs.map((tab) =>
            editorPathsEqual(tab.path, normalizedPath, workspaceRoot)
              ? { ...tab, original: savedContent, contentHash, sizeBytes, externalChanged: false, error: null }
              : tab,
          ),
        };
      }),
    markTabExternalChanged: (path, { workspaceRoot = get().workingDirectory, changed = true } = {}) =>
      updateWorkspace(workspaceRoot, (s) => {
        const normalized = path.replace(/\\/g, "/");
        return {
          editorTabs: s.editorTabs.map((t) => {
            return editorPathsEqual(t.path, normalized, workspaceRoot)
              ? { ...t, externalChanged: changed }
              : t;
          }),
        };
      }),
    insertIntoActiveEditor: (text) => {
      const state = get();
      const path = state.activeTabPath;
      if (!path) return false;
      const tab = state.editorTabs.find((t) => editorPathsEqual(t.path, path, state.workingDirectory));
      if (!tab || tab.loading || tab.error || tab.largeFile || tab.readOnly) return false;
      set((s) => ({
        editorTabs: s.editorTabs.map((t) =>
          editorPathsEqual(t.path, path, s.workingDirectory)
            ? { ...t, content: t.content ? `${t.content}\n${text}` : text }
            : t,
        ),
        activeEditorPath: path,
      }));
      return true;
    },
  };
};
