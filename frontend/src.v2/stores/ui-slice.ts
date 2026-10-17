import type { StateCreator } from "zustand";
import { composerDraftOf, composerWorkspaceKey, emptyComposerDraft } from "./composer-drafts";
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
import { normalizeArtifactContentState } from "../lib/artifact-projection";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import {
  LS,
  writeLS,
  readLS,
  initialTheme,
  initialAppearanceState,
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
  cacheEditorStateForWorkspace,
  editorStateForWorkspace,
} from "./shared-helpers";
import { clampTextScale } from "../lib/text-scale";
import { effectiveAppearanceColors, type AppearanceVariant } from "../lib/appearance-colors";
import { defaultThemePreferences } from "../lib/appearance-themes";
import { formatDiffCommentsPrompt, type DiffComment } from "../lib/diff-comments";
import { APPEARANCE_STORAGE_KEYS, readAppearanceStorage, readAppearanceVariant, writeAppearanceVariant, type SavedAppearanceVariant } from "../lib/appearance-storage";
import { loadCodeTheme } from "../lib/code-themes";
import { clamp } from "../lib/clamp";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { diffFileDecisionForPath, diffFilePathsEqual } from "../chat/diffReviewState";
import { openWebInBrowser } from "../chat/openWebInBrowser";
import { pushToast } from "../overlays/ToastContainer";
import { readDiffViewPreference } from "../chat/diff/diff-view-preferences";

const CLOSED_OVERLAYS = {
  commandPaletteOpen: false,
  settingsOpen: false,
  shortcutsHelpOpen: false,
  skillsMarketplaceOpen: false,
  liveArtifactsOpen: false,
  quickOpenVisible: false,
  agentEditorOpen: false,
};

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
  return state ? { ...normalizeArtifactContentState(state) } : null;
}

export function emptyConversationWorkbenchState(): ConversationWorkbenchState {
  return {
    agentMode: "build",
    diffReview: null,
    gitReviewRequest: null,
    previewArtifact: null,
    livePreviewUrl: null,
    previewServers: [],
    previewLaunchConfigs: [],
    previewLaunchProcesses: [],
    previewVerification: null,
    terminalSessions: [],
    activeTerminalSessionId: null,
    rightStackTab: "tasks",
    rightPanelOpen: false,
    rightPanelExpanded: false,
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
    agentMode: state.agentMode ?? "build",
    diffReview: cloneDiffReviewState(state.diffReview),
    previewArtifact: cloneArtifactState(state.previewArtifact),
    previewServers: (state.previewServers ?? []).map((server) => ({ ...server })),
    previewLaunchConfigs: (state.previewLaunchConfigs ?? []).map((config) => ({ ...config })),
    previewLaunchProcesses: (state.previewLaunchProcesses ?? []).map((process) => ({
      ...process,
      stderr_tail: process.stderr_tail ? [...process.stderr_tail] : process.stderr_tail,
      output_tail: process.output_tail?.map((line) => ({ ...line })),
    })),
    previewVerification: state.previewVerification ? { ...state.previewVerification } : null,
    terminalSessions: (state.terminalSessions ?? []).map((session) => ({ ...session })),
    draft: state.draft ?? "",
    attachments: (state.attachments ?? []).map((attachment) => ({ ...attachment })),
    quotedMessage: state.quotedMessage ? { ...state.quotedMessage } : null,
    selectedMentions: (state.selectedMentions ?? []).map((mention) => ({ ...mention })),
    selectedSkills: (state.selectedSkills ?? []).map((skill) => ({ ...skill })),
    allowedRemoteImageDomains: [...(state.allowedRemoteImageDomains ?? [])],
  };
}

