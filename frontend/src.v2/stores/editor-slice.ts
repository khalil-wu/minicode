import type { StateCreator } from "zustand";
import type { AppStore, EditorSlice, EditorTab } from "./types";
import {
  editorPathsEqual,
  editorPathComparisonKey,
  editorWorkspaceKey,
  cacheEditorStateForWorkspace,
  editorStateForWorkspace,
  ensureCodePanelSlots,
  normalizeEditorPath,
  normalizePanelSlots,
  persistPanelSlots,
  persistEditorTabs,
  renameEditorViewState,
  readLS,
  writeLS,
  LS,
  uniqueMessageId,
} from "./shared-helpers";
import { isPreviewableMediaPath } from "../lib/media-types";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { safeJsonParse } from "../lib/safe-parse";
import { cachedEditorDrafts } from "./editor-drafts";
import { applyWorkbenchPreferences, defaultWorkbenchPreferences, type WorkbenchPreferences } from "../lib/workbench-preferences";

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
  const initialEditorState = editorStateForWorkspace("");
  type WorkspaceEditorState = Pick<AppStore, "editorTabs" | "activeTabPath" | "activeEditorPath" | "editorOpenRequests">;
  const updateWorkspace = (workspaceRoot: string, update: (state: WorkspaceEditorState) => Partial<WorkspaceEditorState>) => {
    set((state) => {
      if (workspaceRootsEqual(workspaceRoot, state.workingDirectory)) return update(state);
      const cached = editorStateForWorkspace(workspaceRoot);
      const next = { ...cached, ...update(cached) };
      persistEditorTabs(next.editorTabs, workspaceRoot, next);
      cacheEditorStateForWorkspace(workspaceRoot, next.editorTabs, next.activeTabPath, next.activeEditorPath);
      return {};
    });
  };
  return {
    inlineCompletionUsage: { requests: 0, inputTokens: 0, outputTokens: 0, lastError: "", pending: false },
    workbenchPreferences: { ...defaultWorkbenchPreferences, ...safeJsonParse<Partial<WorkbenchPreferences>>(readLS("minicode.workbench.preferences") ?? "{}", {}) },
    setWorkbenchPreferences: (patch) => set((state) => {
      const workbenchPreferences = { ...state.workbenchPreferences, ...patch };
      writeLS("minicode.workbench.preferences", JSON.stringify(workbenchPreferences));
      applyWorkbenchPreferences(workbenchPreferences);
      return { workbenchPreferences };
    }),
    pinEditorTab: (path, pinned) => set((state) => ({
      editorTabs: state.editorTabs.map((tab) => editorPathsEqual(tab.path, path, state.workingDirectory) ? { ...tab, pinned, preview: false } : tab)
        .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))),
    })),
    keepEditorTab: (path) => set((state) => ({
      editorTabs: state.editorTabs.map((tab) => editorPathsEqual(tab.path, path, state.workingDirectory) ? { ...tab, preview: false } : tab),
    })),
    setEditorTabLanguage: (path, language) => set((state) => ({
      editorTabs: state.editorTabs.map((tab) => editorPathsEqual(tab.path, path, state.workingDirectory) ? { ...tab, language } : tab),
    })),
    ...initialEditorState,
    editorExplorerOpen: true,
    setEditorExplorerOpen: (open) => set({ editorExplorerOpen: open }),
    workspaceSearchOpen: false,
    openWorkspaceSearch: () => set((state) => {
      const panelSlots = ensureCodePanelSlots(state.panelSlots).map((slot) => ({ ...slot, focused: slot.kind === "editor" }));
      persistPanelSlots(panelSlots);
      return {
        appMode: "code",
        panelSlots,
        editorExplorerOpen: true,
        workspaceSearchOpen: true,
        rightPanelExpanded: false,
        settingsOpen: false,
        skillsMarketplaceOpen: false,
      };
    }),
    closeWorkspaceSearch: () => set({ workspaceSearchOpen: false }),
    queueEditorBufferTransaction: (path, transaction, original, contentHash, workspaceRoot) => updateWorkspace(workspaceRoot, (state) => {
      const existing = state.editorTabs.find((tab) => editorPathsEqual(tab.path, path, workspaceRoot));
      const tab = existing ?? { id: uniqueMessageId("editor"), path, content: transaction.before, original, contentHash, loading: false, error: null };
      const updated = { ...tab, content: transaction.after, recoveryPending: false, preview: false, pendingBufferTransactions: [...(tab.pendingBufferTransactions ?? []), transaction] };
      return { editorTabs: existing ? state.editorTabs.map((entry) => entry === existing ? updated : entry) : [...state.editorTabs, updated] };
    }),
    consumeEditorBufferTransactions: (path, workspaceRoot) => updateWorkspace(workspaceRoot, (state) => ({
      editorTabs: state.editorTabs.map((tab) => editorPathsEqual(tab.path, path, workspaceRoot) ? { ...tab, pendingBufferTransactions: undefined } : tab),
    })),
    agentEditReviewKept: safeJsonParse<Record<string, string[]>>(readLS(LS.editorReviewKept) ?? "{}", {}),
    keepAgentEditBlocks: (scope, keys) => set((state) => {
      const agentEditReviewKept = { ...state.agentEditReviewKept, [scope]: [...new Set([...(state.agentEditReviewKept[scope] ?? []), ...keys])] };
      writeLS(LS.editorReviewKept, JSON.stringify(agentEditReviewKept));
      return { agentEditReviewKept };
    }),
    openEditorTab: (path, { activate = true, preview = false } = {}) =>
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
            return { ...selection, editorTabs: s.editorTabs.map((tab) => tab === existing ? { ...tab, lastActivated: activate ? Date.now() : tab.lastActivated, preview: preview && tab.preview } : tab) };
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
          recoveryPending: cachedEditorDrafts(editorWorkspaceKey(s.workingDirectory)) === undefined,
          error: null,
          largeFile: false,
          loadWarning: null,
          sizeBytes: undefined,
          readOnly: false,
          preview: preview && s.workbenchPreferences.previewTabs,
          lastActivated: activate ? Date.now() : 0,
        };
        const next = [...s.editorTabs.filter((entry) => !tab.preview || !entry.preview || entry.pinned || entry.content !== entry.original), tab];
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
          activeTabPath = [...next].sort((a, b) => (b.lastActivated ?? 0) - (a.lastActivated ?? 0))[0]?.path ?? null;
        }
        const activeEditorPath = editorPathsEqual(s.activeEditorPath, targetPath, s.workingDirectory) ? activeTabPath : s.activeEditorPath;
        if (next.length > 0) return { editorTabs: next, activeTabPath, activeEditorPath, editorOpenRequests, activeEditorOpenRequestId };
        const panelSlots = panelSlotsAfterClosingLastEditor(s);
        persistPanelSlots(panelSlots);
        return {
          editorTabs: next,
          appMode: s.appMode === "code" ? "cowork" : s.appMode,
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
          appMode: s.appMode === "code" ? "cowork" : s.appMode,
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
          if (path !== tab.path) renameEditorViewState(workspaceRoot, tab.path, path);
          return path === tab.path ? tab : { ...tab, path, recoveryPath: tab.recoveryPending ? tab.recoveryPath ?? tab.path : undefined };
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
        const workspaceKey = editorWorkspaceKey(workspaceRoot);
        const agentEditReviewKept = Object.fromEntries(Object.entries(state.agentEditReviewKept).map(([scope, keys]) => {
          const owner = JSON.parse(scope) as string[];
          if (owner[0] === workspaceKey) owner[3] = editorPathComparisonKey(renamedPath(owner[3]), workspaceRoot);
          return [JSON.stringify(owner), keys];
        }));
        writeLS(LS.editorReviewKept, JSON.stringify(agentEditReviewKept));
        return { agentEditReviewKept };
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
      return { activeTabPath: activePath, activeEditorPath: activePath, activeEditorOpenRequestId: null,
        editorTabs: s.editorTabs.map((tab) => tab === existing ? { ...tab, lastActivated: Date.now() } : tab) };
    }),
    updateTabContent: (path, content) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        return {
          editorTabs: s.editorTabs.map((t) =>
            editorPathsEqual(t.path, normalizedPath, s.workingDirectory) && !t.readOnly ? { ...t, content, recoveryPending: false, preview: content !== t.original ? false : t.preview, draftRestored: t.draftRestored && content !== t.original } : t
          ),
        };
      }),
    adoptEditorModelChanges: (changes, workspaceRoot) => {
      updateWorkspace(workspaceRoot, (state) => {
        let editorTabs = state.editorTabs;
        for (const change of changes) {
          const path = normalizeEditorPath(change.path, workspaceRoot);
          const existing = editorTabs.find((tab) => editorPathsEqual(tab.path, path, workspaceRoot));
          if (existing) {
            editorTabs = editorTabs.map((tab) => tab === existing ? { ...tab, content: change.content, recoveryPending: false, preview: change.content !== tab.original ? false : tab.preview, draftRestored: tab.draftRestored && change.content !== tab.original } : tab);
          } else if (change.content !== change.original) {
            editorTabs = [...editorTabs, {
              id: uniqueMessageId("editor"), path, content: change.content, original: change.original,
              contentHash: change.contentHash, sizeBytes: change.sizeBytes,
              loading: false, error: null, externalChanged: false, readOnly: false, largeFile: false,
            }];
          }
        }
        persistEditorTabs(editorTabs, workspaceRoot);
        return { editorTabs };
      });
    },
    markTabLoaded: (path, content, error, contentHash, meta) =>
      set((s) => {
        const normalizedPath = normalizeEditorPath(path, s.workingDirectory);
        const loadedTabs = s.editorTabs.map((t) =>
          editorPathsEqual(t.path, normalizedPath, s.workingDirectory)
            ? {
                ...t,
                recoveryPending: false,
                content: t.draftRestorePending ? t.content : content,
                original: t.draftRestorePending ? t.original : content,
                contentHash: t.draftRestorePending && content !== t.original ? t.contentHash : contentHash,
                externalChanged: Boolean(t.draftRestorePending && (error || content !== t.original)),
                loading: false,
                error: t.draftRestorePending ? null : error ?? null,
                largeFile: t.draftRestorePending ? false : Boolean(meta?.largeFile),
                loadWarning: t.draftRestorePending && error ? error : meta?.loadWarning ?? null,
                sizeBytes: meta?.sizeBytes,
                readOnly: Boolean(meta?.readOnly),
                draftRestorePending: false,
                draftRestored: Boolean(t.draftRestorePending),
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
              ? { ...tab, original: savedContent, contentHash, sizeBytes, externalChanged: false, error: null, draftRestored: false }
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
  };
};
