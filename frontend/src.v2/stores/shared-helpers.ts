import type {
  AppStore,
  ChatMessage,
  ContentBlock,
  EditorTab,
  PanelSlot,
  PendingToolCallResume,
  ThinkingContentBlock,
  UISlice,
  WorkspaceSlice,
  ResolvedTheme,
} from "./types";
import { clamp } from "../lib/clamp";
import { clampTextScale } from "../lib/text-scale";
import { safeJsonParse } from "../lib/safe-parse";
import { getContentBlocks, getThinkingFromMessage, getToolCallsFromMessage } from "../lib/content-blocks";
import {
  isTerminalToolCallStatus,
  type ToolCallRecord,
} from "../lib/tool-call-reducer";
import { DEFAULT_SHORTCUT_BINDINGS, SHORTCUT_DEFINITIONS, type ShortcutBindings } from "../lib/keyboard-shortcuts";
import {
  normalizeWorkspacePath,
  normalizeWorkspaceRoot,
  workspaceFilePathComparisonKey,
  workspacePathWithin,
  workspacePathsEqual,
} from "../lib/workspace-path";

// ── LocalStorage keys ──────────────────────────────────────────────

export const LS = {
  theme: "minicode.theme",
  textScale: "minicode.textScale",
  codeTextScale: "minicode.codeTextScale",
  reducedMotion: "minicode.reducedMotion",
  viewMode: "minicode.activity.viewMode",
  sendShortcut: "minicode.composer.sendShortcut",
  followUpBehavior: "minicode.composer.followUpBehavior",
  shortcutBindings: "minicode.keyboard.bindings",
  layout: {
    leftWidth: "minicode.layout.left-width",
    rightWidth: "minicode.layout.right-width",
    rightOpen: "minicode.layout.right-open",
    dockHeight: "minicode.layout.dock-height",
    dockCollapsed: "minicode.layout.dock-collapsed",
    dockTab: "minicode.layout.dock-tab",
    panelSlots: "minicode.layout.panel-slots",
  },
  permissionMode: "minicode.composer.permissionMode",
  agentMode: "minicode.composer.agentMode",
  remoteImagePolicy: "minicode.markdown.remoteImagePolicy",
  conversation: {
    activeId: "minicode.conversation.active-id",
  },
  editorTabs: "minicode.editor.tabs",
};

export const DEFAULT_WORKSPACE_KEY = "__default__";
export const LEFT_SIDEBAR_DEFAULT_WIDTH = 320;
export const LEFT_SIDEBAR_MIN_WIDTH = 272;
export const LEFT_SIDEBAR_MAX_WIDTH = 400;
export const RIGHT_SIDEBAR_DEFAULT_WIDTH = 380;
export const RIGHT_SIDEBAR_MAX = 1040;
export const COMPACT_WORKBENCH_MAX_WIDTH = 1599;

export const isCompactWorkbenchViewport = () => (
  typeof window !== "undefined" && window.innerWidth <= COMPACT_WORKBENCH_MAX_WIDTH
);

// ── LocalStorage read/write ────────────────────────────────────────

export const readLS = (key: string): string | null => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};

export const writeLS = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* noop */
  }
};

// ── Theme & text scale ─────────────────────────────────────────────

export const resolveTheme = (mode: UISlice["themeMode"]): ResolvedTheme => {
  let resolved: ResolvedTheme = "dark";
  if (mode === "light") resolved = "light";
  else if (mode === "dark") resolved = "dark";
  else {
    const mediaQuery = typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: light)")
      : null;
    resolved = mediaQuery?.matches ? "light" : "dark";
  }
  return resolved;
};

export const applyTheme = (mode: UISlice["themeMode"]): ResolvedTheme => {
  const resolved = resolveTheme(mode);
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", resolved);
  }
  return resolved;
};

export const applyTextScale = (s: number) => {
  document.documentElement.style.setProperty("--app-text-scale", String(s));
};

export const applyCodeTextScale = (s: number) => {
  document.documentElement.style.setProperty("--code-text-scale", String(s));
};

export const applyReducedMotion = (reduced: boolean) => {
  document.documentElement.setAttribute("data-reduced-motion", reduced ? "true" : "false");
};

