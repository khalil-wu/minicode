import type { StateCreator } from "zustand";
import type {
  AppStore,
  ArtifactContentState,
  ConversationWorkbenchState,
  DiffReviewState,
  UISlice,
} from "./types";
import {
  commandResultSucceeded,
  createClientCommandId,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendPromptResponseCommand,
} from "../protocol/ws-outbox";
import { desktop } from "../desktop/runtime";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import {
  LS,
  writeLS,
  readLS,
  initialTheme,
  initialResolvedTheme,
  initialTextScale,
  initialCodeTextScale,
  initialReducedMotion,
  initialViewMode,
  initialSendShortcut,
  initialFollowUpBehavior,
  initialShortcutBindings,
  applyTheme,
  applyTextScale,
  applyCodeTextScale,
  applyReducedMotion,
  ensureCodePanelSlots,
  normalizePanelSlots,
  persistPanelSlots,
  preferredRightSidebarWidth,
  editorStateForWorkspace,
} from "./shared-helpers";
import { clampTextScale } from "../lib/text-scale";
import { clamp } from "../lib/clamp";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { diffFileDecisionForPath, diffFilePathsEqual } from "../chat/diffReviewState";

const initialRemoteImagePolicy = (): UISlice["remoteImagePolicy"] => {
  const stored = readLS(LS.remoteImagePolicy);
  return stored === "allow" || stored === "block" ? stored : "ask";
};

function cloneDiffReviewState(state: DiffReviewState | null): DiffReviewState | null {
  if (!state) return null;
  return {
    ...state,
    files: state.files.map((file) => ({ ...file })),
    fileDecisions: { ...state.fileDecisions },
    lineComments: state.lineComments?.map((comment) => ({ ...comment })) ?? [],
  };
}

function cloneArtifactState(state: ArtifactContentState | null): ArtifactContentState | null {
  return state ? { ...state } : null;
}

function emptyConversationWorkbenchState(): ConversationWorkbenchState {
  return {
    diffReview: null,
    previewArtifact: null,
    livePreviewUrl: null,
    terminalSessions: [],
    activeTerminalSessionId: null,
    rightStackTab: "tasks",
    rightPanelOpen: false,
    rightStackTabLocked: false,
    draft: "",
    attachments: [],
    quotedMessage: null,
    selectedMentions: [],
    selectedSkills: [],
    allowedRemoteImageDomains: [],
  };
}

function cloneConversationWorkbenchState(state: ConversationWorkbenchState): ConversationWorkbenchState {
  return {
    ...state,
    diffReview: cloneDiffReviewState(state.diffReview),
    previewArtifact: cloneArtifactState(state.previewArtifact),
    terminalSessions: (state.terminalSessions ?? []).map((session) => ({ ...session })),
    draft: state.draft ?? "",
    attachments: (state.attachments ?? []).map((attachment) => ({ ...attachment })),
    quotedMessage: state.quotedMessage ? { ...state.quotedMessage } : null,
    selectedMentions: (state.selectedMentions ?? []).map((mention) => ({ ...mention })),
    selectedSkills: (state.selectedSkills ?? []).map((skill) => ({ ...skill })),
    allowedRemoteImageDomains: [...(state.allowedRemoteImageDomains ?? [])],
  };
}

function liveConversationWorkbenchState(s: AppStore): ConversationWorkbenchState {
  return {
    diffReview: cloneDiffReviewState(s.diffReview),
    previewArtifact: cloneArtifactState(s.previewArtifact),
    livePreviewUrl: s.livePreviewUrl,
    terminalSessions: s.terminalSessions
      .filter((session) => session.conversationId === s.conversationId)
      .map((session) => ({ ...session })),
    activeTerminalSessionId: s.activeTerminalSessionId,
    rightStackTab: s.rightStackTab,
    rightPanelOpen: s.rightPanelOpen,
    rightStackTabLocked: s.rightStackTabLocked,
    draft: s.draft,
    attachments: s.attachments.map((attachment) => ({ ...attachment })),
    quotedMessage: s.quotedMessage ? { ...s.quotedMessage } : null,
    selectedMentions: s.selectedMentions.map((mention) => ({ ...mention })),
    selectedSkills: s.selectedSkills.map((skill) => ({ ...skill })),
    allowedRemoteImageDomains: [...s.allowedRemoteImageDomains],
  };
}

