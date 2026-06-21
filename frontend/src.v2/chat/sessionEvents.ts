import { useAppStore } from "../stores";
import type {
  ConversationListEvent,
  ConversationRecordPayload,
  ConversationSummaryPayload,
  ConversationSwitchedEvent,
  GoalInfo,
  GoalUpdatedEvent,
  LlmModelUpdatedEvent,
  RuntimeSessionSnapshot,
  ServerEvent,
  SessionRestoredEvent,
  SessionSyncedEvent,
} from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { clearStreamingState } from "./streamingState";
import type {
  AgentProgressEntry,
  ChatMessage,
  ConversationAgentState,
  PlanState,
  SubagentState,
  TodoItem,
} from "../stores/types";
import { toConversationGoal } from "../stores/types";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { mergeCapabilities } from "../protocol/capabilities";
import { conversationResetPayload } from "../stores/shared-helpers";
import type { ConversationMeta } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";

type ConversationSummary = ConversationSummaryPayload;
type ConversationPayload = ConversationRecordPayload;
const UI_AGENT_STATE_KEY = "ui_agent_state";

const maybeString = (value: string | null | undefined): string | undefined =>
  typeof value === "string" && value ? value : undefined;

const stringValue = (value: string | null | undefined): string =>
  maybeString(value) ?? "";

const toConversationMeta = (conversation: ConversationSummary) => ({
  id: conversation.id,
  title: maybeString(conversation.title) ?? "Untitled",
  updatedAt: maybeString(conversation.updated_at) ?? new Date().toISOString(),
  archived: conversation.archived,
  workspaceRoot: maybeString(conversation.workspace_root),
  gitBranch: maybeString(conversation.git_branch),
  worktreePath: maybeString(conversation.worktree_path),
  gitIsolated: conversation.git_isolated,
  goal: toConversationGoal(conversation.goal),
});

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value));

const PLAN_STATUSES = new Set<PlanState["status"]>(["draft", "accepted", "executing", "completed", "cancelled"]);
const PLAN_STEP_STATUSES = new Set<PlanState["steps"][number]["status"]>(["pending", "running", "done", "skipped", "failed"]);
const TODO_STATUSES = new Set<TodoItem["status"]>(["pending", "in_progress", "completed", "blocked"]);
const SUBAGENT_STATUSES = new Set<SubagentState["status"]>(["running", "done", "error"]);
const PROGRESS_STAGES = new Set<AgentProgressEntry["stage"]>(["status", "planning", "tool", "approval", "verification", "final"]);
const PROGRESS_PHASES = new Set<NonNullable<AgentProgressEntry["phase"]>>([
  "orienting",
  "planning",
  "model",
  "tool",
  "approval",
  "verify",
  "final",
  "recover",
  "status",
  "iteration",
]);
const PROGRESS_STATUSES = new Set<AgentProgressEntry["status"]>(["running", "completed", "failed", "info"]);

const normalizePlanFromSnapshot = (value: unknown): PlanState | null => {
  if (!isRecord(value) || !Array.isArray(value.steps)) return null;
  const steps: PlanState["steps"] = value.steps
    .filter(isRecord)
    .map((step, index) => {
      const rawStatus = String(step.status ?? "pending") as PlanState["steps"][number]["status"];
      return {
        id: String(step.id ?? `step-${index}`),
        title: String(step.title ?? `Step ${index + 1}`),
        detail: typeof step.detail === "string" ? step.detail : undefined,
        status: PLAN_STEP_STATUSES.has(rawStatus) ? rawStatus : "pending",
      };
    });
  const rawStatus = String(value.status ?? "executing") as PlanState["status"];
  const currentStep = typeof value.currentStep === "number"
    ? value.currentStep
    : typeof value.current_step === "number"
      ? value.current_step
      : 0;
  return {
    planId: String(value.planId ?? value.plan_id ?? "plan"),
    status: PLAN_STATUSES.has(rawStatus) ? rawStatus : "executing",
    currentStep,
    steps,
  };
};