export const initialViewMode = (): UISlice["viewMode"] => {
  const stored = readLS(LS.viewMode);
  return stored === "summary" || stored === "verbose" ? stored : "normal";
};

export const initialTheme = (): UISlice["themeMode"] => {
  const v = readLS(LS.theme);
  if (v === "dark" || v === "light" || v === "system") return v;
  return "system";
};

export const initialTextScale = (): number => {
  const v = parseFloat(readLS(LS.textScale) ?? "");
  return clampTextScale(Number.isFinite(v) ? v : 1.0);
};

// ── Panel helpers ──────────────────────────────────────────────────

export const normalizePanelSlots = (slots: PanelSlot[]): PanelSlot[] => {
  const visibleSlots = slots
    .filter((slot) => slot.kind !== "inspector")
    .map((slot) => slot.kind === "plan" ? { ...slot, kind: "tasks" as const, label: slot.label ?? "上下文" } : slot);
  if (visibleSlots.length === 0) return [{ id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true }];
  const hasFocused = visibleSlots.some((slot) => slot.focused);
  return visibleSlots.map((slot, index) => ({
    ...slot,
    size: Number.isFinite(slot.size) && slot.size! > 0 ? slot.size : 1,
    focused: hasFocused ? Boolean(slot.focused) : index === 0,
  }));
};

export const defaultPanelSlots = (): PanelSlot[] => [
  { id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: true, maximized: false },
];

export const ensureCodePanelSlots = (slots: PanelSlot[]): PanelSlot[] => {
  const withoutMaximized = slots.map((slot) => ({ ...slot, maximized: false }));
  const codeSlots = withoutMaximized.filter((slot) => slot.kind === "chat" || slot.kind === "editor");
  const focusedSlotId = codeSlots.find((slot) => slot.focused)?.id;
  const chatSlot: PanelSlot = codeSlots.find((slot) => slot.kind === "chat") ?? {
    id: "main-chat",
    kind: "chat",
    label: "Chat",
    size: 1,
    focused: !focusedSlotId,
  };
  const editorSlot: PanelSlot = codeSlots.find((slot) => slot.kind === "editor") ?? {
    id: "main-editor",
    kind: "editor",
    label: "File",
    size: 1,
    focused: false,
  };
  const next: PanelSlot[] = [chatSlot, editorSlot];
  return normalizePanelSlots(next.map((slot) => ({
    ...slot,
    label: slot.kind === "chat" ? (slot.label ?? "Chat") : (slot.label ?? "File"),
    focused: focusedSlotId ? slot.id === focusedSlotId : Boolean(slot.focused),
  })));
};

export const persistPanelSlots = (slots: PanelSlot[]) => {
  writeLS(LS.layout.panelSlots, JSON.stringify(normalizePanelSlots(slots)));
};

// ── Layout initialization ──────────────────────────────────────────

export const loadInitialLayout = () => {
  const left = parseInt(readLS(LS.layout.leftWidth) ?? "", 10);
  const right = parseInt(readLS(LS.layout.rightWidth) ?? "", 10);
  const dock = parseInt(readLS(LS.layout.dockHeight) ?? "", 10);
  const rightOpenRaw = readLS(LS.layout.rightOpen);
  const collapsedRaw = readLS(LS.layout.dockCollapsed);
  const rightOpen = rightOpenRaw == null ? false : rightOpenRaw === "1";
  const collapsed = collapsedRaw == null ? true : collapsedRaw === "1";
  const tab = (readLS(LS.layout.dockTab) ?? "terminal") as WorkspaceSlice["activeBottomTab"];
  let slots: PanelSlot[] = defaultPanelSlots();
  try {
    const raw = readLS(LS.layout.panelSlots);
    if (raw) {
      const rawSlots = safeJsonParse<unknown>(raw, []);
      const parsed = (Array.isArray(rawSlots) ? rawSlots : [])
        .filter((slot): slot is Record<string, unknown> => Boolean(slot) && typeof slot === "object")
        .map((slot) =>
          slot.kind === "subagent"
            ? { ...slot, kind: "subagents", label: slot.label ?? "协作" }
            : slot.kind === "plan"
              ? { ...slot, kind: "tasks", label: slot.label ?? "上下文" }
              : slot,
        ) as unknown as PanelSlot[];
      if (Array.isArray(parsed) && parsed.length > 0) slots = parsed;
    }
  } catch {
    /* keep default */
  }
  const leftSidebarWidth = Number.isFinite(left)
    ? left <= 0
      ? 0
      : clamp(LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_MAX_WIDTH, left)
    : LEFT_SIDEBAR_DEFAULT_WIDTH;
  return {
    leftSidebarWidth,
    rightSidebarWidth: clamp(320, RIGHT_SIDEBAR_MAX, Number.isFinite(right) ? right : RIGHT_SIDEBAR_DEFAULT_WIDTH),
    rightPanelOpen: rightOpen,
    dockHeight: clamp(180, 520, Number.isFinite(dock) ? dock : 240),
    dockCollapsed: collapsed,
    activeBottomTab: tab,
    panelSlots: normalizePanelSlots(slots),
  };
};

