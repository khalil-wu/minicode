import type { StateCreator } from "zustand";
import type { AppStore, UISlice } from "./types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import {
  LS,
  writeLS,
  readLS,
  initialTheme,
  initialTextScale,
  applyTheme,
  applyTextScale,
  ensureCodePanelSlots,
  persistPanelSlots,
  preferredRightSidebarWidth,
  editorStateForWorkspace,
} from "./shared-helpers";
import { clampTextScale } from "../lib/text-scale";

export const createUISlice: StateCreator<AppStore, [], [], UISlice> = (set, get) => ({
  themeMode: initialTheme(),
  textScale: initialTextScale(),
  viewMode: "normal" as const,
  appMode: "code" as const,
  rightStackTab: "tasks" as const,
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
  mcpServers: [],
  envVars: [],
  gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
  skillsMarketplaceOpen: false,
  liveArtifactsOpen: false,
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
      writeLS(LS.layout.rightOpen, "1");
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
    set((s) => {
      if (s.commandPaletteOpen) {
        return { commandPaletteOpen: false };
      }
      // Close all other modals when opening command palette
      return {
        commandPaletteOpen: true,
        settingsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
      };
    }),
  toggleSettings: () =>
    set((s) => {
      if (s.settingsOpen) {
        return { settingsOpen: false };
      }
      // Close all other modals when opening settings
      return {
        settingsOpen: true,
        commandPaletteOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
      };
    }),
  toggleShortcutsHelp: () =>
    set((s) => {
      if (s.shortcutsHelpOpen) {
        return { shortcutsHelpOpen: false };
      }
      // Close all other modals when opening shortcuts help
      return {
        shortcutsHelpOpen: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
      };
    }),
  toggleSkillsMarketplace: () =>
    set((s) => {
      if (s.skillsMarketplaceOpen) {
        return { skillsMarketplaceOpen: false };
      }
      // Close all other modals when opening skills marketplace
      return {
        skillsMarketplaceOpen: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        shortcutsHelpOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
      };
    }),
  toggleLiveArtifacts: () =>
    set((s) => {
      if (s.liveArtifactsOpen) {
        return { liveArtifactsOpen: false };
      }
      // Close all other modals when opening live artifacts
      return {
        liveArtifactsOpen: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        quickOpenVisible: false,
      };
    }),
  toggleQuickOpen: () =>
    set((s) => {
      if (s.quickOpenVisible) {
        return { quickOpenVisible: false };
      }
      // Close all other modals when opening quick open
      return {
        quickOpenVisible: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
      };
    }),
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
    const { requestId, lineComments, protocol } = s.diffReview;
    const comments = lineComments ?? [];
    if (comments.length > 0) {
      sendClientCommand({
        type: "user_message",
        content: `Review the following diff comments and revise accordingly:\n${comments.map((c) => `- ${c.filePath}:${c.lineIndex + 1}: ${c.content}`).join("\n")}`,
      });
    }
    const sent = sendClientCommand(buildApprovalResponseCommand(requestId, "approve", protocol));
    set((state) => ({
      diffReview: state.diffReview?.requestId === requestId
        ? { ...state.diffReview, status: sent ? "submitted" : "error", error: sent ? undefined : "Connection is offline" }
        : state.diffReview,
    }));
  },
  submitPartialApproval: () => {
    const s = get();
    if (!s.diffReview) return;
    const { requestId, fileDecisions, protocol } = s.diffReview;
    const sent = sendClientCommand(buildApprovalResponseCommand(requestId, "partial", protocol, fileDecisions));
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
      writeLS(LS.layout.rightOpen, "1");
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
  setMcpServers: (servers) => set({ mcpServers: servers }),
  setEnvVars: (entries) => set({ envVars: entries }),
  setGitChanges: (changes) =>
    set((s) => ({ gitChanges: { ...s.gitChanges, ...changes } })),
  setGitChangesLoading: (loading) =>
    set((s) => ({ gitChanges: { ...s.gitChanges, loading } })),
  requestGitChanges: () => {
    set((s) => ({ gitChanges: { ...s.gitChanges, loading: true } }));
    sendClientCommand({ type: "diff.git_working_tree" });
    sendClientCommand({ type: "diff.git_staged" });
  },
});
