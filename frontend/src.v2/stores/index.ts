import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type {
  AgentSlice,
  AppStore,
  ChatMessage,
  ChatSlice,
  ComposerSlice,
  EditorTab,
  FileContextRef,
  MessageContextRef,
  MessageAttachmentRef,
  PanelKind,
  PanelSlot,
  SkillContextRef,
  UISlice,
  WorkspaceSlice,
} from "./types";
import { clamp } from "../lib/clamp";
import { clampTextScale } from "../lib/text-scale";
import { canonicalWorkspacePath } from "../lib/workspace-display";
import { syncPermissionMode, toBackendPermissionMode } from "../protocol/permissions";
import { sendClientCommand } from "../protocol/ws-outbox";
import { getContentBlocks, getThinkingFromMessage, getToolCallsFromMessage, stripLegacyContentFields } from "../lib/content-blocks";

const LS = {
  theme: "minicode.theme",
  textScale: "minicode.textScale",
  layout: {
    leftWidth: "minicode.layout.left-width",
    rightWidth: "minicode.layout.right-width",
    dockHeight: "minicode.layout.dock-height",
    dockCollapsed: "minicode.layout.dock-collapsed",
    dockTab: "minicode.layout.dock-tab",
    panelSlots: "minicode.layout.panel-slots",
  },
  permissionMode: "minicode.composer.permissionMode",
  editorTabs: "minicode.editor.tabs",
};
const DEFAULT_WORKSPACE_KEY = "__default__";
const RIGHT_SIDEBAR_MAX = 1040;

const readLS = (key: string): string | null => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};
const writeLS = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* noop */
  }
};

const normalizePanelSlots = (slots: PanelSlot[]): PanelSlot[] => {
  const visibleSlots = slots.filter((slot) => slot.kind !== "inspector");
  if (visibleSlots.length === 0) return [{ id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true }];
  const hasFocused = visibleSlots.some((slot) => slot.focused);
  return visibleSlots.map((slot, index) => ({
    ...slot,
    size: Number.isFinite(slot.size) && slot.size! > 0 ? slot.size : 1,
    focused: hasFocused ? Boolean(slot.focused) : index === 0,
  }));
};

const defaultPanelSlots = (): PanelSlot[] => [
  { id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true, maximized: false },
];

const ensureCodePanelSlots = (slots: PanelSlot[]): PanelSlot[] => {
  const withoutMaximized = slots.map((slot) => ({ ...slot, maximized: false }));
  const hasChat = withoutMaximized.some((slot) => slot.kind === "chat");
  const chatSlot: PanelSlot = { id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true };
  const next: PanelSlot[] = [
    ...(hasChat ? [] : [chatSlot]),
    ...withoutMaximized.filter((slot) => slot.kind === "chat"),
  ];
  return normalizePanelSlots(next.map((slot) => ({
    ...slot,
    focused: slot.kind === "chat" ? true : slot.focused,
  })));
};

const isStructurallyEmptyAssistantMessage = (message: ChatMessage) =>
  message.role === "assistant"
  && message.isStreaming
  && !message.content
  && !getThinkingFromMessage(message)
  && getToolCallsFromMessage(message).length === 0
  && message.artifacts.length === 0;

const persistPanelSlots = (slots: PanelSlot[]) => {
  writeLS(LS.layout.panelSlots, JSON.stringify(normalizePanelSlots(slots)));
};

const loadInitialLayout = () => {
  const left = parseInt(readLS(LS.layout.leftWidth) ?? "", 10);
  const right = parseInt(readLS(LS.layout.rightWidth) ?? "", 10);
  const dock = parseInt(readLS(LS.layout.dockHeight) ?? "", 10);
  const collapsedRaw = readLS(LS.layout.dockCollapsed);
  const collapsed = collapsedRaw == null ? true : collapsedRaw === "1";
  const tab = (readLS(LS.layout.dockTab) ?? "terminal") as WorkspaceSlice["activeBottomTab"];
  let slots: PanelSlot[] = defaultPanelSlots();
  try {
    const raw = readLS(LS.layout.panelSlots);
    if (raw) {
      const parsed = (JSON.parse(raw) as (PanelSlot | (Omit<PanelSlot, "kind"> & { kind: "subagent" }))[])
        .map((slot) =>
          slot.kind === "subagent"
            ? { ...slot, kind: "subagents", label: slot.label ?? "Subagents" }
            : slot,
        ) as PanelSlot[];
      if (Array.isArray(parsed) && parsed.length > 0) slots = parsed;
    }
  } catch {
    /* keep default */
  }
  return {
    leftSidebarWidth: clamp(280, 360, Number.isFinite(left) ? left : 352),
    rightSidebarWidth: clamp(320, RIGHT_SIDEBAR_MAX, Number.isFinite(right) ? right : 382),
    dockHeight: clamp(180, 520, Number.isFinite(dock) ? dock : 240),
    dockCollapsed: collapsed,
    activeBottomTab: tab,
    panelSlots: normalizePanelSlots(slots),
  };
};

const initialTheme = (): UISlice["themeMode"] => {
  const v = readLS(LS.theme);
  if (v === "dark" || v === "light" || v === "system") return v;
  return "dark";
};

const initialTextScale = (): number => {
  const v = parseFloat(readLS(LS.textScale) ?? "");
  return clampTextScale(Number.isFinite(v) ? v : 1.0);
};

const editorWorkspaceKey = (workspace: string | null | undefined): string => {
  const value = (workspace || "").trim();
  return value || DEFAULT_WORKSPACE_KEY;
};

const editorTabsStorageKey = (workspace: string | null | undefined): string =>
  `${LS.editorTabs}:${editorWorkspaceKey(workspace)}`;

const blankEditorTab = (path: string): EditorTab => ({
  path,
  content: "",
  original: "",
  loading: true,
  error: null,
  externalChanged: false,
});

const loadPersistedEditorTabs = (workspace?: string | null): EditorTab[] => {
  try {
    const scopedRaw = readLS(editorTabsStorageKey(workspace));
    const raw = scopedRaw ?? (editorWorkspaceKey(workspace) === DEFAULT_WORKSPACE_KEY ? readLS(LS.editorTabs) : null);
    if (!raw) return [];
    const paths = JSON.parse(raw) as string[];
    if (!Array.isArray(paths)) return [];
    return paths.slice(0, 20).map(blankEditorTab);
  } catch {
    return [];
  }
};

const persistEditorTabs = (tabs: EditorTab[], workspace?: string | null) => {
  writeLS(editorTabsStorageKey(workspace), JSON.stringify(tabs.map((t) => t.path)));
};

