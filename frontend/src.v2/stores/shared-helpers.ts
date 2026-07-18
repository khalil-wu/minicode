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
} from "./types";
import { clamp } from "../lib/clamp";
import { clampTextScale } from "../lib/text-scale";
import { getContentBlocks, getThinkingFromMessage, getToolCallsFromMessage } from "../lib/content-blocks";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

// ── LocalStorage keys ──────────────────────────────────────────────

export const LS = {
  theme: "minicode.theme",
  textScale: "minicode.textScale",
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
  promptPersona: "minicode.composer.promptPersona",
  remoteImagePolicy: "minicode.markdown.remoteImagePolicy",
  conversation: {
    activeId: "minicode.conversation.active-id",
  },
  editorTabs: "minicode.editor.tabs",
};

export const DEFAULT_WORKSPACE_KEY = "__default__";
export const LEFT_SIDEBAR_MIN_WIDTH = 252;
export const LEFT_SIDEBAR_MAX_WIDTH = 380;
export const RIGHT_SIDEBAR_MAX = 1040;

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

export const applyTheme = (mode: UISlice["themeMode"]) => {
  let resolved: "dark" | "light" = "dark";
  if (mode === "light") resolved = "light";
  else if (mode === "dark") resolved = "dark";
  else
    resolved = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", resolved);
};

export const applyTextScale = (s: number) => {
  document.documentElement.style.setProperty("--app-text-scale", String(s));
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
  const visibleSlots = slots.filter((slot) => slot.kind !== "inspector");
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
      const parsed = (JSON.parse(raw) as (PanelSlot | (Omit<PanelSlot, "kind"> & { kind: "subagent" }))[])
        .map((slot) =>
          slot.kind === "subagent"
            ? { ...slot, kind: "subagents", label: slot.label ?? "协作" }
            : slot,
        ) as PanelSlot[];
      if (Array.isArray(parsed) && parsed.length > 0) slots = parsed;
    }
  } catch {
    /* keep default */
  }
  const leftSidebarWidth = Number.isFinite(left)
    ? left <= 0
      ? 0
      : clamp(LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_MAX_WIDTH, left)
    : 320;
  return {
    leftSidebarWidth,
    rightSidebarWidth: clamp(320, RIGHT_SIDEBAR_MAX, Number.isFinite(right) ? right : 440),
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
        tab === "terminal" ? 720 :
          tab === "subagents" ? 360 :
            tab === "artifacts" ? 360 :
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
    state.rightStackTab !== "tasks" &&
    !(state.rightStackTab === "plan" && tab === "tasks")
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
    dockCollapsed: tab === "terminal" ? true : state.dockCollapsed,
  };
};

// ── Editor tab helpers ─────────────────────────────────────────────

const editorWorkspaceKey = (workspace: string | null | undefined): string => {
  const value = (workspace || "").trim();
  return value || DEFAULT_WORKSPACE_KEY;
};

const editorTabsStorageKey = (workspace: string | null | undefined): string =>
  `${LS.editorTabs}:${editorWorkspaceKey(workspace)}`;

export const normalizeEditorPath = (path: string, workingDirectory = ""): string => {
  const raw = String(path || "").trim().replace(/\\/g, "/");
  if (!raw) return raw;
  const root = workingDirectory.replace(/\\/g, "/").replace(/\/+$/, "");
  const normalizedRoot = root.toLowerCase();
  const normalizedRaw = raw.toLowerCase();
  if (normalizedRoot && (normalizedRaw === normalizedRoot || normalizedRaw.startsWith(`${normalizedRoot}/`))) {
    return raw.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return raw.replace(/\/+/g, "/").replace(/^\.\/+/, "");
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
});