// ── Right sidebar helpers ──────────────────────────────────────────

export const preferredRightSidebarWidth = (
  tab: UISlice["rightStackTab"],
  currentWidth: number,
): number => {
  const viewportWidth = typeof window === "undefined" ? 1440 : window.innerWidth;
  if (viewportWidth < 1180) return currentWidth;
  const preferred =
    tab === "preview" ? 440 :
      tab === "browser" ? 680 :
        tab === "subagents" ? RIGHT_SIDEBAR_DEFAULT_WIDTH :
            tab === "artifacts" ? RIGHT_SIDEBAR_DEFAULT_WIDTH :
            0;
  if (!preferred || currentWidth >= preferred) return currentWidth;
  return clamp(320, RIGHT_SIDEBAR_MAX, preferred);
};

export const automaticRightPanelState = (
  state: AppStore,
  tab: UISlice["rightStackTab"],
): Partial<Pick<AppStore, "rightStackTab" | "rightPanelOpen" | "rightSidebarWidth" | "dockCollapsed">> => {
  if (state.rightStackTabLocked) return {};
  if (
    state.rightPanelOpen &&
    state.rightStackTab !== "preview" &&
    state.rightStackTab !== "tasks"
  ) {
    return {};
  }
  const rightSidebarWidth = preferredRightSidebarWidth(tab, state.rightSidebarWidth);
  if (rightSidebarWidth !== state.rightSidebarWidth) {
    writeLS(LS.layout.rightWidth, String(rightSidebarWidth));
  }
  writeLS(LS.layout.rightOpen, "1");
  return {
    rightStackTab: tab,
    rightPanelOpen: true,
    rightSidebarWidth,
    dockCollapsed: state.dockCollapsed,
  };
};

// ── Editor tab helpers ─────────────────────────────────────────────

const editorWorkspaceKey = (workspace: string | null | undefined): string => {
  const value = normalizeWorkspaceRoot(workspace);
  return value || DEFAULT_WORKSPACE_KEY;
};

const editorTabsStorageKey = (workspace: string | null | undefined): string =>
  `${LS.editorTabs}:${editorWorkspaceKey(workspace)}`;

const legacyEditorTabsStorageKey = (workspace: string | null | undefined): string => {
  const value = (workspace || "").trim() || DEFAULT_WORKSPACE_KEY;
  return `${LS.editorTabs}:${value}`;
};