export function liveConversationWorkbenchState(s: AppStore): ConversationWorkbenchState {
  return {
    agentMode: s.agentMode,
    diffReview: cloneDiffReviewState(s.diffReview),
    gitReviewRequest: s.gitReviewRequest,
    previewArtifact: cloneArtifactState(s.previewArtifact),
    livePreviewUrl: s.livePreviewUrl,
    previewServers: s.previewServers.map((server) => ({ ...server })),
    previewLaunchConfigs: s.previewLaunchConfigs.map((config) => ({ ...config })),
    previewLaunchProcesses: s.previewLaunchProcesses.map((process) => ({
      ...process,
      stderr_tail: process.stderr_tail ? [...process.stderr_tail] : process.stderr_tail,
      output_tail: process.output_tail?.map((line) => ({ ...line })),
    })),
    previewVerification: s.previewVerification ? { ...s.previewVerification } : null,
    terminalSessions: s.terminalSessions
      .filter((session) => session.conversationId === s.conversationId)
      .map((session) => ({ ...session })),
    activeTerminalSessionId: s.activeTerminalSessionId,
    rightStackTab: s.rightStackTab,
    rightPanelOpen: s.rightPanelOpen,
    rightPanelExpanded: s.rightPanelExpanded,
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

type PreviewWorkbenchPatch = Pick<
  ConversationWorkbenchState,
  "previewArtifact"
  | "livePreviewUrl"
  | "previewServers"
  | "previewLaunchConfigs"
  | "previewLaunchProcesses"
  | "previewVerification"
>;

function updatePreviewWorkbench(
  s: AppStore,
  conversationId: string | undefined,
  patch: Partial<PreviewWorkbenchPatch>,
): Partial<AppStore> {
  const targetId = String(conversationId || s.conversationId || "").trim();
  if (!targetId) return patch as Partial<AppStore>;
  // The first scoped preview update for the active conversation must retain
  // its live mirror. Starting from an empty state loses an already-open image
  // whenever an unrelated preview-server event arrives.
  const current = s.conversationWorkbenchStates?.[targetId]
    ?? (targetId === s.conversationId
      ? liveConversationWorkbenchState(s)
      : emptyConversationWorkbenchState());
  const nextWorkbench: ConversationWorkbenchState = cloneConversationWorkbenchState({
    ...current,
    ...patch,
  });
  return {
    conversationWorkbenchStates: {
      ...(s.conversationWorkbenchStates ?? {}),
      [targetId]: nextWorkbench,
    },
    ...(targetId === s.conversationId ? patch : {}),
} as Partial<AppStore>;
}

function previewStateForConversation(s: AppStore, conversationId: string | undefined): PreviewWorkbenchPatch {
  const targetId = String(conversationId || "").trim();
  const stored = targetId ? s.conversationWorkbenchStates?.[targetId] : undefined;
  if (stored) {
    const next = cloneConversationWorkbenchState(stored);
    return {
      previewArtifact: next.previewArtifact,
      livePreviewUrl: next.livePreviewUrl,
      previewServers: next.previewServers,
      previewLaunchConfigs: next.previewLaunchConfigs,
      previewLaunchProcesses: next.previewLaunchProcesses,
      previewVerification: next.previewVerification,
    };
  }
  if (targetId && targetId === s.conversationId) {
    return {
      previewArtifact: cloneArtifactState(s.previewArtifact),
      livePreviewUrl: s.livePreviewUrl,
      previewServers: s.previewServers.map((server) => ({ ...server })),
      previewLaunchConfigs: s.previewLaunchConfigs.map((config) => ({ ...config })),
      previewLaunchProcesses: s.previewLaunchProcesses.map((process) => ({
        ...process,
        stderr_tail: process.stderr_tail ? [...process.stderr_tail] : process.stderr_tail,
        output_tail: process.output_tail?.map((line) => ({ ...line })),
      })),
      previewVerification: s.previewVerification ? { ...s.previewVerification } : null,
    };
  }
  return {
    previewArtifact: null,
    livePreviewUrl: null,
    previewServers: [],
    previewLaunchConfigs: [],
    previewLaunchProcesses: [],
    previewVerification: null,
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

export const createUISlice: StateCreator<AppStore, [], [], UISlice> = (set, get) => {
  const appearance = initialAppearanceState();
  const themeEditRevision = { light: 0, dark: 0 };
  const applySavedAppearance = (appearance: ReturnType<typeof readAppearanceStorage>) => {
    const mode = initialTheme();
    const resolvedTheme = applyTheme(mode, appearance);
    set({ ...appearance, themeMode: mode, resolvedTheme });
  };
  const saveAppearanceTheme = async (variant: AppearanceVariant, snapshot: Parameters<UISlice["importAppearanceTheme"]>[1],
    revision: number, previous: SavedAppearanceVariant) => {
    const { prepareCodeHighlighting } = await import("../lib/code-highlighting");
    await prepareCodeHighlighting(snapshot.codeThemeId, variant);
    // A newer local edit or another window owns the latest value while the
    // highlighter is loading; its configuration must not be overwritten.
    if (revision !== themeEditRevision[variant]
      || JSON.stringify(previous) !== JSON.stringify(readAppearanceVariant(variant))) return;
    const { accent, surface, ink, contrast, opaqueWindows, semanticColors } = snapshot.theme;
    applySavedAppearance(writeAppearanceVariant(variant, { colors: { accent, surface, ink }, theme: {
      codeThemeId: snapshot.codeThemeId, contrast, opaqueWindows, semanticColors,
    } }));
  };
  const commitAppearanceTheme: UISlice["importAppearanceTheme"] = async (variant, snapshot) => {
    const revision = ++themeEditRevision[variant];
    await saveAppearanceTheme(variant, snapshot, revision, readAppearanceVariant(variant));
  };
  return ({
  contextCardCollapsed: true,
  setContextCardCollapsed: (collapsed) => set({ contextCardCollapsed: collapsed }),
  themeMode: initialTheme(),
  resolvedTheme: initialResolvedTheme(),
  ...appearance,
  textScale: initialTextScale(),
  codeTextScale: initialCodeTextScale(),
  reducedMotion: initialReducedMotion(),
  viewMode: initialViewMode(),
  sendShortcut: initialSendShortcut(),
  followUpBehavior: initialFollowUpBehavior(),
  shortcutBindings: initialShortcutBindings(),
  appMode: "cowork" as const,
  workbenchLayout: readLS(LS.layout.workbench) === "split" ? "split" : "tabs",
  rightStackTab: "tasks" as const,
  rightStackTabLocked: false,
  focusedSubagentId: null,
  contextUsage: null,
  remoteImagePolicy: initialRemoteImagePolicy(),
  allowedRemoteImageDomains: [],
  commandPaletteOpen: false,
  settingsOpen: false,
  settingsTab: "general" as const,
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
  modelSelectionMode: null,
  defaultModelSelection: null,
  availableModelOptions: [],
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
  previewOwnerConversationId: null,
  previewServiceManagerRequest: null,
  fileChanges: [],
  fileTreeVersion: 0,
  fileTreeRevealRequests: [],
  mcpServers: [],
  envVars: [],
  gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
  skillsMarketplaceOpen: false,
  skillsMarketplaceTab: "skills",
  skillsMarketplaceSelectedPluginId: null,
  skillsMarketplaceReturnTarget: "app",
  liveArtifactsOpen: false,
  agentEditorOpen: false,
  toggleAgentEditor: () =>
    set((s) => {
      if (s.agentEditorOpen) {
        return { agentEditorOpen: false };
      }
      return {
        ...CLOSED_OVERLAYS,
        agentEditorOpen: true,
      };
    }),
  setThemeMode: (mode) => {
    localStorage.setItem(APPEARANCE_STORAGE_KEYS.mode, mode);
    applySavedAppearance(readAppearanceStorage());
  },
  setAppearanceColor: (variant, role, value) => {
    ++themeEditRevision[variant];
    const current = readAppearanceVariant(variant);
    applySavedAppearance(writeAppearanceVariant(variant, { ...current,
      colors: { ...(current.colors ?? effectiveAppearanceColors()), [role]: value } }));
  },
  setCodeThemeId: async (variant, codeThemeId) => {
    const revision = ++themeEditRevision[variant];
    const previous = readAppearanceVariant(variant);
    const { chromeSeed } = await loadCodeTheme(codeThemeId, variant);
    if (revision !== themeEditRevision[variant]) return;
    const preferences = previous.theme ?? defaultThemePreferences(variant);
    await saveAppearanceTheme(variant, { codeThemeId, theme: { ...preferences, ...chromeSeed, accentSource: "custom" } }, revision, previous);
  },
  importAppearanceTheme: commitAppearanceTheme,
  setTextScale: (s) => {
    const v = clampTextScale(s);
    writeLS(LS.textScale, String(v));
    applyTextScale(v);
    set({ textScale: v });
  },
  setCodeTextScale: (s) => {
    const value = clamp(11 / 14, 24 / 14, s);
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
        if (s.appMode === m) return s;
        return { appMode: m };
      }
      const panelSlots = ensureCodePanelSlots(s.panelSlots);
      const layoutChanged = !panelSlotsEqual(s.panelSlots, panelSlots);
      if (!layoutChanged && s.appMode === m) return s;
      if (layoutChanged) persistPanelSlots(panelSlots);
      return layoutChanged ? { appMode: m, panelSlots } : { appMode: m };
    }),
  setWorkbenchLayout: (layout) => {
    writeLS(LS.layout.workbench, layout);
    set({ workbenchLayout: layout });
  },
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
      writeLS(LS.layout.rightOpen, "1");
      return {
        rightStackTab: canonicalTab,
        ...(canonicalTab === "sidechat" ? { sideChatOpen: true } : {}),
        rightStackTabLocked: options?.automatic ? s.rightStackTabLocked : true,
        rightPanelOpen: true,
        rightPanelExpanded: (["browser", "preview", "diff", "artifacts"].includes(canonicalTab) || canonicalTab.startsWith("mcp-app:")) && s.rightPanelExpanded,
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
        ...CLOSED_OVERLAYS,
        commandPaletteOpen: true,
      };
    }),
  toggleSettings: () =>
    set((s) => {
      if (s.settingsOpen) {
        return { settingsOpen: false };
      }
      // Close all other modals when opening settings
      return {
        ...CLOSED_OVERLAYS,
        settingsOpen: true,
      };
    }),
  setSettingsTab: (tab) => set({ settingsTab: tab }),
  toggleShortcutsHelp: () =>
    set((s) => {
      if (s.shortcutsHelpOpen) {
        return { shortcutsHelpOpen: false };
      }
      // Close all other modals when opening shortcuts help
      return {
        ...CLOSED_OVERLAYS,
        shortcutsHelpOpen: true,
      };
    }),
  toggleSkillsMarketplace: (returnTarget = "app", tab = "skills") =>
    set((s) => {
      if (s.skillsMarketplaceOpen) {
        return { skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app", skillsMarketplaceSelectedPluginId: null };
      }
      // Close all other modals when opening skills marketplace
      return {
        ...CLOSED_OVERLAYS,
        skillsMarketplaceOpen: true,
        skillsMarketplaceTab: tab,
        skillsMarketplaceSelectedPluginId: null,
        skillsMarketplaceReturnTarget: returnTarget,
      };
    }),
  toggleLiveArtifacts: () =>
    set((s) => {
      if (s.liveArtifactsOpen) {
        return { liveArtifactsOpen: false };
      }
      // Close all other modals when opening live artifacts
      return {
        ...CLOSED_OVERLAYS,
        liveArtifactsOpen: true,
      };
    }),
  toggleQuickOpen: () =>
    set((s) => {
      if (s.quickOpenVisible) {
        return { quickOpenVisible: false };
      }
      // Close all other modals when opening quick open
      return {
        ...CLOSED_OVERLAYS,
        quickOpenVisible: true,
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
      const workspaceComposerDrafts = workspaceChanged && !s.conversationId
        ? { ...s.workspaceComposerDrafts, [composerWorkspaceKey(s.workingDirectory)]: composerDraftOf(s) }
        : s.workspaceComposerDrafts;
      if (workspaceChanged) {
        cacheEditorStateForWorkspace(
          s.workingDirectory,
          s.editorTabs,
          s.activeTabPath,
          s.activeEditorPath,
        );
      }
      return {
        ...(workspaceChanged ? editorStateForWorkspace(d) : {}),
        workingDirectory: d,
        ...(workspaceChanged ? {
          availableSkills: [], selectedSkills: [], selectedMentions: [], mentionResults: [],
          slashCommands: [], runtimeCapabilities: null,
          scheduledTasks: [], scheduledTaskRuns: [],
          gitReviewRequest: null, rightPanelExpanded: false,
        } : {}),
        workspaceComposerDrafts,
        ...(workspaceChanged && !s.conversationId
          ? workspaceComposerDrafts[composerWorkspaceKey(d)] ?? emptyComposerDraft()
          : {}),
        workspaceGit: workspaceChanged ? null : s.workspaceGit,
        ...(workspaceChanged ? { gitChanges: { workingTree: [], staged: [], untracked: [], loading: false, workspaceRoot: d } } : {}),
        mcpServers: workspaceChanged ? [] : s.mcpServers,
      };
    });
    if (d) {
      const rt = desktop();
      if (rt?.trustWorkspace) void rt.trustWorkspace(d).catch((error) => {
        pushToast(`无法信任工作区 ${d}：${error instanceof Error ? error.message : String(error)}`, "error");
      });
    }
  },
  setWorkspaceGit: (state) => set({ workspaceGit: state }),
  setDiffReviewState: (state) => set((current) => ({
    diffReview: state,
    ...(state && state.requestId !== current.diffReview?.requestId ? { gitReviewRequest: null } : {}),
  })),
  snapshotWorkbenchState: (conversationId) =>
    set((s) => {
      const targetId = conversationId || s.conversationId || undefined;
      if (!targetId) return { workspaceComposerDrafts: {
        ...s.workspaceComposerDrafts, [composerWorkspaceKey(s.workingDirectory)]: composerDraftOf(s),
      } };
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
      const next = cloneConversationWorkbenchState(stored ?? {
        ...emptyConversationWorkbenchState(),
        agentMode: targetId ? s.conversations.find(conversation => conversation.id === targetId)?.agentMode ?? "build" : "build",
        ...(!targetId ? s.workspaceComposerDrafts[composerWorkspaceKey(s.workingDirectory)] : {}),
      });
      const terminalSessions = (next.terminalSessions ?? [])
        .filter((session) => session.conversationId === targetId)
        .map((session) => ({ ...session }));
      // Keep the preferred id until the authoritative terminal.list arrives.
      // It may not be present in an older UI cache, but clearing it here would
      // overwrite a valid backend session before reconciliation can occur.
      const activeTerminalSessionId = next.activeTerminalSessionId;
      return {
        diffReview: next.diffReview,
        gitReviewRequest: next.gitReviewRequest ?? null,
        previewArtifact: next.previewArtifact,
        livePreviewUrl: next.livePreviewUrl,
        previewServers: next.previewServers,
        previewLaunchConfigs: next.previewLaunchConfigs,
        previewLaunchProcesses: next.previewLaunchProcesses,
        previewVerification: next.previewVerification,
        previewOwnerConversationId: targetId ?? null,
        terminalSessions,
        activeTerminalSessionId,
        rightStackTab: next.rightStackTab === "sidechat" && !s.sideChatOpen ? "tasks" : next.rightStackTab,
        rightPanelOpen: next.rightPanelOpen,
        rightPanelExpanded: next.rightPanelExpanded ?? false,
        rightStackTabLocked: next.rightStackTabLocked,
        draft: next.draft,
        agentMode: next.agentMode ?? "build",
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
        previewOwnerConversationId: null,
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
      const root = s.diffReview.workspaceRoot ?? s.workingDirectory;
      const matches = (current: typeof comment) => diffFilePathsEqual(current.filePath, comment.filePath, root)
        && current.position.side === comment.position.side && current.position.line === comment.position.line;
      const existing = s.diffReview.lineComments ?? [];
      return {
        diffReview: {
          ...s.diffReview,
          lineComments: existing.some(matches) ? existing.map(current => matches(current) ? comment : current) : [...existing, comment],
        },
      };
    }),
  removeDiffLineComment: (position) =>
    set((s) => {
      if (!s.diffReview) return s;
      return {
        diffReview: {
          ...s.diffReview,
          lineComments: (s.diffReview.lineComments ?? []).filter(
            (c) => !(diffFilePathsEqual(c.position.path, position.path, s.diffReview!.workspaceRoot ?? s.workingDirectory)
              && c.position.side === position.side && c.position.line === position.line),
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
    const feedbackComments: DiffComment[] = comments.map(comment => ({ type: "comment",
      content: [{ content_type: "text", text: comment.content }], position: comment.position,
      localDiffHunk: comment.localDiffHunk, localWorkspaceRoot: comment.localWorkspaceRoot }));
    const feedback = comments.length > 0
      ? formatDiffCommentsPrompt("Revise the proposed change using these review comments.", feedbackComments)
      : undefined;
    const command = buildApprovalResponseCommand(
      requestId,
      comments.length > 0 ? "reject" : "approve",
      { feedback, owner: { conversationId, turnId, messageId } },
    );
    try {
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
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
      if (!commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      get().clearDiffReview(requestId);
    } catch (error) {
      set((state) => ({
        diffReview: state.diffReview?.requestId === requestId
          ? { ...state.diffReview, status: "error", error: error instanceof Error ? error.message : "审批提交失败" }
          : state.diffReview,
      }));
    }
  },
  setPreviewArtifact: (artifact) =>
    set((s) => {
      const value = cloneArtifactState(artifact);
      return {
        ...updatePreviewWorkbench(s, s.conversationId ?? undefined, { previewArtifact: value }),
        previewArtifact: value,
      };
    }),
  setConversationPreviewArtifact: (conversationId, artifact) =>
    set((s) => {
      const targetId = String(conversationId || "").trim();
      if (!targetId) return s;
      return updatePreviewWorkbench(s, targetId, { previewArtifact: cloneArtifactState(artifact) });
    }),
  setPreviewOwnerConversationId: (conversationId) =>
    set({ previewOwnerConversationId: String(conversationId || "").trim() || null }),
  restorePreviewState: (conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const preview = previewStateForConversation(s, targetId || undefined);
      return {
        ...preview,
        previewOwnerConversationId: targetId || null,
      };
    }),
  setLivePreviewUrl: (url, conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const value = url == null ? null : String(url);
      // Background preview events populate their own conversation cache, but
      // only an active conversation or an explicit open action may take over
      // the visible preview surface.
      const ownerPatch = value && (targetId === s.conversationId || !targetId)
        ? { previewOwnerConversationId: targetId || null }
        : s.previewOwnerConversationId === targetId
          ? { previewOwnerConversationId: null }
          : {};
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { livePreviewUrl: value }),
        ...(targetId === s.conversationId || !targetId ? { livePreviewUrl: value } : {}),
        ...ownerPatch,
      };
    }),
  openLivePreview: (url, conversationId) => {
    const normalizedUrl = /^https?:\/\//i.test(url.trim()) ? url.trim() : `http://${url.trim()}`;
    const targetId = String(conversationId || get().conversationId || "").trim();
    get().setLivePreviewUrl(normalizedUrl, targetId);
    if (targetId && targetId === get().conversationId) openWebInBrowser(normalizedUrl);
  },
  setPreviewServers: (servers, conversationId) =>
    set((s) => {
      const value = servers.map((server) => ({ ...server }));
      const targetId = String(conversationId || s.conversationId || "").trim();
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewServers: value }),
        ...(targetId === s.conversationId || !targetId ? { previewServers: value } : {}),
      };
    }),
  addPreviewServer: (server, conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const current = targetId === s.conversationId
        ? s.previewServers
        : s.conversationWorkbenchStates?.[targetId]?.previewServers ?? [];
      const value = [
        ...current.filter((existing) => existing.port !== server.port),
        { ...server },
      ];
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewServers: value }),
        ...(targetId === s.conversationId || !targetId ? { previewServers: value } : {}),
      };
    }),
  removePreviewServer: (port, conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const current = targetId === s.conversationId
        ? s.previewServers
        : s.conversationWorkbenchStates?.[targetId]?.previewServers ?? [];
      const value = current.filter((existing) => existing.port !== port);
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewServers: value }),
        ...(targetId === s.conversationId || !targetId ? { previewServers: value } : {}),
      };
    }),
  setPreviewLaunchConfigs: (configs, conversationId) =>
    set((s) => {
      const value = configs.map((config) => ({ ...config }));
      const targetId = String(conversationId || s.conversationId || "").trim();
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewLaunchConfigs: value }),
        ...(targetId === s.conversationId || !targetId ? { previewLaunchConfigs: value } : {}),
      };
    }),
  setPreviewLaunchProcesses: (processes, conversationId) =>
    set((s) => {
      const value = processes.map((process) => ({
        ...process,
        stderr_tail: process.stderr_tail ? [...process.stderr_tail] : process.stderr_tail,
        output_tail: process.output_tail?.map((line) => ({ ...line })),
      }));
      const targetId = String(conversationId || s.conversationId || "").trim();
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewLaunchProcesses: value }),
        ...(targetId === s.conversationId || !targetId ? { previewLaunchProcesses: value } : {}),
      };
    }),
  upsertPreviewLaunchProcess: (process, conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const current = targetId === s.conversationId
        ? s.previewLaunchProcesses
        : s.conversationWorkbenchStates?.[targetId]?.previewLaunchProcesses ?? [];
      const value = [
        {
          ...process,
          stderr_tail: process.stderr_tail ? [...process.stderr_tail] : process.stderr_tail,
          output_tail: process.output_tail?.map((line) => ({ ...line })),
        },
        ...current.filter((existing) => existing.id !== process.id),
      ];
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewLaunchProcesses: value }),
        ...(targetId === s.conversationId || !targetId ? { previewLaunchProcesses: value } : {}),
      };
    }),
  removePreviewLaunchProcess: (id, conversationId) =>
    set((s) => {
      const targetId = String(conversationId || s.conversationId || "").trim();
      const current = targetId === s.conversationId
        ? s.previewLaunchProcesses
        : s.conversationWorkbenchStates?.[targetId]?.previewLaunchProcesses ?? [];
      const value = current.filter((process) => process.id !== id);
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewLaunchProcesses: value }),
        ...(targetId === s.conversationId || !targetId ? { previewLaunchProcesses: value } : {}),
      };
    }),
  setPreviewVerification: (verification, conversationId) =>
    set((s) => {
      const value = verification ? { ...verification } : null;
      const targetId = String(conversationId || s.conversationId || "").trim();
      return {
        ...updatePreviewWorkbench(s, targetId || undefined, { previewVerification: value }),
        ...(targetId === s.conversationId || !targetId ? { previewVerification: value } : {}),
      };
    }),
  setQuickOpenResults: (results) => set({ quickOpenResults: results, quickOpenLoading: false }),
  setQuickOpenLoading: (loading) => set({ quickOpenLoading: loading }),
  addFileChange: (change) =>
    set((s) => {
      const sequence = (s.fileChanges.at(-1)?.sequence ?? 0) + 1;
      return { fileChanges: [...s.fileChanges.slice(-99), { ...change, sequence, workspaceRoot: s.workingDirectory }] };
    }),
  bumpFileTreeVersion: () =>
    set((s) => ({ fileTreeVersion: s.fileTreeVersion + 1 })),
  requestFileTreeReveal: (path, kind = "folder") =>
    set((s) => {
      const panelSlots = ensureCodePanelSlots(s.panelSlots).map((slot) => ({ ...slot, focused: slot.kind === "editor" }));
      persistPanelSlots(panelSlots);
      return {
        appMode: "code",
        panelSlots,
        editorExplorerOpen: true,
        workspaceSearchOpen: false,
        rightPanelExpanded: false,
        settingsOpen: false,
        skillsMarketplaceOpen: false,
        fileTreeRevealRequests: [
          ...s.fileTreeRevealRequests,
          {
            id: `reveal-${Date.now().toString(36)}-${s.fileTreeRevealRequests.length}`,
            path,
            kind,
            workspaceRoot: s.workingDirectory,
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
    // A requested navigation still leaves the old conversation visible in
    // the store. Its workspace is already being replaced on the server.
    // The mounted Git surfaces retry when that navigation settles.
    if (state.pendingConversationSwitchId || state.pendingConversationCreateId) return;
    const conversationId = String(state.conversationId || "").trim();
    const workspaceRoot = String(state.workingDirectory || "").trim();
    if (!conversationId || !workspaceRoot) {
      set((s) => ({
        gitChanges: {
          ...s.gitChanges,
          isGitRepo: undefined,
          error: undefined,
          workingTree: [],
          staged: [],
          untracked: [],
          loading: false,
          workspaceRoot,
          workingTreeRequestId: undefined,
          stagedRequestId: undefined,
          comparison: undefined,
        },
      }));
      return;
    }
    const workingTreeRequestId = createClientCommandId();
    const stagedRequestId = createClientCommandId();
    const hideWhitespace = readDiffViewPreference("hideWhitespace");
    set((s) => ({
      gitChanges: {
        ...s.gitChanges,
        error: undefined,
        loading: true,
        workspaceRoot,
        workingTreeRequestId,
        stagedRequestId,
        ...(s.gitChanges.comparison && (s.gitChanges.comparison.source === "unstaged" || s.gitChanges.comparison.source === "staged")
          ? { comparison: { ...s.gitChanges.comparison, loading: true, error: undefined,
              requestId: s.gitChanges.comparison.source === "staged" ? stagedRequestId : workingTreeRequestId } } : {}),
      },
    }));
    const workingTreeSent = sendClientCommand({
      type: "diff.git_working_tree",
      source: "unstaged",
      hide_whitespace: hideWhitespace,
      conversation_id: conversationId,
      workspace: workspaceRoot,
      request_id: workingTreeRequestId,
      client_command_id: workingTreeRequestId,
    }, { silent: true });
    const stagedSent = sendClientCommand({
      type: "diff.git_staged",
      hide_whitespace: hideWhitespace,
      conversation_id: conversationId,
      workspace: workspaceRoot,
      request_id: stagedRequestId,
      client_command_id: stagedRequestId,
    }, { silent: true });
    if (!workingTreeSent || !stagedSent) get().setGitChangesLoading(false);
    const comparison = get().gitChanges.comparison;
    if (comparison && comparison.source !== "unstaged" && comparison.source !== "staged") get().requestGitComparison(comparison);
  },
  requestGitComparison: (selection) => {
    const state = get();
    if (state.pendingConversationSwitchId || state.pendingConversationCreateId) return;
    const conversationId = state.conversationId;
    const workspaceRoot = state.workingDirectory;
    if (!conversationId || !workspaceRoot) return;
    const requestId = createClientCommandId();
    const current = state.gitChanges.comparison;
    const same = current?.source === selection.source && current.baseBranch === selection.baseBranch && current.commitSha === selection.commitSha;
    state.setGitChanges({ comparison: {
      ...(same ? current : { files: [] }),
      source: selection.source, baseBranch: selection.baseBranch, commitSha: selection.commitSha,
      loading: true, error: undefined, requestId,
    }, ...(selection.source === "unstaged" ? { workingTreeRequestId: requestId }
      : selection.source === "staged" ? { stagedRequestId: requestId } : {}) });
    const sent = sendClientCommand({
      type: selection.source === "staged" ? "diff.git_staged" : "diff.git_working_tree",
      source: selection.source,
      base_branch: selection.baseBranch, commit_sha: selection.commitSha,
      hide_whitespace: readDiffViewPreference("hideWhitespace"),
      conversation_id: conversationId, workspace: workspaceRoot,
      request_id: requestId, client_command_id: requestId,
    }, { silent: true });
    if (!sent) get().setGitChanges({ comparison: { ...get().gitChanges.comparison!, loading: false } });
  },
  });
};