const normalizeTodosFromSnapshot = (value: unknown): TodoItem[] => {
  if (!Array.isArray(value)) return [];
  return value
    .filter(isRecord)
    .map((todo) => {
      const status = String(todo.status ?? "") as TodoItem["status"];
      return {
        id: String(todo.id ?? todo.todo_id ?? "").trim(),
        content: String(todo.content ?? ""),
        activeForm: String(todo.activeForm ?? todo.active_form ?? todo.content ?? ""),
        status,
      };
    })
    .filter((todo) => Boolean(todo.id) && Boolean(todo.content) && TODO_STATUSES.has(todo.status));
};

const normalizeSubagentsFromSnapshot = (value: unknown): SubagentState[] => {
  if (!Array.isArray(value)) return [];
  return value
    .filter(isRecord)
    .map((subagent) => {
      const status = String(subagent.status ?? "running") as SubagentState["status"];
      return {
        id: String(subagent.id ?? subagent.subagent_id ?? "").trim(),
        role: String(subagent.role ?? "subagent"),
        status: SUBAGENT_STATUSES.has(status) ? status : "running",
        summary: maybeString(subagent.summary as string | null | undefined),
        parentRunId: maybeString((subagent.parentRunId ?? subagent.parent_run_id) as string | null | undefined),
        iteration: typeof subagent.iteration === "number" ? subagent.iteration : undefined,
        maxIterations: typeof subagent.maxIterations === "number"
          ? subagent.maxIterations
          : typeof subagent.max_iterations === "number"
            ? subagent.max_iterations
            : undefined,
        currentTool: maybeString((subagent.currentTool ?? subagent.tool_name) as string | null | undefined),
        detail: maybeString(subagent.detail as string | null | undefined),
      };
    })
    .filter((subagent) => Boolean(subagent.id));
};

const normalizeAgentProgressFromSnapshot = (value: unknown, conversationId: string): AgentProgressEntry[] => {
  if (!Array.isArray(value)) return [];
  return value
    .filter(isRecord)
    .flatMap((progress) => {
      const stage = String(progress.stage ?? "") as AgentProgressEntry["stage"];
      const status = String(progress.status ?? "") as AgentProgressEntry["status"];
      const phase = String(progress.phase ?? "") as NonNullable<AgentProgressEntry["phase"]>;
      if (!PROGRESS_STAGES.has(stage) || !PROGRESS_STATUSES.has(status)) return [];
      const entry: AgentProgressEntry = {
        type: "progress" as const,
        id: String(progress.id ?? "").trim(),
        stage,
        ...(PROGRESS_PHASES.has(phase) ? { phase } : {}),
        status,
        message: String(progress.message ?? ""),
        label: maybeString(progress.label as string | null | undefined),
        summary: maybeString(progress.summary as string | null | undefined),
        visibility: progress.visibility === "timeline" || progress.visibility === "compact" || progress.visibility === "debug"
          ? progress.visibility
          : undefined,
        detail: maybeString(progress.detail as string | null | undefined),
        toolCallId: maybeString((progress.toolCallId ?? progress.tool_call_id) as string | null | undefined),
        toolName: maybeString((progress.toolName ?? progress.tool_name) as string | null | undefined),
        groupId: maybeString((progress.groupId ?? progress.group_id) as string | null | undefined),
        stepId: maybeString((progress.stepId ?? progress.step_id) as string | null | undefined),
        count: typeof progress.count === "number" ? progress.count : undefined,
        iterationId: maybeString((progress.iterationId ?? progress.iteration_id) as string | null | undefined),
        displayScope: maybeString((progress.displayScope ?? progress.display_scope) as string | null | undefined),
        panelHint: maybeString((progress.panelHint ?? progress.panel_hint) as string | null | undefined),
        requiresAttention: typeof (progress.requiresAttention ?? progress.requires_attention) === "boolean"
          ? Boolean(progress.requiresAttention ?? progress.requires_attention)
          : undefined,
        timestamp: typeof progress.timestamp === "number" ? progress.timestamp : Date.now(),
        conversationId: maybeString(progress.conversationId as string | null | undefined) ?? conversationId,
      };
      return entry.id && entry.message ? [entry] : [];
    })
    .slice(-80);
};