const editorStateForWorkspace = (workspace: string | null | undefined) => {
  const editorTabs = loadPersistedEditorTabs(workspace);
  return {
    editorTabs,
    activeTabPath: editorTabs[0]?.path ?? null,
    activeEditorPath: null,
    editorOpenRequests: [],
  };
};

const applyTheme = (mode: UISlice["themeMode"]) => {
  let resolved: "dark" | "light" = "dark";
  if (mode === "light") resolved = "light";
  else if (mode === "dark") resolved = "dark";
  else
    resolved = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", resolved);
};

const applyTextScale = (s: number) => {
  document.documentElement.style.setProperty("--app-text-scale", String(s));
};

const preferredRightSidebarWidth = (
  tab: UISlice["rightStackTab"],
  currentWidth: number,
): number => {
  const viewportWidth = typeof window === "undefined" ? 1440 : window.innerWidth;
  if (viewportWidth < 1180) return currentWidth;
  const preferred =
    tab === "preview" ? 382 :
      tab === "browser" ? 680 :
        tab === "terminal" ? 720 :
          tab === "subagents" ? 360 :
            0;
  if (!preferred || currentWidth >= preferred) return currentWidth;
  return clamp(320, RIGHT_SIDEBAR_MAX, preferred);
};

let _msgIdCounter = 0;
const uniqueMessageId = (suffix = ""): string => {
  const base = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
    : `${Date.now().toString(36)}${(++_msgIdCounter).toString(36)}`;
  return suffix ? `m-${base}-${suffix}` : `m-${base}`;
};