export const loadPersistedEditorTabs = (workspace?: string | null): EditorTab[] => {
  try {
    const storageKey = editorTabsStorageKey(workspace);
    const scopedRaw = readLS(storageKey);
    const raw = scopedRaw ?? (editorWorkspaceKey(workspace) === DEFAULT_WORKSPACE_KEY ? readLS(LS.editorTabs) : null);
    if (!raw) return [];
    const paths = JSON.parse(raw) as string[];
    if (!Array.isArray(paths)) return [];
    const normalizedPaths = paths
      .slice(0, 20)
      .map((path) => normalizeEditorPath(path, workspace ?? ""))
      .filter(Boolean);
    const normalizedRaw = JSON.stringify(normalizedPaths);
    if (normalizedRaw !== JSON.stringify(paths.slice(0, 20))) writeLS(storageKey, normalizedRaw);
    return normalizedPaths.map(blankEditorTab);
  } catch {
    return [];
  }
};

export const persistEditorTabs = (tabs: EditorTab[], workspace?: string | null) => {
  writeLS(editorTabsStorageKey(workspace), JSON.stringify(tabs.map((t) => t.path)));
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

export const progressConversationKey = (conversationId?: string): string => conversationId || "__active__";

export const conversationWorkspacePath = (conversation?: AppStore["conversations"][number] | null): string =>
  conversation?.worktreePath || conversation?.workspaceRoot || "";

// ── Thinking metadata helpers ──────────────────────────────────────

const THINKING_METADATA_KEYS = ["source", "visibility", "is_raw_provider_reasoning", "provider_reasoning_type"] as const;

export const normalizeThinkingMetadata = (
  metadata?: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
): Partial<Omit<ThinkingContentBlock, "type" | "content">> => {
  if (!metadata) return {};
  const normalized: Partial<Omit<ThinkingContentBlock, "type" | "content">> = {};
  if (metadata.source !== undefined) normalized.source = metadata.source;
  if (metadata.visibility !== undefined) normalized.visibility = metadata.visibility;
  if (metadata.is_raw_provider_reasoning !== undefined) {
    normalized.is_raw_provider_reasoning = metadata.is_raw_provider_reasoning;
  }
  if (metadata.provider_reasoning_type !== undefined) {
    normalized.provider_reasoning_type = metadata.provider_reasoning_type;
  }
  return normalized;
};

export const thinkingMetadataMatches = (
  block: ThinkingContentBlock,
  metadata: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
): boolean =>
  THINKING_METADATA_KEYS.every((key) => block[key] === metadata[key]);

// ── Tool call resume helpers ───────────────────────────────────────

const pendingToolCallToRecord = (tc: PendingToolCallResume) => ({
  id: tc.id,
  name: tc.name,
  args: tc.args ?? {},
  status: tc.status === "pending" ? "pending" as const : "running" as const,
  startedAt: tc.startedAt ?? tc.started_at ?? Date.now(),
  displayHint: tc.displayHint ?? tc.display_hint,
  inputSummary: tc.inputSummary ?? tc.input_summary,
  turnId: tc.turnId ?? tc.turn_id,
  iterationId: tc.iterationId ?? tc.iteration_id,
  phase: tc.phase,
});

export const mergeResumeToolCalls = (
  blocks: ContentBlock[],
  pendingToolCalls?: PendingToolCallResume[],
): ContentBlock[] => {
  const next = blocks.filter((block) => block.type !== "text");
  for (const pending of pendingToolCalls ?? []) {
    const record = pendingToolCallToRecord(pending);
    const existingIndex = next.findIndex((block) =>
      block.type === "tool_call" && sameToolCallRecord(block.record, record)
    );
    if (existingIndex >= 0) {
      const existing = next[existingIndex];
      if (existing.type === "tool_call") {
        next[existingIndex] = {
          type: "tool_call",
          record: {
            ...existing.record,
            ...record,
          },
        };
      }
      continue;
    }
    next.push({ type: "tool_call", record });
  }
  return next;
};

/** Single source of truth for resetting conversation-scoped state on switch/create. */
export function conversationResetPayload(): Record<string, unknown> {
  return {
    pendingApproval: null,
    approvalQueue: [],
    pendingDiffReview: null,
    diffReview: null,
    previewArtifact: null,
    livePreviewUrl: null,
    activeTerminalSessionId: null,
    rightStackTab: "tasks",
    rightPanelOpen: false,
    rightStackTabLocked: false,
    allowedRemoteImageDomains: [],
    pendingAskUser: null,
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
  };
}

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