const agentStateFromSnapshot = (
  conversation: ConversationPayload | null | undefined,
  conversationId: string,
): ConversationAgentState | null => {
  const snapshot = conversation?.context_snapshot;
  if (!isRecord(snapshot)) return null;
  const raw = snapshot[UI_AGENT_STATE_KEY];
  if (!isRecord(raw)) return null;
  return {
    plan: normalizePlanFromSnapshot(raw.plan),
    todos: normalizeTodosFromSnapshot(raw.todos),
    subagents: normalizeSubagentsFromSnapshot(raw.subagents),
    agentProgress: normalizeAgentProgressFromSnapshot(raw.agentProgress, conversationId),
  };
};

const hydrateConversationAgentState = (
  conversationId: string,
  conversation: ConversationPayload | null | undefined,
) => {
  const agentState = agentStateFromSnapshot(conversation, conversationId);
  if (!agentState) return;
  useAppStore.setState((state) => ({
    ...(state.conversationId === conversationId ? agentState : {}),
    conversationAgentStates: {
      ...(state.conversationAgentStates ?? {}),
      [conversationId]: agentState,
    },
  }));
};

const isVisibleConversationMeta = (conversation: ConversationMeta | undefined | null): boolean =>
  Boolean(conversation && !conversation.archived);

const visibleActiveConversationId = (
  activeConversationId: string | undefined,
  conversations: ConversationMeta[],
): string | undefined => {
  if (!activeConversationId) return undefined;
  const activeMeta = conversations.find((conversation) => conversation.id === activeConversationId);
  return isVisibleConversationMeta(activeMeta) ? activeConversationId : undefined;
};

const fallbackVisibleConversationId = (
  currentConversationId: string | null | undefined,
  conversations: ConversationMeta[],
): string | undefined => {
  const current = currentConversationId
    ? conversations.find((conversation) => conversation.id === currentConversationId)
    : undefined;
  if (isVisibleConversationMeta(current)) return currentConversationId || undefined;
  return conversations.find(isVisibleConversationMeta)?.id;
};

const upsertConversationMeta = (conversation: ConversationSummary) => {
  useAppStore.setState((state) => {
    const meta = toConversationMeta(conversation);
    return {
      conversations: [
        meta,
        ...state.conversations.filter((item) => item.id !== meta.id),
      ],
    };
  });
};

const hydrateActiveConversation = (
  conversation: ConversationPayload | null | undefined,
  activeConversationId?: string,
  fallbackMessages?: BackendTranscriptMessage[],
  options: { upsertMeta?: boolean; preserveStreamingDraft?: boolean } = {},
) => {
  const conversationId = maybeString(activeConversationId) || conversation?.id || "";
  if (!conversationId) return;

  if (conversation && options.upsertMeta !== false) {
    upsertConversationMeta(conversation);
  }
  useAppStore.getState().applyConversationSwitched({ conversationId });
  if (conversation) {
    useAppStore.getState().setActiveGoal(toConversationGoal(conversation.goal), conversationId);
  }
  hydrateConversationAgentState(conversationId, conversation);

  const transcript = conversation?.messages ?? conversation?.transcript ?? fallbackMessages;
  if (transcript) {
    const cachedMessages = useAppStore.getState().conversationMessages[conversationId]
      ?? (useAppStore.getState().conversationId === conversationId ? useAppStore.getState().messages : []);
    if (Array.isArray(transcript) && transcript.length === 0 && cachedMessages.length > 0) {
      useAppStore.getState().hydrateConversationMessages(
        conversationId,
        cachedMessages,
        { activate: true, isStreaming: cachedMessages.some((message) => Boolean(message.isStreaming)) },
      );
    } else {
      const hydrated = hydrateMessages(transcript);
      const messages = options.preserveStreamingDraft
        ? mergeHydratedWithStreamingDrafts(conversationId, hydrated)
        : hydrated;
      useAppStore.getState().hydrateConversationMessages(
        conversationId,
        messages,
        { activate: true, isStreaming: messages.some((message) => Boolean(message.isStreaming)) },
      );
    }
  }

  const workspaceRoot = maybeString(conversation?.worktree_path) || maybeString(conversation?.workspace_root);
  if (conversation) {
    useAppStore.getState().setWorkingDirectory(workspaceRoot || "");
  }
};

