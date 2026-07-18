import type { StateCreator } from "zustand";
import type { AppStore, PanelKind, PanelSlot, UISlice, WorkspaceSlice } from "./types";
import { clamp } from "../lib/clamp";
import { isDesktop, openPath } from "../desktop/runtime";
import {
  LS,
  LEFT_SIDEBAR_MAX_WIDTH,
  LEFT_SIDEBAR_MIN_WIDTH,
  RIGHT_SIDEBAR_MAX,
  writeLS,
  normalizePanelSlots,
  defaultPanelSlots,
  persistPanelSlots,
  loadInitialLayout,
  preferredRightSidebarWidth,
} from "./shared-helpers";

const normalizeEditorOpenPath = (path: string, workingDirectory = ""): string => {
  const raw = String(path || "").trim().replace(/\\/g, "/");
  if (!raw) return raw;
  const root = workingDirectory.replace(/\\/g, "/").replace(/\/+$/, "");
  const rootLower = root.toLowerCase();
  const rawLower = raw.toLowerCase();
  if (rootLower && (rawLower === rootLower || rawLower.startsWith(`${rootLower}/`))) {
    return raw.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return raw.replace(/\/+/g, "/").replace(/^\.\/+/, "");
};

const DEFAULT_APP_EXTENSIONS = new Set(["doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp"]);

const shouldOpenWithDefaultApp = (path: string): boolean => {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return DEFAULT_APP_EXTENSIONS.has(extension);
};

const resolveLocalOpenPath = (path: string, workingDirectory: string): string => {
  const normalized = String(path || "").trim();
  if (/^[a-zA-Z]:[\\/]/.test(normalized) || normalized.startsWith("\\\\") || !workingDirectory.trim()) {
    return normalized;
  }
  return `${workingDirectory.replace(/[\\/]+$/, "")}\\${normalized.replace(/^[\\/]+/, "")}`;
};

export const createWorkspaceSlice: StateCreator<AppStore, [], [], WorkspaceSlice> = (set, get) => {
  const layout = loadInitialLayout();
  return {
    ...layout,
    sideChatOpen: false,
    terminalSessions: [],
    terminalSnapshots: {},
    backgroundTasks: [],
    browserAnnotations: [],
    activeTerminalSessionId: null,
    editorOpenRequests: [],
    activeEditorPath: null,
    setLeftSidebarWidth: (w) => {
      const v = w <= 0 ? 0 : clamp(LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_MAX_WIDTH, w);
      writeLS(LS.layout.leftWidth, String(v));
      set({ leftSidebarWidth: v });
    },
    setRightSidebarWidth: (w) => {
      const v = clamp(320, RIGHT_SIDEBAR_MAX, w);
      writeLS(LS.layout.rightWidth, String(v));
      set({ rightSidebarWidth: v });
    },
    toggleRightPanel: () =>
      set((s) => {
        const rightPanelOpen = !s.rightPanelOpen;
        writeLS(LS.layout.rightOpen, rightPanelOpen ? "1" : "0");
        return { rightPanelOpen };
      }),
    setDockHeight: (h) => {
      const v = clamp(180, 520, h);
      writeLS(LS.layout.dockHeight, String(v));
      set({ dockHeight: v });
    },
    toggleDock: () =>
      set((s) => {
        const next = !s.dockCollapsed;
        writeLS(LS.layout.dockCollapsed, next ? "1" : "0");
        return { dockCollapsed: next };
      }),
    setActiveBottomTab: (t) => {
      writeLS(LS.layout.dockTab, t);
      set({ activeBottomTab: t });
    },
    addPanel: (slot) =>
      set((s) => {
        const canonicalSlot: PanelSlot =
          slot.kind === "subagent"
            ? { ...slot, kind: "subagents", label: slot.label ?? "协作" }
            : slot;
        const rightStackByKind: Partial<Record<PanelKind, UISlice["rightStackTab"]>> = {
          preview: "preview",
          terminal: "terminal",
          diff: "diff",
          plan: "plan",
          tasks: "tasks",
          subagents: "subagents",
          artifacts: "artifacts",
          inspector: "inspector",
        };
        const rightTab = rightStackByKind[canonicalSlot.kind];
        if (rightTab) {
          const rightSidebarWidth = preferredRightSidebarWidth(rightTab, s.rightSidebarWidth);
          if (rightSidebarWidth !== s.rightSidebarWidth) {
            writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
          }
          writeLS(LS.layout.rightOpen, "1");
          return {
            rightStackTab: rightTab,
            rightPanelOpen: true,
            rightSidebarWidth,
            dockCollapsed: true,
            panelSlots: normalizePanelSlots(s.panelSlots.filter((p) => p.kind === "chat")),
          };
        }
        if (s.panelSlots.some((p) => p.kind === canonicalSlot.kind && canonicalSlot.kind !== "chat")) {
          const next = normalizePanelSlots(s.panelSlots.map((p) => ({ ...p, focused: p.kind === canonicalSlot.kind })));
          persistPanelSlots(next);
          return { panelSlots: next };
        }
        let next: PanelSlot[] = [
          ...s.panelSlots.map((p) => ({ ...p, focused: false, maximized: false })),
          { ...canonicalSlot, size: canonicalSlot.size ?? 1, focused: true, maximized: false },
        ];
        if (next.length > 2) {
          const firstNonChat = next.findIndex((p) => p.kind !== "chat");
          if (firstNonChat >= 0) next.splice(firstNonChat, 1);
          else next = next.slice(-2);
        }
        next = normalizePanelSlots(next);
        persistPanelSlots(next);
        return { panelSlots: next };
      }),
    removePanel: (id) =>
      set((s) => {
        const next = s.panelSlots.filter((p) => p.id !== id);
        if (next.length === 0)
          next.push({ id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true });
        const normalized = normalizePanelSlots(next);
        persistPanelSlots(normalized);
        return { panelSlots: normalized };
      }),
    focusPanel: (id) =>
      set((s) => {
        const next = normalizePanelSlots(s.panelSlots.map((p) => ({ ...p, focused: p.id === id })));
        persistPanelSlots(next);
        return { panelSlots: next };
      }),
    movePanel: (id, direction) =>
      set((s) => {
        const index = s.panelSlots.findIndex((p) => p.id === id);
        const target = index + direction;
        if (index < 0 || target < 0 || target >= s.panelSlots.length) return s;
        const next = s.panelSlots.slice();
        const [slot] = next.splice(index, 1);
        next.splice(target, 0, slot);
        const normalized = normalizePanelSlots(next);
        persistPanelSlots(normalized);
        return { panelSlots: normalized };
      }),
    reorderPanels: (fromIndex, toIndex) =>
      set((s) => {
        if (
          fromIndex === toIndex ||
          fromIndex < 0 ||
          toIndex < 0 ||
          fromIndex >= s.panelSlots.length ||
          toIndex >= s.panelSlots.length
        ) {
          return s;
        }
        const next = s.panelSlots.slice();
        const [moved] = next.splice(fromIndex, 1);
        next.splice(toIndex, 0, moved);
        const normalized = normalizePanelSlots(next);
        persistPanelSlots(normalized);
        return { panelSlots: normalized };
      }),
    resizePanel: (id, delta) =>
      set((s) => {
        const index = s.panelSlots.findIndex((p) => p.id === id);
        if (index < 0 || index >= s.panelSlots.length - 1) return s;
        const next = s.panelSlots.slice();
        const current = next[index];
        const neighbor = next[index + 1];
        const step = delta / 360;
        const currentSize = Math.max(0.45, (current.size ?? 1) + step);
        const neighborSize = Math.max(0.45, (neighbor.size ?? 1) - step);
        next[index] = { ...current, size: currentSize };
        next[index + 1] = { ...neighbor, size: neighborSize };
        const normalized = normalizePanelSlots(next);
        persistPanelSlots(normalized);
        return { panelSlots: normalized };
      }),
    togglePanelMaximized: (id) =>
      set((s) => {
        const target = s.panelSlots.find((p) => p.id === id);
        const shouldMaximize = !target?.maximized;
        const next = normalizePanelSlots(s.panelSlots.map((p) => ({
          ...p,
          maximized: p.id === id ? shouldMaximize : false,
          focused: p.id === id,
        })));
        persistPanelSlots(next);
        return { panelSlots: next };
      }),
    resetPanelLayout: () => {
      const next = normalizePanelSlots(defaultPanelSlots());
      persistPanelSlots(next);
      set({
        panelSlots: next,
        leftSidebarWidth: 320,
        rightSidebarWidth: 440,
        rightPanelOpen: false,
        dockHeight: 240,
        dockCollapsed: true,
        activeBottomTab: "terminal",
      });
      writeLS(LS.layout.leftWidth, "320");
      writeLS(LS.layout.rightWidth, "440");
      writeLS(LS.layout.rightOpen, "0");
      writeLS(LS.layout.dockHeight, "240");
      writeLS(LS.layout.dockCollapsed, "1");
      writeLS(LS.layout.dockTab, "terminal");
    },
    setTerminalSessions: (sessions) =>
      set((s) => ({
        terminalSessions: sessions,
        activeTerminalSessionId:
          s.activeTerminalSessionId && sessions.some((session) => session.id === s.activeTerminalSessionId)
            ? s.activeTerminalSessionId
            : sessions[0]?.id ?? null,
      })),
    upsertTerminalSession: (session) =>
      set((s) => {
        const exists = s.terminalSessions.some((item) => item.id === session.id);
        const terminalSessions = exists
          ? s.terminalSessions.map((item) => (item.id === session.id ? { ...item, ...session } : item))
          : [...s.terminalSessions, session];
        return {
          terminalSessions,
          activeTerminalSessionId: s.activeTerminalSessionId ?? session.id,
        };
      }),
    upsertTerminalSnapshot: (snapshot) =>
      set((s) => ({
        terminalSnapshots: {
          ...s.terminalSnapshots,
          [snapshot.id]: snapshot,
        },
      })),
    removeTerminalSession: (id) =>
      set((s) => {
        const terminalSessions = s.terminalSessions.filter((session) => session.id !== id);
        const { [id]: _removed, ...terminalSnapshots } = s.terminalSnapshots;
        return {
          terminalSessions,
          terminalSnapshots,
          activeTerminalSessionId:
            s.activeTerminalSessionId === id
              ? terminalSessions[0]?.id ?? null
              : s.activeTerminalSessionId,
        };
      }),
    setActiveTerminalSession: (id) => set({ activeTerminalSessionId: id }),
    openEditorFile: (path, label, target) => {
      const current = get();
      if (isDesktop() && shouldOpenWithDefaultApp(path)) {
        void openPath(resolveLocalOpenPath(path, current.workingDirectory));
        return;
      }
      set((s) => {
        const normalizedPath = normalizeEditorOpenPath(path, s.workingDirectory);
        const editorLabel = label ?? normalizedPath.split(/[/\\]/).pop() ?? normalizedPath;
        const editorSlot = s.panelSlots.find((p) => p.kind === "editor");
        const baseSlots = s.panelSlots.filter((p) => p.kind === "chat" || p.kind === "editor");
        const line = Number.isFinite(target?.line) && Number(target?.line) > 0
          ? Math.floor(Number(target?.line))
          : undefined;
        const column = Number.isFinite(target?.column) && Number(target?.column) > 0
          ? Math.floor(Number(target?.column))
          : undefined;
        const nextSlots = editorSlot
          ? baseSlots.map((p) => p.kind === "editor" ? { ...p, label: editorLabel } : p)
          : [
              ...baseSlots,
              { id: `editor-${Date.now().toString(36)}`, kind: "editor" as const, label: editorLabel, size: 1 },
            ];
        const normalizedSlots = normalizePanelSlots(nextSlots.map((p) => ({ ...p, focused: p.kind === "editor", maximized: false })));
        persistPanelSlots(normalizedSlots);
        return {
          panelSlots: normalizedSlots,
          editorOpenRequests: [
            ...s.editorOpenRequests,
            {
              id: `open-${Date.now().toString(36)}-${s.editorOpenRequests.length}`,
              path: normalizedPath,
              ...(line ? { line } : {}),
              ...(column ? { column } : {}),
            },
          ],
          activeEditorPath: normalizedPath,
          appMode: "code",
        };
      });
    },
    consumeEditorOpenRequest: (path) =>
      set((s) => ({
        editorOpenRequests: s.editorOpenRequests.filter((request) =>
          request.id !== path && request.path !== path,
        ),
      })),
    toggleSideChat: () => set((s) => ({ sideChatOpen: !s.sideChatOpen })),
    addBackgroundTask: (task) =>
      set((s) => ({
        backgroundTasks: [task, ...s.backgroundTasks.filter((t) => t.id !== task.id)].slice(0, 30),
      })),
    addBrowserAnnotation: (annotation) =>
      set((s) => ({
        browserAnnotations: [annotation, ...s.browserAnnotations.filter((item) => item.id !== annotation.id)].slice(0, 80),
      })),
    removeBrowserAnnotation: (id) =>
      set((s) => ({
        browserAnnotations: s.browserAnnotations.filter((item) => item.id !== id),
      })),
    clearBrowserAnnotations: (target) =>
      set((s) => ({
        browserAnnotations: target
          ? s.browserAnnotations.filter((item) =>
              target.targetId && item.targetId === target.targetId
                ? false
                : target.url && item.url === target.url
                  ? false
                  : true,
            )
          : [],
      })),
    prStatus: null,
    ciChecks: [],
    setPrStatus: (pr, checks) => set({ prStatus: pr, ciChecks: checks }),
    scheduledTasks: [],
    setScheduledTasks: (tasks) => set({ scheduledTasks: tasks }),
    marketplaceConnectors: [],
    setMarketplaceConnectors: (connectors) => set({ marketplaceConnectors: connectors }),
  };
};