function storeConversationWorkbenchState(
  s: AppStore,
  conversationId: string,
  state: ConversationWorkbenchState,
): Record<string, ConversationWorkbenchState> {
  return {
    ...(s.conversationWorkbenchStates ?? {}),
    [conversationId]: cloneConversationWorkbenchState(state),
  };
}

function panelSlotsEqual(left: AppStore["panelSlots"], right: AppStore["panelSlots"]): boolean {
  return left.length === right.length && left.every((slot, index) => {
    const candidate = right[index];
    if (!candidate) return false;
    return slot.id === candidate.id
      && slot.kind === candidate.kind
      && slot.label === candidate.label
      && slot.size === candidate.size
      && Boolean(slot.focused) === Boolean(candidate.focused)
      && Boolean(slot.maximized) === Boolean(candidate.maximized);
  });
}

export const createUISlice: StateCreator<AppStore, [], [], UISlice> = (set, get) => ({
  themeMode: initialTheme(),
  resolvedTheme: initialResolvedTheme(),
  textScale: initialTextScale(),
  codeTextScale: initialCodeTextScale(),
  reducedMotion: initialReducedMotion(),
  viewMode: initialViewMode(),
  sendShortcut: initialSendShortcut(),
  followUpBehavior: initialFollowUpBehavior(),
  shortcutBindings: initialShortcutBindings(),
  appMode: "code" as const,
  rightStackTab: "tasks" as const,
  rightStackTabLocked: false,
  focusedSubagentId: null,
  contextUsage: null,
  remoteImagePolicy: initialRemoteImagePolicy(),
  allowedRemoteImageDomains: [],
  commandPaletteOpen: false,
  settingsOpen: false,
  settingsTab: "general" as const,
  automationsOpen: false,
  shortcutsHelpOpen: false,
  quickOpenVisible: false,
  quickOpenResults: [],
  quickOpenLoading: false,
  currentModel: "",
  currentProvider: "",
  currentProviderId: "",
  currentProviderBaseUrl: "",
  currentWireApi: "",
  availableModels: [],
  availableModelLabels: {},
  modelsSource: "",
  availableSkills: [],
  marketplaceSkills: [],
  slashCommands: [],
  workingDirectory: "",
  workspaceGit: null,
  diffReview: null,
  conversationWorkbenchStates: {},
  previewArtifact: null,
  livePreviewUrl: null,
  previewServers: [],
  previewLaunchConfigs: [],
  previewLaunchProcesses: [],
  previewVerification: null,
  fileChanges: [],
  fileTreeVersion: 0,
  fileTreeRevealRequests: [],
  mcpServers: [],
  envVars: [],
  gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
  skillsMarketplaceOpen: false,
  skillsMarketplaceReturnTarget: "app",
  liveArtifactsOpen: false,
  agentEditorOpen: false,
  toggleAgentEditor: () =>
    set((s) => {
      if (s.agentEditorOpen) {
        return { agentEditorOpen: false };
      }
      return {
        agentEditorOpen: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        automationsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
      };
    }),
  setThemeMode: (mode) => {
    writeLS(LS.theme, mode);
    const resolvedTheme = applyTheme(mode);
    set({ themeMode: mode, resolvedTheme });
  },
  setTextScale: (s) => {
    const v = clampTextScale(s);
    writeLS(LS.textScale, String(v));
    applyTextScale(v);
    set({ textScale: v });
  },
  setCodeTextScale: (s) => {
    const value = clamp(0.88, 1.2, s);
    writeLS(LS.codeTextScale, String(value));
    applyCodeTextScale(value);
    set({ codeTextScale: value });
  },
  setReducedMotion: (reduced) => {
    writeLS(LS.reducedMotion, reduced ? "1" : "0");
    applyReducedMotion(reduced);
    set({ reducedMotion: reduced });
  },
  setViewMode: (m) => {
    writeLS(LS.viewMode, m);
    set({ viewMode: m });
  },
  setSendShortcut: (shortcut) => {
    writeLS(LS.sendShortcut, shortcut);
    set({ sendShortcut: shortcut });
  },
  setFollowUpBehavior: (behavior) => {
    writeLS(LS.followUpBehavior, behavior);
    set({ followUpBehavior: behavior });
  },
  setShortcutBinding: (action, binding) => set((state) => {
    const shortcutBindings = { ...state.shortcutBindings, [action]: binding };
    writeLS(LS.shortcutBindings, JSON.stringify(shortcutBindings));
    return { shortcutBindings };
  }),
  resetShortcutBindings: () => {
    const shortcutBindings = { ...DEFAULT_SHORTCUT_BINDINGS };
    writeLS(LS.shortcutBindings, JSON.stringify(shortcutBindings));
    set({ shortcutBindings });
  },
  setAppMode: (m) =>
    set((s) => {
      if (m !== "code") {
        if (m === "cowork" && s.editorTabs.length === 0 && s.panelSlots.some((slot) => slot.kind === "editor")) {
          const panelSlots = normalizePanelSlots(s.panelSlots.filter((slot) => slot.kind !== "editor"));
          if (!panelSlotsEqual(s.panelSlots, panelSlots)) {
            persistPanelSlots(panelSlots);
            return { appMode: m, panelSlots };
          }
        }
        if (s.appMode === m) return s;
        return { appMode: m };
      }
      const panelSlots = ensureCodePanelSlots(s.panelSlots);
      const layoutChanged = !panelSlotsEqual(s.panelSlots, panelSlots);
      if (!layoutChanged && s.appMode === m) return s;
      if (layoutChanged) persistPanelSlots(panelSlots);
      return layoutChanged ? { appMode: m, panelSlots } : { appMode: m };
    }),
  ensureCodeLayout: () =>
    set((s) => {
      const panelSlots = ensureCodePanelSlots(s.panelSlots);
      if (panelSlotsEqual(s.panelSlots, panelSlots)) return s;
      persistPanelSlots(panelSlots);
      return { panelSlots };
    }),
  setRightStackTab: (t, options) =>
    set((s) => {
      if (t === "terminal") {
        writeLS(LS.layout.dockTab, "terminal");
        writeLS(LS.layout.dockCollapsed, "0");
        return {
          activeBottomTab: "terminal",
          dockCollapsed: false,
        };
      }
      // Legacy callers and replayed command results may still request a
      // separate plan panel. MiniCode surfaces plan state in the task/activity
      // view, so keep one canonical destination instead of storing a tab that
      // SidebarRight cannot render.
      const canonicalTab = t === "plan" ? "tasks" : t;
      const rightSidebarWidth = preferredRightSidebarWidth(canonicalTab, s.rightSidebarWidth);
      if (rightSidebarWidth !== s.rightSidebarWidth) {
        writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
      }
      writeLS(LS.layout.rightOpen, "1");
      return {
        rightStackTab: canonicalTab,
        rightStackTabLocked: options?.automatic ? s.rightStackTabLocked : true,
        rightPanelOpen: true,
        rightSidebarWidth,
      };
    }),
  setRightStackTabLocked: (locked) => set({ rightStackTabLocked: locked }),
  setFocusedSubagentId: (id) => set({ focusedSubagentId: id }),
  setContextUsage: (u) => set({ contextUsage: u }),
  setRemoteImagePolicy: (policy) => {
    writeLS(LS.remoteImagePolicy, policy);
    set({ remoteImagePolicy: policy });
  },
  allowRemoteImageDomain: (domain) =>
    set((s) => {
      const normalized = domain.trim().toLowerCase();
      if (!normalized || s.allowedRemoteImageDomains.includes(normalized)) return s;
      return { allowedRemoteImageDomains: [...s.allowedRemoteImageDomains, normalized] };
    }),
  clearAllowedRemoteImageDomains: () => set({ allowedRemoteImageDomains: [] }),
  toggleCommandPalette: () =>
    set((s) => {
      if (s.commandPaletteOpen) {
        return { commandPaletteOpen: false };
      }
      // Close all other modals when opening command palette
      return {
        commandPaletteOpen: true,
        settingsOpen: false,
        automationsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
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
        automationsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
      };
    }),
  setSettingsTab: (tab) => set({ settingsTab: tab }),
  toggleAutomations: () =>
    set((s) => {
      if (s.automationsOpen) {
        return { automationsOpen: false };
      }
      return {
        automationsOpen: true,
        commandPaletteOpen: false,
        settingsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
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
        automationsOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
      };
    }),
  toggleSkillsMarketplace: (returnTarget = "app") =>
    set((s) => {
      if (s.skillsMarketplaceOpen) {
        return { skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" };
      }
      // Close all other modals when opening skills marketplace
      return {
        skillsMarketplaceOpen: true,
        skillsMarketplaceReturnTarget: returnTarget,
        commandPaletteOpen: false,
        settingsOpen: false,
        automationsOpen: false,
        shortcutsHelpOpen: false,
        liveArtifactsOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
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
        automationsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        quickOpenVisible: false,
        agentEditorOpen: false,
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
        automationsOpen: false,
        shortcutsHelpOpen: false,
        skillsMarketplaceOpen: false,
        liveArtifactsOpen: false,
        agentEditorOpen: false,
      };
    }),
  setCurrentModel: (m) => set({ currentModel: m }),
  setCurrentProvider: (p) => set({ currentProvider: p }),
  setCurrentProviderMeta: (meta) =>
    set({
      currentProviderId: String(meta.providerId || ""),
      currentProviderBaseUrl: String(meta.baseUrl || ""),
      currentWireApi: String(meta.wireApi || ""),
    }),
  setAvailableModels: (models) => set({ availableModels: models }),
  setAvailableModelLabels: (labels) => set({ availableModelLabels: labels }),
  setModelsSource: (source) => set({ modelsSource: source }),
  setAvailableSkills: (skills) => set({ availableSkills: skills }),
  setSlashCommands: (cmds) => set({ slashCommands: cmds }),
  setMarketplaceSkills: (skills) => set({ marketplaceSkills: skills }),
  setWorkingDirectory: (d) => {
    set((s) => {
      const workspaceChanged = !workspaceRootsEqual(d, s.workingDirectory);
      return {
        ...(workspaceChanged ? editorStateForWorkspace(d) : {}),
        workingDirectory: d,
        workspaceGit: workspaceChanged ? null : s.workspaceGit,
      };
    });
    if (d) {
      const rt = desktop();
      if (rt?.trustWorkspace) rt.trustWorkspace(d);
    }
  },
  setWorkspaceGit: (state) => set({ workspaceGit: state }),
  setDiffReviewState: (state) => set({ diffReview: state }),
  snapshotWorkbenchState: (conversationId) =>
    set((s) => {
      const targetId = conversationId || s.conversationId || undefined;
      if (!targetId) return s;
      return {
        conversationWorkbenchStates: storeConversationWorkbenchState(
          s,
          targetId,
          liveConversationWorkbenchState(s),
        ),
      };
    }),
  restoreWorkbenchState: (conversationId) =>
    set((s) => {
      const targetId = conversationId || s.conversationId || undefined;
      const stored = targetId
        ? s.conversationWorkbenchStates?.[targetId]
        : undefined;
      const next = cloneConversationWorkbenchState(stored ?? emptyConversationWorkbenchState());
      const terminalSessions = (next.terminalSessions ?? [])
        .filter((session) => session.conversationId === targetId)
        .map((session) => ({ ...session }));
      // Keep the preferred id until the authoritative terminal.list arrives.
      // It may not be present in an older UI cache, but clearing it here would
      // overwrite a valid backend session before reconciliation can occur.
      const activeTerminalSessionId = next.activeTerminalSessionId;
      return {
        diffReview: next.diffReview,
        previewArtifact: next.previewArtifact,
        livePreviewUrl: next.livePreviewUrl,
        terminalSessions,
        activeTerminalSessionId,
        rightStackTab: next.rightStackTab,
        rightPanelOpen: next.rightPanelOpen,
        rightStackTabLocked: next.rightStackTabLocked,
        draft: next.draft,
        attachments: next.attachments,
        quotedMessage: next.quotedMessage,
        selectedMentions: next.selectedMentions,
        selectedSkills: next.selectedSkills,
        allowedRemoteImageDomains: next.allowedRemoteImageDomains,
        ...(targetId
          ? {
              conversationWorkbenchStates: storeConversationWorkbenchState(
                s,
                targetId,
                { ...next, terminalSessions, activeTerminalSessionId },
              ),
            }
          : {}),
      };
    }),
  clearConversationWorkbenchState: (conversationId) =>
    set((s) => {
      const next = { ...(s.conversationWorkbenchStates ?? {}) };
      delete next[conversationId];
      if (s.conversationId !== conversationId) return { conversationWorkbenchStates: next };
      return {
        ...emptyConversationWorkbenchState(),
        conversationWorkbenchStates: next,
      };
    }),
  updateDiffReviewFile: (path, patch) =>
    set((s) => ({
      diffReview: s.diffReview
        ? {
            ...s.diffReview,
            files: s.diffReview.files.map((file) =>
              diffFilePathsEqual(file.path, path, s.workingDirectory) ? { ...file, ...patch } : file,
            ),
            diff: diffFilePathsEqual(s.diffReview.selectedPath, path, s.workingDirectory) && patch.patch != null
              ? patch.patch
              : s.diffReview.diff,
          }
        : null,
    })),
  setDiffReviewSelectedPath: (path) =>
    set((s) => {
      if (!s.diffReview) return s;
      const file = s.diffReview.files.find((item) =>
        diffFilePathsEqual(item.path, path, s.workingDirectory),
      );
      return {
        diffReview: {
          ...s.diffReview,
          selectedPath: file?.path ?? path,
          diff: file?.patch ?? s.diffReview.diff,
        },
      };
    }),
  setDiffFileDecision: (path, decision) =>
    set((s) => {
      if (!s.diffReview) return s;
      const fileDecisions = Object.fromEntries(
        Object.entries(s.diffReview.fileDecisions).filter(([candidate]) =>
          !diffFilePathsEqual(candidate, path, s.workingDirectory),
        ),
      ) as Record<string, "approved" | "rejected">;
      fileDecisions[path] = decision;
      return {
        diffReview: {
          ...s.diffReview,
          fileDecisions,
          files: s.diffReview.files.map((f) =>
            diffFilePathsEqual(f.path, path, s.workingDirectory) ? { ...f, decision } : f,
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
            (c) => !(diffFilePathsEqual(c.filePath, filePath, s.workingDirectory) && c.lineIndex === lineIndex),
          ),
        },
      };
    }),
  submitDiffReviewWithComments: async () => {
    const s = get();
    if (!s.diffReview) return;
    const { requestId, lineComments, conversationId, turnId, messageId } = s.diffReview;
    const comments = lineComments ?? [];
    set((state) => ({
      diffReview: state.diffReview?.requestId === requestId
        ? { ...state.diffReview, status: "submitted", error: undefined }
        : state.diffReview,
    }));
    const feedback = comments.length > 0
      ? `Revise the proposed change using these review comments:\n${comments.map((c) => `- ${c.filePath}:${c.lineIndex + 1}: ${c.content}`).join("\n")}`
      : undefined;
    const command = buildApprovalResponseCommand(
      requestId,
      comments.length > 0 ? "reject" : "approve",
      { feedback, owner: { conversationId, turnId, messageId } },
    );
    try {
      const result = await sendPromptResponseCommand(command);
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      get().clearDiffReview(requestId);
    } catch (error) {
      set((state) => ({
        diffReview: state.diffReview?.requestId === requestId
          ? { ...state.diffReview, status: "error", error: error instanceof Error ? error.message : "审批提交失败" }
          : state.diffReview,
      }));
    }
  },
  submitPartialApproval: async () => {
    const s = get();
    if (!s.diffReview) return;
    const { requestId, fileDecisions, files, conversationId, turnId, messageId } = s.diffReview;
    const approved = files
      .filter((file) => diffFileDecisionForPath(fileDecisions, file.path, s.workingDirectory) === "approved")
      .map((file) => file.path);
    const rejected = files
      .filter((file) => diffFileDecisionForPath(fileDecisions, file.path, s.workingDirectory) === "rejected")
      .map((file) => file.path);
    const action = rejected.length === 0 ? "approve" : "reject";
    const feedback = approved.length > 0 && rejected.length > 0
      ? [
          "The proposed tool call is atomic and was not executed.",
          `Reissue a new tool call containing only these approved files: ${approved.join(", ")}.`,
          `Do not include these rejected files: ${rejected.join(", ")}.`,
        ].join("\n")
      : rejected.length > 0
        ? `Do not modify these rejected files: ${rejected.join(", ")}.`
        : undefined;
    set((state) => ({
      diffReview: state.diffReview?.requestId === requestId
        ? { ...state.diffReview, status: "submitted", error: undefined }
        : state.diffReview,
    }));
    const command = buildApprovalResponseCommand(
      requestId,
      action,
      { feedback, owner: { conversationId, turnId, messageId } },
    );
    try {
      const result = await sendPromptResponseCommand(command);
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      get().clearDiffReview(requestId);
    } catch (error) {
      set((state) => ({
        diffReview: state.diffReview?.requestId === requestId
          ? { ...state.diffReview, status: "error", error: error instanceof Error ? error.message : "审批提交失败" }
          : state.diffReview,
      }));
    }
  },
  setPreviewArtifact: (artifact) => set({ previewArtifact: artifact }),
  setConversationPreviewArtifact: (conversationId, artifact) =>
    set((s) => {
      const targetId = String(conversationId || "").trim();
      if (!targetId) return s;
      const nextWorkbench = {
        ...(s.conversationWorkbenchStates ?? {}),
        [targetId]: {
          ...(s.conversationWorkbenchStates?.[targetId] ?? emptyConversationWorkbenchState()),
          previewArtifact: cloneArtifactState(artifact),
        },
      };
      return targetId === s.conversationId
        ? { previewArtifact: cloneArtifactState(artifact), conversationWorkbenchStates: nextWorkbench }
        : { conversationWorkbenchStates: nextWorkbench };
    }),
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
  requestFileTreeReveal: (path, kind = "folder") =>
    set((s) => {
      const panelSlots = ensureCodePanelSlots(s.panelSlots);
      persistPanelSlots(panelSlots);
      return {
        appMode: "code",
        panelSlots,
        fileTreeRevealRequests: [
          ...s.fileTreeRevealRequests,
          {
            id: `reveal-${Date.now().toString(36)}-${s.fileTreeRevealRequests.length}`,
            path,
            kind,
          },
        ],
      };
    }),
  consumeFileTreeRevealRequest: (id) =>
    set((s) => ({
      fileTreeRevealRequests: s.fileTreeRevealRequests.filter((request) => request.id !== id),
    })),
  setMcpServers: (servers) => set({ mcpServers: servers }),
  setEnvVars: (entries) => set({ envVars: entries }),
  setGitChanges: (changes) =>
    set((s) => ({ gitChanges: { ...s.gitChanges, ...changes } })),
  setGitChangesLoading: (loading) =>
    set((s) => ({ gitChanges: { ...s.gitChanges, loading } })),
  requestGitChanges: () => {
    const state = get();
    const conversationId = String(state.conversationId || "").trim();
    const workspaceRoot = String(state.workingDirectory || "").trim();
    if (!conversationId || !workspaceRoot) {
      set((s) => ({
        gitChanges: {
          ...s.gitChanges,
          workingTree: [],
          staged: [],
          untracked: [],
          loading: false,
          workspaceRoot,
          workingTreeRequestId: undefined,
          stagedRequestId: undefined,
        },
      }));
      return;
    }
    const workingTreeRequestId = createClientCommandId();
    const stagedRequestId = createClientCommandId();
    set((s) => ({
      gitChanges: {
        ...s.gitChanges,
        loading: true,
        workspaceRoot,
        workingTreeRequestId,
        stagedRequestId,
      },
    }));
    sendClientCommand({
      type: "diff.git_working_tree",
      conversation_id: conversationId,
      workspace: workspaceRoot,
      request_id: workingTreeRequestId,
      client_command_id: workingTreeRequestId,
    }, { silent: true });
    sendClientCommand({
      type: "diff.git_staged",
      conversation_id: conversationId,
      workspace: workspaceRoot,
      request_id: stagedRequestId,
      client_command_id: stagedRequestId,
    }, { silent: true });
  },
});