const mergeHydratedWithStreamingDrafts = (
  conversationId: string,
  hydratedMessages: ChatMessage[],
): ChatMessage[] => {
  const state = useAppStore.getState();
  const cachedMessages = state.conversationMessages[conversationId]
    ?? (state.conversationId === conversationId ? state.messages : []);
  const streamingDrafts = cachedMessages.filter(
    (message) => message.role === "assistant" && Boolean(message.isStreaming),
  );
  if (!streamingDrafts.length) return hydratedMessages;

  const hydratedIds = new Set(hydratedMessages.map((message) => message.id));
  const preservedDrafts = streamingDrafts.filter((message) => !hydratedIds.has(message.id));
  if (!preservedDrafts.length) return hydratedMessages;
  return [...hydratedMessages, ...preservedDrafts];
};

const activeConversationWorkspace = (): string => {
  const state = useAppStore.getState();
  const active = state.conversations.find((conversation) => conversation.id === state.conversationId);
  return active?.worktreePath || active?.workspaceRoot || "";
};

const clearActiveConversationView = () => {
  useAppStore.getState().setWorkingDirectory("");
  useAppStore.setState({
    ...conversationResetPayload(),
    conversationId: null,
    activeGoal: null,
    messages: [],
    isStreaming: false,
    toolCallCount: 0,
  });
};

const applyRuntimeSessionSnapshot = (session: RuntimeSessionSnapshot | undefined | null) => {
  if (!session) return;
  useAppStore.getState().setRuntimeSession(session);
  if (session.permission_mode) {
    useAppStore.setState({ permissionMode: fromBackendPermissionMode(session.permission_mode) });
  }
  if (session.capabilities) {
    const current = useAppStore.getState().runtimeCapabilities;
    useAppStore.getState().setRuntimeCapabilities(mergeCapabilities(current ?? undefined, session.capabilities) ?? null);
  }
};