export const normalizeEditorPath = (path: string, workingDirectory = ""): string => {
  const raw = normalizeWorkspacePath(path);
  if (!raw) return raw;
  const root = normalizeWorkspacePath(workingDirectory);
  if (root && workspacePathWithin(raw, root)) {
    if (workspacePathsEqual(raw, root)) return ".";
    return raw.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return raw.replace(/\/+/g, "/").replace(/^\.\/+/, "");
};

export const editorPathComparisonKey = (path: string, workingDirectory = ""): string => {
  const normalized = normalizeEditorPath(path, workingDirectory);
  // Tabs are stored workspace-relative, so the workspace supplies Windows
  // case-insensitivity that cannot be inferred from the relative spelling.
  return workspaceFilePathComparisonKey(normalized, workingDirectory);
};

export const editorPathsEqual = (
  left: string | null | undefined,
  right: string | null | undefined,
  workingDirectory = "",
): boolean => {
  if (!left || !right) return false;
  return editorPathComparisonKey(left, workingDirectory) === editorPathComparisonKey(right, workingDirectory);
};

const blankEditorTab = (path: string): EditorTab => ({
  path,
  content: "",
  original: "",
  loading: true,
  error: null,
  externalChanged: false,
  largeFile: false,
  loadWarning: null,
  sizeBytes: undefined,
  readOnly: false,
});

export const loadPersistedEditorTabs = (workspace?: string | null): EditorTab[] => {
  try {
    const storageKey = editorTabsStorageKey(workspace);
    const legacyStorageKey = legacyEditorTabsStorageKey(workspace);
    const canonicalRaw = readLS(storageKey);
    const scopedRaw = canonicalRaw
      ?? (legacyStorageKey !== storageKey ? readLS(legacyStorageKey) : null);
    const raw = scopedRaw ?? (editorWorkspaceKey(workspace) === DEFAULT_WORKSPACE_KEY ? readLS(LS.editorTabs) : null);
    if (!raw) return [];
    const parsed = safeJsonParse<unknown>(raw, []);
    if (!Array.isArray(parsed)) return [];
    const paths = parsed.filter((path): path is string => typeof path === "string");
    const seen = new Set<string>();
    const normalizedPaths = paths
      .slice(0, 20)
      .map((path) => normalizeEditorPath(path, workspace ?? ""))
      .filter((path) => {
        if (!path) return false;
        const key = editorPathComparisonKey(path, workspace ?? "");
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    const normalizedRaw = JSON.stringify(normalizedPaths);
    if (canonicalRaw == null || normalizedRaw !== JSON.stringify(paths.slice(0, 20))) {
      writeLS(storageKey, normalizedRaw);
    }
    return normalizedPaths.map(blankEditorTab);
  } catch {
    return [];
  }
};

export const persistEditorTabs = (tabs: EditorTab[], workspace?: string | null) => {
  const seen = new Set<string>();
  const paths = tabs.flatMap((tab) => {
    const path = normalizeEditorPath(tab.path, workspace ?? "");
    const key = editorPathComparisonKey(path, workspace ?? "");
    if (!path || seen.has(key)) return [];
    seen.add(key);
    return [path];
  });
  writeLS(editorTabsStorageKey(workspace), JSON.stringify(paths));
};

export const editorStateForWorkspace = (workspace: string | null | undefined) => {
  const editorTabs = loadPersistedEditorTabs(workspace);
  return {
    editorTabs,
    activeTabPath: editorTabs[0]?.path ?? null,
    activeEditorPath: null as string | null,
    editorOpenRequests: [],
  };
};

// ── Message ID helpers ─────────────────────────────────────────────

let _msgIdCounter = 0;
export const uniqueMessageId = (suffix = ""): string => {
  const base = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
    : `${Date.now().toString(36)}${(++_msgIdCounter).toString(36)}`;
  return suffix ? `m-${base}-${suffix}` : `m-${base}`;
};

export const newConversationId = (): string =>
  `conv_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;

// ── Message helpers ────────────────────────────────────────────────

const hasVisibleContentBlocks = (message: ChatMessage): boolean =>
  getContentBlocks(message).some((block) => {
    if (block.type === "text" || block.type === "thinking" || block.type === "process") {
      return Boolean(block.content?.trim());
    }
    if (block.type === "progress") {
      return Boolean((block.label || block.summary || block.message || "").trim());
    }
    return block.type === "tool_call";
  });

export const isStructurallyEmptyAssistantMessage = (message: ChatMessage) =>
  message.role === "assistant"
  && message.isStreaming
  && !message.content
  && !getThinkingFromMessage(message)
  && getToolCallsFromMessage(message).length === 0
  && !hasVisibleContentBlocks(message)
  && message.artifacts.length === 0;

export const cacheMessagesForConversation = (
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

export const updateMessagesForConversation = (
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

export const findLastStreamingIndex = (messages: AppStore["messages"]): number => {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.isStreaming) return i;
  }
  return -1;
};

export const computeToolCallCount = (messages: ChatMessage[]): number =>
  messages.reduce((sum, m) => sum + getToolCallsFromMessage(m).length, 0);

export const progressConversationKey = (conversationId?: string): string => conversationId?.trim() || "__unowned__";

export const conversationWorkspacePath = (conversation?: AppStore["conversations"][number] | null): string =>
  conversation?.worktreePath || conversation?.workspaceRoot || "";

// ── Thinking metadata helpers ──────────────────────────────────────

const THINKING_METADATA_KEYS = [
  "source",
  "visibility",
  "phase",
  "item_id",
  "content_index",
] as const;

export const normalizeThinkingMetadata = (
  metadata?: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
): Partial<Omit<ThinkingContentBlock, "type" | "content">> => {
  if (!metadata) return {};
  const normalized: Partial<Omit<ThinkingContentBlock, "type" | "content">> = {};
  if (metadata.source !== undefined) normalized.source = metadata.source;
  if (metadata.visibility !== undefined) normalized.visibility = metadata.visibility;
  if (metadata.phase !== undefined) normalized.phase = metadata.phase;
  if (metadata.item_id !== undefined) normalized.item_id = metadata.item_id;
  if (metadata.content_index !== undefined) normalized.content_index = metadata.content_index;
  if (metadata.lifecycle !== undefined) normalized.lifecycle = metadata.lifecycle;
  return normalized;
};

export const thinkingMetadataMatches = (
  block: ThinkingContentBlock,
  metadata: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
): boolean =>
  THINKING_METADATA_KEYS.every((key) => block[key] === metadata[key]);

// ── Tool call resume helpers ───────────────────────────────────────

const resumeToolStatus = (status: PendingToolCallResume["status"]): ToolCallRecord["status"] => {
  const normalized = String(status || "running").toLowerCase();
  if (normalized === "completed") return "success";
  if (normalized === "error") return "failed";
  if (normalized === "waiting_approval") return "pending";
  if (["pending", "running", "success", "failed", "blocked", "partial", "timeout", "cancelled"].includes(normalized)) {
    return normalized as ToolCallRecord["status"];
  }
  return "running";
};

export const initialResolvedTheme = (): ResolvedTheme => resolveTheme(initialTheme());

export const initialCodeTextScale = (): number => {
  const value = parseFloat(readLS(LS.codeTextScale) ?? "");
  return clamp(0.88, 1.2, Number.isFinite(value) ? value : 1);
};

export const initialReducedMotion = (): boolean => readLS(LS.reducedMotion) === "1";

export const initialSendShortcut = (): UISlice["sendShortcut"] => (
  readLS(LS.sendShortcut) === "mod-enter" ? "mod-enter" : "enter"
);

export const initialFollowUpBehavior = (): UISlice["followUpBehavior"] => (
  readLS(LS.followUpBehavior) === "steer" ? "steer" : "queue"
);

export const initialShortcutBindings = (): ShortcutBindings => {
  try {
    const raw = safeJsonParse<unknown>(readLS(LS.shortcutBindings) ?? "{}", {});
    const parsed = raw && typeof raw === "object" && !Array.isArray(raw)
      ? raw as Record<string, unknown>
      : {};
    const bindings = { ...DEFAULT_SHORTCUT_BINDINGS };
    for (const definition of SHORTCUT_DEFINITIONS) {
      if (typeof parsed[definition.id] === "string") bindings[definition.id] = parsed[definition.id] as string;
    }
    return bindings;
  } catch {
    return { ...DEFAULT_SHORTCUT_BINDINGS };
  }
};

const pendingToolCallToRecord = (
  tc: PendingToolCallResume,
  fallbackStartedAt?: number,
): ToolCallRecord => ({
  id: tc.id,
  name: tc.name,
  args: tc.args ?? {},
  status: resumeToolStatus(tc.status),
  transition: tc.transition,
  startedAt: tc.startedAt ?? tc.started_at ?? fallbackStartedAt ?? Date.now(),
  finishedAt: tc.finishedAt ?? tc.finished_at,
  durationMs: tc.durationMs ?? tc.duration_ms,
  displayHint: tc.displayHint ?? tc.display_hint,
  inputSummary: tc.inputSummary ?? tc.input_summary,
  turnId: tc.turnId ?? tc.turn_id,
  iterationId: tc.iterationId ?? tc.iteration_id,
  phase: tc.phase,
  waitingOn: tc.waitingOn ?? tc.waiting_on,
  blockingReason: tc.blockingReason ?? tc.blocking_reason,
  outputPreview: tc.outputPreview,
  stdoutPreview: tc.stdoutPreview,
  stderrPreview: tc.stderrPreview,
});

export const mergeResumeToolCalls = (
  blocks: ContentBlock[],
  pendingToolCalls?: PendingToolCallResume[],
  preserveText = false,
): ContentBlock[] => {
  const next = preserveText
    ? blocks.slice()
    : blocks.filter((block) => block.type !== "text" && block.type !== "thinking");
  for (const pending of pendingToolCalls ?? []) {
    const candidate = pendingToolCallToRecord(pending);
    const existingIndex = next.findIndex((block) =>
      block.type === "tool_call" && sameToolCallRecord(block.record, candidate)
    );
    if (existingIndex >= 0) {
      const existing = next[existingIndex];
      if (existing.type === "tool_call") {
        const record = pendingToolCallToRecord(pending, existing.record.startedAt);
        const definedRecord = Object.fromEntries(
          Object.entries(record).filter(([, value]) => value !== undefined),
        ) as Partial<ToolCallRecord>;
        next[existingIndex] = {
          type: "tool_call",
          record: {
            ...existing.record,
            ...definedRecord,
            // Reconnect data may enrich a completed item, but cannot move it
            // back into the active lifecycle.
            status: isTerminalToolCallStatus(existing.record.status)
              ? existing.record.status
              : record.status,
            args: pending.args ?? existing.record.args,
            name: pending.name || existing.record.name,
          },
        };
      }
      continue;
    }
    next.push({ type: "tool_call", record: candidate });
  }
  return next;
};

/** Single source of truth for resetting conversation-scoped state on switch/create. */
export function conversationResetPayload(): Record<string, unknown> {
  return {
    diffReview: null,
    previewArtifact: null,
    livePreviewUrl: null,
    terminalSessions: [],
    activeTerminalSessionId: null,
    rightStackTab: "tasks",
    rightPanelOpen: false,
    rightStackTabLocked: false,
    allowedRemoteImageDomains: [],
    plan: null,
    todos: [],
    subagents: [],
    agentProgress: [],
    activeGoal: null,
    budgetBuckets: [],
    totalBudgetPercent: 0,
    contextUsage: null,
    lastUsage: null,
    usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 },
    inspectorEntries: [],
    inspectorFocus: null,
  };
}

export const promptTargetsConversation = (
  prompt: { conversationId?: string } | null | undefined,
  conversationId: string | null | undefined,
): boolean => {
  const owner = prompt?.conversationId?.trim();
  const target = conversationId?.trim();
  if (!target) return false;
  return !owner || owner === target;
};

export const visibleDiffReviewForConversation = (
  conversationId: string | null | undefined,
  pending: AppStore["pendingDiffReview"],
  queue: AppStore["diffReviewQueue"],
) => [pending, ...queue].find((item) =>
  promptTargetsConversation(item, conversationId),
)?.reviewState ?? null;

export const removeConversationOwnedPrompts = <T extends { conversationId?: string }>(
  pending: T | null,
  queue: T[],
  conversationId: string,
): { pending: T | null; queue: T[] } => {
  const retained = [pending, ...queue].filter((item): item is T => (
    Boolean(item) && item?.conversationId?.trim() !== conversationId
  ));
  const [next, ...rest] = retained;
  return { pending: next ?? null, queue: rest };
};

const sameToolCallRecord = (
  left: ToolCallRecord,
  right: ToolCallRecord,
): boolean => {
  if (left.id !== right.id) return false;
  if (left.turnId && right.turnId) return left.turnId === right.turnId;
  if (left.iterationId && right.iterationId) return left.iterationId === right.iterationId;
  if (left.stepId && right.stepId) return left.stepId === right.stepId;
  return true;
};