const newConversationId = (): string =>
  `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

const cacheMessagesForConversation = (
  state: AppStore,
  id: string | null,
  messages: AppStore["messages"] = state.messages,
  isStreaming: boolean = state.isStreaming,
) => {
  if (!id) {
    return {
      conversationMessages: state.conversationMessages,
      conversationStreaming: state.conversationStreaming,
    };
  }
  return {
    conversationMessages: {
      ...state.conversationMessages,
      [id]: messages,
    },
    conversationStreaming: {
      ...state.conversationStreaming,
      [id]: isStreaming,
    },
  };
};

const updateMessagesForConversation = (
  state: AppStore,
  id: string | undefined,
  updater: (messages: AppStore["messages"]) => AppStore["messages"] | null,
  streaming?: boolean,
) => {
  const targetId = id || state.conversationId || undefined;
  if (!targetId) {
    const nextMessages = updater(state.messages);
    if (!nextMessages) return state;
    return {
      messages: nextMessages,
      isStreaming: streaming ?? state.isStreaming,
    };
  }

  if (state.sideChats[targetId]) {
    const thread = state.sideChats[targetId];
    const nextMessages = updater(thread.messages);
    if (!nextMessages) return state;
    return {
      sideChats: {
        ...state.sideChats,
        [targetId]: {
          ...thread,
          messages: nextMessages,
          isStreaming: streaming ?? thread.isStreaming,
        },
      },
    };
  }

  const isActive = targetId === state.conversationId;
  const sourceMessages = isActive
    ? state.messages
    : state.conversationMessages[targetId] ?? [];
  const nextMessages = updater(sourceMessages);
  if (!nextMessages) return state;

  return {
    ...(isActive ? { messages: nextMessages, isStreaming: streaming ?? state.isStreaming } : {}),
    conversationMessages: {
      ...state.conversationMessages,
      [targetId]: nextMessages,
    },
    conversationStreaming: {
      ...state.conversationStreaming,
      [targetId]: streaming ?? (isActive ? state.isStreaming : state.conversationStreaming[targetId] ?? false),
    },
  };
};

const findLastStreamingIndex = (messages: AppStore["messages"]): number => {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.isStreaming) return i;
  }
  return -1;
};

const progressConversationKey = (conversationId?: string): string => conversationId || "__active__";
const conversationWorkspacePath = (conversation?: AppStore["conversations"][number] | null): string =>
  conversation?.worktreePath || conversation?.workspaceRoot || "";

export const useAppStore = create<AppStore>()(
  subscribeWithSelector((set, get) => {
    const layout = loadInitialLayout();
    const initialEditorTabs = loadPersistedEditorTabs("");
    return {
      // ── UI ──
      themeMode: initialTheme(),
      textScale: initialTextScale(),
      viewMode: "normal" as const,
      appMode: "code" as const,
      rightStackTab: "preview" as const,
      rightStackTabLocked: false,
      contextUsage: null,
      commandPaletteOpen: false,
      settingsOpen: false,
      shortcutsHelpOpen: false,
      quickOpenVisible: false,
      quickOpenResults: [],
      quickOpenLoading: false,
      currentModel: "",
      currentProvider: "",
      availableModels: [],
      availableSkills: [],
      marketplaceSkills: [],
      slashCommands: [],
      workingDirectory: "",
      workspaceGit: null,
      diffReview: null,
      previewArtifact: null,
      livePreviewUrl: null,
      previewServers: [],
      previewLaunchConfigs: [],
      previewLaunchProcesses: [],
      previewVerification: null,
      fileChanges: [],
      fileTreeVersion: 0,
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
      setThemeMode: (mode) => {
        writeLS(LS.theme, mode);
        applyTheme(mode);
        set({ themeMode: mode });
      },
      setTextScale: (s) => {
        const v = clampTextScale(s);
        writeLS(LS.textScale, String(v));
        applyTextScale(v);
        set({ textScale: v });
      },
      setViewMode: (m) => set({ viewMode: m }),
      setAppMode: (m) =>
        set((s) => {
          if (m !== "code") return { appMode: m };
          const panelSlots = ensureCodePanelSlots(s.panelSlots);
          persistPanelSlots(panelSlots);
          return { appMode: m, panelSlots };
        }),
      ensureCodeLayout: () =>
        set((s) => {
          const panelSlots = ensureCodePanelSlots(s.panelSlots);
          persistPanelSlots(panelSlots);
          return { panelSlots };
        }),
      setRightStackTab: (t, options) =>
        set((s) => {
          if (t === "terminal") writeLS(LS.layout.dockCollapsed, "1");
          const rightSidebarWidth = preferredRightSidebarWidth(t, s.rightSidebarWidth);
          if (rightSidebarWidth !== s.rightSidebarWidth) {
            writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
          }
          return {
            rightStackTab: t,
            rightStackTabLocked: options?.automatic ? s.rightStackTabLocked : true,
            rightPanelOpen: true,
            rightSidebarWidth,
            dockCollapsed: t === "terminal" ? true : s.dockCollapsed,
          };
        }),
      setRightStackTabLocked: (locked) => set({ rightStackTabLocked: locked }),
      setContextUsage: (u) => set({ contextUsage: u }),
      toggleCommandPalette: () =>
        set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),
      toggleSettings: () => set((s) => ({ settingsOpen: !s.settingsOpen })),
      toggleShortcutsHelp: () =>
        set((s) => ({ shortcutsHelpOpen: !s.shortcutsHelpOpen })),
      skillsMarketplaceOpen: false,
      toggleSkillsMarketplace: () =>
        set((s) => ({ skillsMarketplaceOpen: !s.skillsMarketplaceOpen })),
      setCurrentModel: (m) => set({ currentModel: m }),
      setCurrentProvider: (p) => set({ currentProvider: p }),
      setAvailableModels: (models) => set({ availableModels: models }),
      setAvailableSkills: (skills) => set({ availableSkills: skills }),
      setSlashCommands: (cmds) => set({ slashCommands: cmds }),
      setMarketplaceSkills: (skills) => set({ marketplaceSkills: skills }),
      setWorkingDirectory: (d) => {
        set((s) => ({
          ...(d !== s.workingDirectory ? editorStateForWorkspace(d) : {}),
          workingDirectory: d,
          workspaceGit: d !== s.workingDirectory ? null : s.workspaceGit,
        }));
        if (d) {
          const rt = typeof window !== "undefined"
            ? (window as any).__MINICODE_RUNTIME__?.desktop
            : undefined;
          if (rt?.trustWorkspace) rt.trustWorkspace(d);
        }
      },
      setWorkspaceGit: (state) => set({ workspaceGit: state }),
      setDiffReviewState: (state) => set({ diffReview: state }),
      updateDiffReviewFile: (path, patch) =>
        set((s) => ({
          diffReview: s.diffReview
            ? {
                ...s.diffReview,
                files: s.diffReview.files.map((file) =>
                  file.path === path ? { ...file, ...patch } : file,
                ),
                diff: s.diffReview.selectedPath === path && patch.patch != null ? patch.patch : s.diffReview.diff,
              }
            : null,
        })),
      setDiffReviewSelectedPath: (path) =>
        set((s) => {
          if (!s.diffReview) return s;
          const file = s.diffReview.files.find((item) => item.path === path);
          return {
            diffReview: {
              ...s.diffReview,
              selectedPath: path,
              diff: file?.patch ?? s.diffReview.diff,
            },
          };
        }),
      setDiffFileDecision: (path, decision) =>
        set((s) => {
          if (!s.diffReview) return s;
          return {
            diffReview: {
              ...s.diffReview,
              fileDecisions: { ...s.diffReview.fileDecisions, [path]: decision },
              files: s.diffReview.files.map((f) =>
                f.path === path ? { ...f, decision } : f,
              ),
            },
          };
        }),
      addDiffLineComment: (comment) =>
        set((s) => {
          if (!s.diffReview) return s;
          return {
            diffReview: {
              ...s.diffReview,
              lineComments: [...(s.diffReview.lineComments ?? []), comment],
            },
          };
        }),
      removeDiffLineComment: (filePath, lineIndex) =>
        set((s) => {
          if (!s.diffReview) return s;
          return {
            diffReview: {
              ...s.diffReview,
              lineComments: (s.diffReview.lineComments ?? []).filter(
                (c) => !(c.filePath === filePath && c.lineIndex === lineIndex),
              ),
            },
          };
        }),
      submitDiffReviewWithComments: () => {
        const s = get();
        if (!s.diffReview) return;
        const { requestId, lineComments } = s.diffReview;
        const comments = lineComments ?? [];
        if (comments.length > 0) {
          sendClientCommand({
            type: "user_message",
            content: `Review the following diff comments and revise accordingly:\n${comments.map((c) => `- ${c.filePath}:${c.lineIndex + 1}: ${c.content}`).join("\n")}`,
          });
        }
        const sent = sendClientCommand({
          type: "approval",
          tool_call_id: requestId,
          action: "approve",
        });
        set((state) => ({
          diffReview: state.diffReview?.requestId === requestId
            ? { ...state.diffReview, status: sent ? "submitted" : "error", error: sent ? undefined : "Connection is offline" }
            : state.diffReview,
        }));
      },
      submitPartialApproval: () => {
        const s = get();
        if (!s.diffReview) return;
        const { requestId, fileDecisions } = s.diffReview;
        const sent = sendClientCommand({
          type: "approval",
          tool_call_id: requestId,
          action: "partial",
          decisions: fileDecisions,
        });
        set((state) => ({
          diffReview: state.diffReview?.requestId === requestId
            ? { ...state.diffReview, status: sent ? "submitted" : "error", error: sent ? undefined : "Connection is offline" }
            : state.diffReview,
        }));
      },
      setPreviewArtifact: (artifact) => set({ previewArtifact: artifact }),
      setLivePreviewUrl: (url) => set({ livePreviewUrl: url }),
      openLivePreview: (url) =>
        set((s) => {
          const normalizedUrl = /^https?:\/\//i.test(url.trim()) ? url.trim() : `http://${url.trim()}`;
          const rightSidebarWidth = preferredRightSidebarWidth("preview", s.rightSidebarWidth);
          if (rightSidebarWidth !== s.rightSidebarWidth) {
            writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
          }
          return { livePreviewUrl: normalizedUrl, rightStackTab: "preview", rightPanelOpen: true, rightSidebarWidth };
        }),
      setPreviewServers: (servers) => set({ previewServers: servers }),
      addPreviewServer: (server) =>
        set((s) => ({
          previewServers: [
            ...s.previewServers.filter((existing) => existing.port !== server.port),
            server,
          ],
        })),
      removePreviewServer: (port) =>
        set((s) => ({
          previewServers: s.previewServers.filter((existing) => existing.port !== port),
        })),
      setPreviewLaunchConfigs: (configs) => set({ previewLaunchConfigs: configs }),
      setPreviewLaunchProcesses: (processes) => set({ previewLaunchProcesses: processes }),
      upsertPreviewLaunchProcess: (process) =>
        set((s) => ({
          previewLaunchProcesses: [
            process,
            ...s.previewLaunchProcesses.filter((existing) => existing.id !== process.id),
          ],
        })),
      removePreviewLaunchProcess: (id) =>
        set((s) => ({
          previewLaunchProcesses: s.previewLaunchProcesses.filter((process) => process.id !== id),
        })),
      setPreviewVerification: (verification) => set({ previewVerification: verification }),
      setQuickOpenResults: (results) => set({ quickOpenResults: results, quickOpenLoading: false }),
      setQuickOpenLoading: (loading) => set({ quickOpenLoading: loading }),
      addFileChange: (change) =>
        set((s) => ({ fileChanges: [...s.fileChanges.slice(-99), change] })),
      bumpFileTreeVersion: () =>
        set((s) => ({ fileTreeVersion: s.fileTreeVersion + 1 })),
      setGitChanges: (changes) =>
        set((s) => ({ gitChanges: { ...s.gitChanges, ...changes } })),
      setGitChangesLoading: (loading) =>
        set((s) => ({ gitChanges: { ...s.gitChanges, loading } })),
      requestGitChanges: () => {
        set((s) => ({ gitChanges: { ...s.gitChanges, loading: true } }));
        sendClientCommand({ type: "diff.git_working_tree" });
        sendClientCommand({ type: "diff.git_staged" });
      },
      mcpServers: [],
      setMcpServers: (servers) => set({ mcpServers: servers }),
      envVars: [],
      setEnvVars: (entries) => set({ envVars: entries }),

      // ── Workspace ──
      ...layout,
      rightPanelOpen: false,
      sideChatOpen: false,
      terminalSessions: [],
      backgroundTasks: [],
      activeTerminalSessionId: null,
      editorOpenRequests: [],
      activeEditorPath: null,
      setLeftSidebarWidth: (w) => {
        const v = clamp(280, 360, w);
        writeLS(LS.layout.leftWidth, String(v));
        set({ leftSidebarWidth: v });
      },
      setRightSidebarWidth: (w) => {
        const v = clamp(320, RIGHT_SIDEBAR_MAX, w);
        writeLS(LS.layout.rightWidth, String(v));
        set({ rightSidebarWidth: v });
      },
      toggleRightPanel: () => set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
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
              ? { ...slot, kind: "subagents", label: slot.label ?? "Subagents" }
              : slot;
          const rightStackByKind: Partial<Record<PanelKind, UISlice["rightStackTab"]>> = {
            preview: "preview",
            terminal: "terminal",
            diff: "inspector",
            plan: "plan",
            tasks: "tasks",
            subagents: "subagents",
            inspector: "inspector",
          };
          const rightTab = rightStackByKind[canonicalSlot.kind];
          if (rightTab) {
            const rightSidebarWidth = preferredRightSidebarWidth(rightTab, s.rightSidebarWidth);
            if (rightSidebarWidth !== s.rightSidebarWidth) {
              writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
            }
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
          leftSidebarWidth: 352,
          rightSidebarWidth: 382,
          dockHeight: 240,
          dockCollapsed: true,
          activeBottomTab: "terminal",
        });
        writeLS(LS.layout.leftWidth, "352");
        writeLS(LS.layout.rightWidth, "382");
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
      removeTerminalSession: (id) =>
        set((s) => {
          const terminalSessions = s.terminalSessions.filter((session) => session.id !== id);
          return {
            terminalSessions,
            activeTerminalSessionId:
              s.activeTerminalSessionId === id
                ? terminalSessions[0]?.id ?? null
                : s.activeTerminalSessionId,
          };
        }),
      setActiveTerminalSession: (id) => set({ activeTerminalSessionId: id }),
      openEditorFile: (path, label) =>
        set((s) => {
          const editorLabel = label ?? path.split(/[/\\]/).pop() ?? path;
          const editorSlot = s.panelSlots.find((p) => p.kind === "editor");
          const baseSlots = s.panelSlots.filter((p) => p.kind === "chat" || p.kind === "editor");
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
            editorOpenRequests: [...s.editorOpenRequests, path],
            activeEditorPath: path,
            appMode: "code",
          };
        }),
      consumeEditorOpenRequest: (path) =>
        set((s) => ({
          editorOpenRequests: s.editorOpenRequests.filter((p) => p !== path),
        })),
      toggleSideChat: () => set((s) => ({ sideChatOpen: !s.sideChatOpen })),
      addBackgroundTask: (task) =>
        set((s) => ({
          backgroundTasks: [task, ...s.backgroundTasks.filter((t) => t.id !== task.id)].slice(0, 30),
        })),
      prStatus: null,
      ciChecks: [],
      setPrStatus: (pr, checks) => set({ prStatus: pr, ciChecks: checks }),
      scheduledTasks: [],
      setScheduledTasks: (tasks) => set({ scheduledTasks: tasks }),
      marketplaceConnectors: [],
      setMarketplaceConnectors: (connectors) => set({ marketplaceConnectors: connectors }),

      // ── Chat ──
      conversationId: null,
      conversations: [],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: false,
      lastUsage: null,
      sideChats: {},
      sendMessage: (content: string, options?: { assistant?: boolean; contextRefs?: MessageContextRef[]; attachmentRefs?: MessageAttachmentRef[] }) => {
        const id = uniqueMessageId();
        const includeAssistant = options?.assistant !== false;
        set((s) => {
          const nextMessages = [
            ...s.messages,
            {
              id,
              role: "user" as const,
              content,
              contextRefs: options?.contextRefs ?? [],
              attachmentRefs: options?.attachmentRefs ?? [],
              artifacts: [],
              timestamp: Date.now(),
            },
            ...(includeAssistant
              ? [{
                  id: uniqueMessageId("a"),
                  role: "assistant" as const,
                  content: "",
                  blocks: [],
                  artifacts: [],
                  timestamp: Date.now(),
                  isStreaming: true,
                }]
              : []),
          ];
          const nextStreaming = includeAssistant || s.isStreaming;
          return {
            messages: nextMessages,
            isStreaming: nextStreaming,
            ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
          };
        });
      },
      deleteMessage: (id) =>
        set((s) => {
          const nextMessages = s.messages.filter((message) => message.id !== id);
          const nextStreaming = s.messages.some((message) => message.id === id && message.isStreaming) ? false : s.isStreaming;
          return {
            messages: nextMessages,
            isStreaming: nextStreaming,
            ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
          };
        }),
      upsertSystemMessage: (id, content, options) =>
        set((s) => {
          const targetId = options?.conversationId || s.conversationId || undefined;
          const isActive = !targetId || targetId === s.conversationId;
          const sourceMessages = targetId && !isActive
            ? s.conversationMessages[targetId] ?? []
            : s.messages;
          const existingIdx = sourceMessages.findIndex((message) =>
            message.id === id ||
            (options?.replacePrefix && message.role === "system" && message.content.startsWith(options.replacePrefix))
          );
          const nextMessages = sourceMessages.slice();
          const nextMessage = {
            id,
            role: "system" as const,
            content,
            artifacts: [],
            timestamp: Date.now(),
          };
          if (existingIdx >= 0) {
            nextMessages[existingIdx] = { ...nextMessages[existingIdx], ...nextMessage };
          } else {
            nextMessages.push(nextMessage);
          }
          if (targetId && !isActive) {
            return {
              conversationMessages: {
                ...s.conversationMessages,
                [targetId]: nextMessages,
              },
            };
          }
          return {
            messages: nextMessages,
            ...cacheMessagesForConversation(s, s.conversationId, nextMessages, s.isStreaming),
          };
        }),
      recallMessage: (id) =>
        set((s) => {
          const index = s.messages.findIndex((message) => message.id === id);
          if (index < 0) return s;
          const target = s.messages[index];
          const turnStart =
            target.role === "user"
              ? index
              : (() => {
                  for (let i = index; i >= 0; i -= 1) {
                    if (s.messages[i]?.role === "user") return i;
                  }
                  return index;
                })();
          const recalled = s.messages[turnStart] ?? target;
          const nextMessages = s.messages.slice(0, turnStart);
          const contextRefs = recalled.contextRefs ?? target.contextRefs ?? [];
          const fileRefs: FileContextRef[] = contextRefs.flatMap((ref) =>
            ref.kind === "skill"
              ? []
              : [{ path: ref.path, name: ref.name, kind: ref.kind }],
          );
          const skillRefs: SkillContextRef[] = contextRefs.filter((ref): ref is SkillContextRef => ref.kind === "skill");
          const attachmentRefs = recalled.attachmentRefs ?? target.attachmentRefs ?? [];
          const restoredAttachments = attachmentRefs.map((attachment) => ({
            id: `att-recall-${attachment.artifactId || attachment.id}-${Date.now().toString(36)}`,
            name: attachment.name,
            type: attachment.mediaType || "application/octet-stream",
            size: attachment.sizeBytes || 0,
            status: "ready" as const,
            artifactId: attachment.artifactId,
            docId: attachment.docId,
            indexedChunks: attachment.indexedChunks,
            attachment: {
              id: attachment.id,
              kind: attachment.kind,
              file_name: attachment.name,
              media_type: attachment.mediaType,
              artifact_id: attachment.artifactId,
              doc_id: attachment.docId,
              indexed_chunks: attachment.indexedChunks ?? 0,
              size_bytes: attachment.sizeBytes ?? 0,
            },
          }));
          const removedMessages = s.messages.slice(turnStart);
          const nextStreaming = removedMessages.some((message) => message.isStreaming) ? false : s.isStreaming;
          return {
            messages: nextMessages,
            draft: recalled.role === "user" ? recalled.content : s.draft,
            selectedMentions: fileRefs,
            selectedSkills: skillRefs,
            attachments: restoredAttachments,
            actionChip: null,
            mentionResults: [],
            slashPanelOpen: false,
            mentionPanelOpen: false,
            isStreaming: nextStreaming,
            ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
          };
        }),
      removeEmptyStreamingAssistant: (conversationId) =>
        set((s) => {
          if (conversationId && s.sideChats[conversationId]) {
            const thread = s.sideChats[conversationId];
            return {
              sideChats: {
                ...s.sideChats,
                [conversationId]: {
                  ...thread,
                  messages: thread.messages.filter((m) => !isStructurallyEmptyAssistantMessage(m)),
                },
              },
            };
          }
          const targetId = conversationId || s.conversationId || undefined;
          const isActive = !targetId || targetId === s.conversationId;
          const sourceMessages = targetId && !isActive
            ? s.conversationMessages[targetId] ?? []
            : s.messages;
          const nextMessages = sourceMessages.filter((m) => !isStructurallyEmptyAssistantMessage(m));
          if (targetId && !isActive) {
            return {
              conversationMessages: {
                ...s.conversationMessages,
                [targetId]: nextMessages,
              },
            };
          }
          return {
            messages: nextMessages,
            ...cacheMessagesForConversation(s, s.conversationId, nextMessages, s.isStreaming),
          };
        }),
      interrupt: () => {
        get().finishStreaming(undefined, undefined, "failed");
      },
      switchConversation: (id) => {
        const targetWorkspace = conversationWorkspacePath(get().conversations.find((c) => c.id === id));
        set((s) => {
          const cachedCurrent = cacheMessagesForConversation(s, s.conversationId);
          return {
            conversationId: id,
            messages: cachedCurrent.conversationMessages[id] ?? [],
            isStreaming: cachedCurrent.conversationStreaming[id] ?? false,
            conversationMessages: cachedCurrent.conversationMessages,
            conversationStreaming: cachedCurrent.conversationStreaming,
            ...(targetWorkspace
              ? {
                  workingDirectory: targetWorkspace,
                  workspaceGit: targetWorkspace !== s.workingDirectory ? null : s.workspaceGit,
                  ...(targetWorkspace !== s.workingDirectory ? editorStateForWorkspace(targetWorkspace) : {}),
                }
              : {}),
          };
        });
        if (targetWorkspace) {
          const rt = typeof window !== "undefined"
            ? (window as any).__MINICODE_RUNTIME__?.desktop
            : undefined;
          if (rt?.trustWorkspace) rt.trustWorkspace(targetWorkspace);
        }
      },
      createConversation: () => {
        const state = get();
        const id = newConversationId();
        const workspaceRoot = canonicalWorkspacePath(state.workingDirectory);
        sendClientCommand({
          type: "conversation.create",
          conversation_id: id,
          title: "New chat",
          workspace_root: workspaceRoot || undefined,
          permission_mode: toBackendPermissionMode(state.permissionMode),
        });
        set((s) => {
          const cachedCurrent = cacheMessagesForConversation(s, s.conversationId);
          return {
            conversationId: id,
            conversations: [
              {
                id,
                title: "New chat",
                updatedAt: new Date().toISOString(),
                workspaceRoot: workspaceRoot || undefined,
                gitBranch: state.workspaceGit?.branch || undefined,
                worktreePath: state.workspaceGit?.currentPath || undefined,
                gitIsolated: state.workspaceGit?.isWorktree,
              },
              ...s.conversations,
            ],
            messages: [],
            isStreaming: false,
            conversationMessages: {
              ...cachedCurrent.conversationMessages,
              [id]: [],
            },
            conversationStreaming: {
              ...cachedCurrent.conversationStreaming,
              [id]: false,
            },
          };
        });
      },
      removeConversation: (id) => {
        const state = get();
        const remaining = state.conversations.filter((conversation) => conversation.id !== id);
        const conversationMessages = Object.fromEntries(
          Object.entries(state.conversationMessages).filter(([key]) => key !== id),
        );
        const conversationStreaming = Object.fromEntries(
          Object.entries(state.conversationStreaming).filter(([key]) => key !== id),
        );

        if (state.conversationId !== id) {
          set({ conversations: remaining, conversationMessages, conversationStreaming });
          return;
        }

        const nextActive = remaining.find((conversation) => !conversation.archived) ?? remaining[0];
        if (nextActive) {
          const nextWorkspace = conversationWorkspacePath(nextActive);
          set((s) => ({
            conversations: remaining,
            conversationMessages,
            conversationStreaming,
            conversationId: nextActive.id,
            messages: conversationMessages[nextActive.id] ?? [],
            isStreaming: conversationStreaming[nextActive.id] ?? false,
            ...(nextWorkspace
              ? {
                  workingDirectory: nextWorkspace,
                  workspaceGit: nextWorkspace !== s.workingDirectory ? null : s.workspaceGit,
                  ...(nextWorkspace !== s.workingDirectory ? editorStateForWorkspace(nextWorkspace) : {}),
                }
              : {}),
          }));
          return;
        }

        set({
          conversations: [],
          conversationMessages,
          conversationStreaming,
          conversationId: null,
          messages: [],
          isStreaming: false,
        });
        get().createConversation();
      },
      hydrateConversationMessages: (id, messages, options) =>
        set((s) => {
          const activate = options?.activate ?? id === s.conversationId;
          const nextStreaming = options?.isStreaming ?? messages.some((message) => message.isStreaming);
          const cachedCurrent = activate
            ? cacheMessagesForConversation(s, s.conversationId)
            : {
                conversationMessages: s.conversationMessages,
                conversationStreaming: s.conversationStreaming,
              };
          return {
            ...(activate ? { conversationId: id, messages, isStreaming: nextStreaming } : {}),
            conversationMessages: {
              ...cachedCurrent.conversationMessages,
              [id]: messages,
            },
            conversationStreaming: {
              ...cachedCurrent.conversationStreaming,
              [id]: nextStreaming,
            },
          };
        }),
      appendTextChunk: (content, conversationId) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = findLastStreamingIndex(messages);
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const blocks = msg.blocks ? msg.blocks.slice() : [];
            const last = blocks[blocks.length - 1];
            if (last && last.type === "text") {
              blocks[blocks.length - 1] = { ...last, content: last.content + content };
            } else {
              blocks.push({ type: "text", content });
            }
            next[idx] = { ...msg, content: msg.content + content, isThinkingStreaming: false, blocks };
            return next;
          });
        }),
      appendThinkingChunk: (content, conversationId) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = findLastStreamingIndex(messages);
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const blocks = msg.blocks ? msg.blocks.slice() : [];
            const last = blocks[blocks.length - 1];
            if (last && last.type === "thinking") {
              blocks[blocks.length - 1] = { ...last, content: last.content + content };
            } else {
              blocks.push({ type: "thinking", content });
            }
            next[idx] = { ...msg, isThinkingStreaming: true, blocks };
            return next;
          });
        }),
      appendProgress: (progress, conversationId) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = findLastStreamingIndex(messages);
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const blocks = msg.blocks ? msg.blocks.slice() : [];
            const progressBlock = {
              ...progress,
              type: "progress" as const,
              timestamp: Date.now(),
            };
            const existingIdx = blocks.findIndex((block) =>
              block.type === "progress" && block.id === progress.id,
            );
            if (existingIdx >= 0) {
              blocks[existingIdx] = progressBlock;
            } else {
              blocks.push(progressBlock);
            }
            next[idx] = { ...msg, blocks };
            return next;
          });
        }),
      appendToolCallBlock: (tc, conversationId) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = findLastStreamingIndex(messages);
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const blocks = msg.blocks ? msg.blocks.slice() : [];
            blocks.push({ type: "tool_call", record: tc });
            const baseMsg = stripLegacyContentFields(msg);
            next[idx] = { ...baseMsg, blocks };
            return next;
          });
        }),
      updateToolCall: (id, patch, conversationId) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = messages.findIndex((m) =>
              getToolCallsFromMessage(m).some((tc) => tc.id === id),
            );
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const baseMsg = stripLegacyContentFields(msg);
            next[idx] = {
              ...baseMsg,
              blocks: getContentBlocks(msg).map((block) =>
                block.type === "tool_call" && block.record.id === id
                  ? { ...block, record: { ...block.record, ...patch } }
                  : block,
              ),
            };
            return next;
          });
        }),
      finishStreaming: (conversationId, usage, terminalStatus = "completed") =>
        set((s) => {
          const finishedAt = Date.now();
          return updateMessagesForConversation(
            s,
            conversationId,
            (messages) => messages.map((m) => {
              if (!m.isStreaming) return m;
              const baseMessage = stripLegacyContentFields(m);
              return {
                ...baseMessage,
                isStreaming: false,
                isThinkingStreaming: false,
                resumeState: undefined,
                usage,
                blocks: getContentBlocks(m).map((block) => {
                  if (block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending")) {
                    return {
                      ...block,
                      record: {
                        ...block.record,
                        status: terminalStatus === "failed" ? "failed" as const : "success" as const,
                        finishedAt,
                      },
                    };
                  }
                  if (block.type === "progress" && block.status === "running") {
                    return {
                      ...block,
                      status: terminalStatus === "failed" ? "failed" as const : "completed" as const,
                      timestamp: finishedAt,
                    };
                  }
                  return block;
                }),
              };
            }),
            false,
          );
        }),
      resumeStreaming: (conversationId, toolCallsPending) =>
        set((s) => {
          const targetId = conversationId || s.conversationId;
          const sourceMessages = (targetId && s.conversationMessages[targetId]) || s.messages;
          const lastIdx = sourceMessages.length - 1;
          const lastMsg = lastIdx >= 0 ? sourceMessages[lastIdx] : null;

          let nextMessages: typeof sourceMessages;
          if (lastMsg && lastMsg.role === "assistant") {
            const newTcs = toolCallsPending?.map((tc) => ({ id: tc.id, name: tc.name, args: tc.args, status: "running" as const, startedAt: Date.now() })) || [];
            nextMessages = sourceMessages.slice();
            const baseLastMsg = stripLegacyContentFields(lastMsg);
            nextMessages[lastIdx] = {
              ...baseLastMsg,
              isStreaming: true,
              resumeState: "resumed",
              blocks: newTcs.length
                ? [...getContentBlocks(lastMsg), ...newTcs.map((tc) => ({ type: "tool_call" as const, record: tc }))]
                : getContentBlocks(lastMsg),
            };
          } else {
            const pendingTcs = toolCallsPending?.map((tc) => ({ id: tc.id, name: tc.name, args: tc.args, status: "running" as const, startedAt: Date.now() })) || [];
            nextMessages = [
              ...sourceMessages,
              {
                id: uniqueMessageId("resume"),
                role: "assistant" as const,
                content: "",
                blocks: pendingTcs.map((tc) => ({ type: "tool_call" as const, record: tc })),
                artifacts: [],
                timestamp: Date.now(),
                isStreaming: true,
                resumeState: "resumed",
              },
            ];
          }

          if (targetId && targetId !== s.conversationId) {
            return {
              conversationMessages: { ...s.conversationMessages, [targetId]: nextMessages },
              conversationStreaming: { ...s.conversationStreaming, [targetId]: true },
            };
          }
          return {
            messages: nextMessages,
            isStreaming: true,
            ...cacheMessagesForConversation(s, targetId, nextMessages, true),
          };
        }),
      replaceStreamingText: (conversationId, fullText) =>
        set((s) => {
          return updateMessagesForConversation(s, conversationId, (messages) => {
            const idx = findLastStreamingIndex(messages);
            if (idx < 0) return null;
            const next = messages.slice();
            const msg = next[idx];
            const blocks = msg.blocks ? msg.blocks.slice() : [];
            const lastBlock = blocks[blocks.length - 1];
            if (lastBlock && lastBlock.type === "text") {
              blocks[blocks.length - 1] = { ...lastBlock, content: fullText };
            } else {
              blocks.push({ type: "text", content: fullText });
            }
            next[idx] = { ...msg, content: fullText, blocks };
            return next;
          });
        }),
      setConnected: (c) => set({ isConnected: c }),
      setLastUsage: (u) => set({ lastUsage: u }),
      ensureSideChat: (id) =>
        set((s) => {
          if (s.sideChats[id]) return s;
          const recentMessages = s.messages
            .filter((message) => message.role === "user" || message.role === "assistant")
            .slice(-6)
            .map((message) => {
              const role = message.role === "assistant" ? "Assistant" : "User";
              const content = (message.content || getThinkingFromMessage(message) || "").trim().replace(/\s+/g, " ");
              return content ? `${role}: ${content.slice(0, 280)}` : "";
            })
            .filter(Boolean);
          const inheritedContext = recentMessages.length > 0
            ? `Main conversation context:\n${recentMessages.join("\n")}`
            : "";
          return {
            sideChats: {
              ...s.sideChats,
              [id]: { id, messages: [], isStreaming: false, draft: "", inheritedContext },
            },
          };
        }),
      removeSideChat: (id) =>
        set((s) => {
          if (!s.sideChats[id]) return s;
          const next = { ...s.sideChats };
          delete next[id];
          return { sideChats: next };
        }),
      setSideChatDraft: (id, draft) =>
        set((s) => {
          const thread = s.sideChats[id];
          if (!thread) return s;
          return { sideChats: { ...s.sideChats, [id]: { ...thread, draft } } };
        }),
      startSideChatMessage: (id, content) =>
        set((s) => {
          const thread = s.sideChats[id];
          if (!thread) return s;
          const t = Date.now();
          return {
            sideChats: {
              ...s.sideChats,
              [id]: {
                ...thread,
                isStreaming: true,
                draft: "",
                messages: [
                  ...thread.messages,
                  {
                    id: uniqueMessageId("su"),
                    role: "user",
                    content,
                    artifacts: [],
                    timestamp: t,
                  },
                  {
                    id: uniqueMessageId("sa"),
                    role: "assistant",
                    content: "",
                    blocks: [],
                    artifacts: [],
                    timestamp: t,
                    isStreaming: true,
                  },
                ],
              },
            },
          };
        }),

      // ── Composer ──
      draft: "",
      attachments: [],
      permissionMode:
        (readLS(LS.permissionMode) as ComposerSlice["permissionMode"]) ?? "ask_permissions",
      effortLevel: "high" as const,
      prMonitor: null,
      actionChip: null,
      mentionResults: [],
      selectedMentions: [],
      selectedSkills: [],
      slashPanelOpen: false,
      mentionPanelOpen: false,
      setDraft: (d) => set({ draft: d }),
      addAttachment: (a) =>
        set((s) => ({ attachments: [...s.attachments, a] })),
      updateAttachment: (id, patch) =>
        set((s) => ({
          attachments: s.attachments.map((a) => (a.id === id ? { ...a, ...patch } : a)),
        })),
      removeAttachment: (id) =>
        set((s) => ({ attachments: s.attachments.filter((a) => a.id !== id) })),
      clearAttachments: () => set({ attachments: [] }),
      setPermissionMode: (m) => {
        writeLS(LS.permissionMode, m);
        set({ permissionMode: m });
        syncPermissionMode(m);
      },
      setEffortLevel: (e) => {
        set({ effortLevel: e });
        sendClientCommand({
          type: "llm.config.set",
          provider: get().currentProvider || "openai",
          reasoning_effort: e,
          source: "frontend.footer",
        });
      },
      setPRMonitor: (pr) => set({ prMonitor: pr }),
      setActionChip: (c) => set({ actionChip: c }),
      setMentionResults: (items) => set({ mentionResults: items }),
      addSelectedMention: (item) =>
        set((s) => ({
          selectedMentions: s.selectedMentions.some((existing) => existing.path === item.path)
            ? s.selectedMentions
            : [...s.selectedMentions, item],
        })),
      removeSelectedMention: (path) =>
        set((s) => ({ selectedMentions: s.selectedMentions.filter((item) => item.path !== path) })),
      clearSelectedMentions: () => set({ selectedMentions: [] }),
      addSelectedSkill: (skill) =>
        set((s) => ({
          selectedSkills: s.selectedSkills.some((existing) => existing.name === skill.name)
            ? s.selectedSkills
            : [...s.selectedSkills, { ...skill, kind: "skill" as const }],
        })),
      removeSelectedSkill: (name) =>
        set((s) => ({ selectedSkills: s.selectedSkills.filter((skill) => skill.name !== name) })),
      clearSelectedSkills: () => set({ selectedSkills: [] }),
      openSlashPanel: () => set({ slashPanelOpen: true, mentionPanelOpen: false }),
      closeSlashPanel: () => set({ slashPanelOpen: false }),
      openMentionPanel: () => set({ mentionPanelOpen: true, slashPanelOpen: false }),
      closeMentionPanel: () => set({ mentionPanelOpen: false }),

      // ── Approval ──
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      pendingAskUser: null,
      setApproval: (a) =>
        set((s) => {
          if (
            s.pendingApproval?.requestId === a.requestId ||
            s.approvalQueue.some((queued) => queued.requestId === a.requestId)
          ) {
            return s;
          }
          if (!s.pendingApproval) {
            return { pendingApproval: a };
          }
          return { approvalQueue: [...s.approvalQueue, a].slice(-20) };
        }),
      markApprovalSubmitted: (requestId) =>
        set((s) => ({
          pendingApproval: s.pendingApproval?.requestId === requestId
            ? { ...s.pendingApproval, status: "submitted", error: undefined }
            : s.pendingApproval,
          approvalQueue: s.approvalQueue.map((queued) =>
            queued.requestId === requestId
              ? { ...queued, status: "submitted", error: undefined }
              : queued,
          ),
        })),
      markApprovalError: (requestId, error) =>
        set((s) => ({
          pendingApproval: s.pendingApproval?.requestId === requestId
            ? { ...s.pendingApproval, status: "error", error }
            : s.pendingApproval,
          approvalQueue: s.approvalQueue.map((queued) =>
            queued.requestId === requestId
              ? { ...queued, status: "error", error }
              : queued,
          ),
        })),
      clearApproval: (requestId) =>
        set((s) => {
          if (requestId && s.pendingApproval?.requestId !== requestId) {
            return {
              approvalQueue: s.approvalQueue.filter((queued) => queued.requestId !== requestId),
            };
          }
          const [next, ...rest] = s.approvalQueue;
          return {
            pendingApproval: next ?? null,
            approvalQueue: rest,
          };
        }),
      clearApprovals: (requestIds) =>
        set((s) => {
          if (requestIds.length === 0) return s;
          const ids = new Set(requestIds);
          const pendingApproval = s.pendingApproval && ids.has(s.pendingApproval.requestId)
            ? null
            : s.pendingApproval;
          const approvalQueue = s.approvalQueue.filter((queued) => !ids.has(queued.requestId));
          const [next, ...rest] = approvalQueue;
          return {
            pendingApproval: pendingApproval ?? next ?? null,
            approvalQueue: pendingApproval ? approvalQueue : rest,
          };
        }),
      setDiffReview: (d) => set({ pendingDiffReview: d }),
      clearDiffReview: () => set({ pendingDiffReview: null }),
      setAskUser: (a) => set({ pendingAskUser: a }),
      clearAskUser: () => set({ pendingAskUser: null }),

      // ── Agent ──
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
      budgetBuckets: [],
      totalBudgetPercent: 0,
      setPlan: (p) => set({ plan: p }),
      updatePlanStep: (idx, status) =>
        set((s) => {
          if (!s.plan) return s;
          const steps = s.plan.steps.slice();
          if (idx < 0 || idx >= steps.length) return s;
          steps[idx] = { ...steps[idx], status };
          return { plan: { ...s.plan, steps } };
        }),
      setTodos: (t) => set({ todos: t }),
      updateTodo: (id, patch) =>
        set((s) => ({
          todos: s.todos.map((t) => (t.id === id ? { ...t, ...patch } : t)),
        })),
      addSubagent: (sa) =>
        set((s) => ({
          subagents: [
            ...s.subagents.filter((existing) => existing.id !== sa.id),
            sa,
          ].slice(-20),
        })),
      updateSubagent: (id, patch) =>
        set((s) => ({
          subagents: s.subagents.map((sa) => (sa.id === id ? { ...sa, ...patch } : sa)),
        })),
      removeSubagent: (id) =>
        set((s) => ({ subagents: s.subagents.filter((sa) => sa.id !== id) })),
      appendAgentProgress: (progress, conversationId) =>
        set((s) => {
          const timestamp = Date.now();
          const key = progressConversationKey(conversationId || s.conversationId || undefined);
          const entry = {
            ...progress,
            type: "progress" as const,
            timestamp,
            conversationId: key,
          };
          const existingIdx = s.agentProgress.findIndex((item) =>
            item.conversationId === key && item.id === progress.id,
          );
          if (existingIdx >= 0) {
            const next = s.agentProgress.slice();
            next[existingIdx] = entry;
            return { agentProgress: next.slice(-80) };
          }
          return { agentProgress: [...s.agentProgress, entry].slice(-80) };
        }),
      finishAgentProgress: (conversationId, status = "completed") =>
        set((s) => {
          const key = progressConversationKey(conversationId || s.conversationId || undefined);
          const timestamp = Date.now();
          return {
            agentProgress: s.agentProgress.map((item) =>
              item.conversationId === key && item.status === "running"
                ? { ...item, status, timestamp }
                : item,
            ),
          };
        }),
      clearAgentProgress: (conversationId) =>
        set((s) => {
          const key = progressConversationKey(conversationId || s.conversationId || undefined);
          return {
            agentProgress: s.agentProgress.filter((item) => item.conversationId !== key),
          };
        }),
      setBudget: (buckets, total) =>
        set({ budgetBuckets: buckets, totalBudgetPercent: total }),

      // ── Inspector ──
      inspectorEntries: [],
      inspectorFocus: null,
      addInspectorEntry: (entry) =>
        set((s) => ({
          inspectorEntries: [...s.inspectorEntries.slice(-49), entry],
        })),
      setInspectorFocus: (focus) => set({ inspectorFocus: focus }),
      clearInspector: () => set({ inspectorEntries: [], inspectorFocus: null }),

      // ── Editor Slice ──────────────────────────────────────────────
      editorTabs: initialEditorTabs,
      activeTabPath: initialEditorTabs[0]?.path ?? null,
      openEditorTab: (path) =>
        set((s) => {
          const existing = s.editorTabs.find((t) => t.path === path);
          if (existing) return { activeTabPath: path };
          const tab: EditorTab = { path, content: "", original: "", loading: true, error: null };
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
          return { editorTabs: next, activeTabPath, activeEditorPath };
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
          return { editorTabs: [], activeTabPath: null, activeEditorPath: null };
        }),
      setActiveTab: (path) => set({ activeTabPath: path }),
      updateTabContent: (path, content) =>
        set((s) => ({
          editorTabs: s.editorTabs.map((t) => (t.path === path ? { ...t, content } : t)),
        })),
      markTabLoaded: (path, content, error, contentHash) =>
        set((s) => {
          const loadedTabs = s.editorTabs.map((t) =>
            t.path === path
              ? { ...t, content, original: content, contentHash, loading: false, error: error ?? null }
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
        if (!tab || tab.loading || tab.error) return false;
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
    } satisfies AppStore;
  }),
);

if (typeof window !== "undefined") {
  (window as typeof window & { __zustandStore?: typeof useAppStore }).__zustandStore = useAppStore;
  applyTheme(useAppStore.getState().themeMode);
  applyTextScale(useAppStore.getState().textScale);
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (useAppStore.getState().themeMode === "system") applyTheme("system");
  });
}