export const handleSessionEvent = (
  e: ServerEvent,
  buffers: { textStreamBuffer: StreamBuffer; thinkingStreamBuffer: StreamBuffer },
): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "llm.model.updated": {
      const ev = e as LlmModelUpdatedEvent;
      const model = stringValue(ev.current_model) || stringValue(ev.model);
      if (model) s.setCurrentModel(model);
      if (ev.provider) s.setCurrentProvider(ev.provider);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      const workingDirectory = maybeString(ev.working_directory);
      if (workingDirectory && !activeConversationWorkspace()) {
        s.setWorkingDirectory(workingDirectory);
      }
      return true;
    }
    case "session.restored":
    case "session.synced": {
      const ev = e as SessionRestoredEvent | SessionSyncedEvent;
      const model = stringValue(ev.current_model)
        || stringValue(ev.model)
        || stringValue(ev.session?.selected_model);
      const workspaceRoot = stringValue(ev.working_directory)
        || stringValue(ev.workspace_root)
        || stringValue(ev.workspace?.root_path)
        || stringValue(ev.session?.workspace_root);
      const restoredConversation = ev.type === "session.restored" ? ev.conversation : null;
      const fallbackMessages = ev.type === "session.restored" ? ev.messages : undefined;
      const activeConversation = ev.active_conversation ?? ev.session?.active_conversation ?? restoredConversation ?? null;
      const activeConversationId = stringValue(ev.active_conversation_id)
        || stringValue(ev.session?.active_conversation_id)
        || activeConversation?.id
        || "";
      const activeConversationIsArchived = Boolean(activeConversation?.archived);
      const switchEventWillHydrate = ev.type === "session.restored" && ev.conversation_switched_follows === true;
      if (switchEventWillHydrate) {
        buffers.textStreamBuffer.destroy();
        buffers.thinkingStreamBuffer.destroy();
      } else {
        clearStreamingState(buffers, { conversationId: activeConversationId || s.conversationId });
      }

      if (model) s.setCurrentModel(model);
      const provider = maybeString(ev.provider);
      if (provider) s.setCurrentProvider(provider);
      if (workspaceRoot && !switchEventWillHydrate) s.setWorkingDirectory(workspaceRoot);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      if (activeConversationIsArchived) {
        clearActiveConversationView();
      } else if (switchEventWillHydrate) {
        // The backend will immediately emit the canonical conversation.switched
        // event. Keep conversation activation on that single path so restore and
        // manual switching cannot diverge.
      } else if (activeConversationId) {
        hydrateActiveConversation(activeConversation, activeConversationId, fallbackMessages);
      } else {
        clearActiveConversationView();
      }
      applyRuntimeSessionSnapshot(ev.session);
      if (ev.type === "session.restored" && ev.error) {
        pushToast(`Session restore warning: ${ev.error}`, "warning", 5000);
      }
      return true;
    }
    case "conversation.list": {
      const ev = e as ConversationListEvent;
      if (ev.conversations) {
        const conversationMetas = ev.conversations.map(toConversationMeta);
        const requestedActiveConversationId = maybeString(ev.active_conversation_id);
        const storeState = useAppStore.getState();
        const requestedEffectiveActiveConversationId = visibleActiveConversationId(requestedActiveConversationId, conversationMetas);
        const pendingLocalActive = !requestedEffectiveActiveConversationId && storeState.conversationId
          ? storeState.conversations.find((conversation) =>
              conversation.id === storeState.conversationId &&
              conversation.title === "New chat" &&
              !conversationMetas.some((item) => item.id === conversation.id) &&
              storeState.messages.length === 0
            )
          : undefined;
        const effectiveActiveConversationId =
          requestedEffectiveActiveConversationId
          ?? (pendingLocalActive ? undefined : fallbackVisibleConversationId(storeState.conversationId, conversationMetas));
        const nextConversationMetas = pendingLocalActive
          ? [pendingLocalActive, ...conversationMetas]
          : conversationMetas;
        const knownConversationIds = new Set(nextConversationMetas.map((conversation) => conversation.id));
        useAppStore.setState((state) => ({
          conversations: nextConversationMetas,
          conversationMessages: Object.fromEntries(
            Object.entries(state.conversationMessages).filter(([id]) => knownConversationIds.has(id)),
          ),
          conversationStreaming: Object.fromEntries(
            Object.entries(state.conversationStreaming).filter(([id]) => knownConversationIds.has(id)),
          ),
          conversationAgentStates: Object.fromEntries(
            Object.entries(state.conversationAgentStates ?? {}).filter(([id]) => knownConversationIds.has(id)),
          ),
        }));
        const eventActiveConversation = ev.active_conversation ?? null;
        const activeConversation = eventActiveConversation
          && eventActiveConversation.id === effectiveActiveConversationId
          && !eventActiveConversation.archived
            ? eventActiveConversation
            : null;
        if (effectiveActiveConversationId) {
          hydrateActiveConversation(activeConversation, effectiveActiveConversationId, undefined, { upsertMeta: false });
          if (requestedActiveConversationId !== effectiveActiveConversationId) {
            sendClientCommand({ type: "conversation.switch", conversation_id: effectiveActiveConversationId });
          }
        } else if (pendingLocalActive) {
          // A freshly-created optimistic conversation can coexist with a stale
          // list response that was already in flight. Keep the blank local
          // conversation visible until the create response reconciles it.
        } else {
          clearActiveConversationView();
        }
        applyRuntimeSessionSnapshot(ev.session);
      }
      return true;
    }
    case "goal.updated": {
      const ev = e as GoalUpdatedEvent & { goal?: GoalInfo | null };
      useAppStore.getState().setActiveGoal(toConversationGoal(ev.goal), maybeString(ev.conversation_id));
      return true;
    }
    case "conversation.switched": {
      const ev = e as ConversationSwitchedEvent;
      if (ev.conversation?.archived) {
        clearActiveConversationView();
      } else if (ev.conversation) {
        const nextConversationId = maybeString(ev.conversation_id) ?? ev.conversation.id;
        hydrateActiveConversation(ev.conversation, nextConversationId, undefined, { preserveStreamingDraft: true });
      }
      applyRuntimeSessionSnapshot(ev.session);
      return true;
    }
    default:
      return false;
  }
};
