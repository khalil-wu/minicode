import { useAppStore } from "../stores";
import type {
  ConversationListEvent,
  ConversationRecordPayload,
  ConversationSummaryPayload,
  ConversationSwitchedEvent,
  GoalInfo,
  GoalUpdatedEvent,
  LlmModelUpdatedEvent,
  ProviderOAuthAuthEvent,
  ProviderOAuthDeviceCodeEvent,
  ProviderOAuthInfoEvent,
  ProviderOAuthProgressEvent,
  UserMessageQueueUpdatedEvent,
  RuntimeSessionSnapshot,
  ServerEvent,
  SessionRestoredEvent,
  SessionSyncedEvent,
} from "../protocol/events";
import { isReplayedEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { clearStreamingState } from "./streamingState";
import type {
  AgentProgressEntry,
  ChatMessage,
  ConversationAgentState,
  EffortLevel,
  PlanState,
  SubagentMessageState,
  SubagentState,
  TodoItem,
  ProviderOAuthFlowProjection,
} from "../stores/types";
import { toConversationGoal } from "../stores/types";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { mergeCapabilities } from "../protocol/capabilities";
import {
  conversationResetPayload,
  LS,
  removeConversationOwnedPrompts,
  visibleDiffReviewForConversation,
  writeLS,
} from "../stores/shared-helpers";
import type { ConversationMeta } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { selectableModelsForProvider } from "../lib/provider-models";
import { normalizeContextLedger } from "./contextLedger";
import {
  isAgentProgressPhase,
  isAgentProgressProviderState,
} from "../protocol/streaming-types";
import { isDesktop, ptyKillConversation } from "../desktop/runtime";
import { releasePreviewScope } from "./previewRequestScope";
import { providerTracePayloadFromDone } from "./providerTrace";

type ConversationSummary = ConversationSummaryPayload;
type ConversationPayload = ConversationRecordPayload;
const UI_AGENT_STATE_KEY = "ui_agent_state";
const RUNTIME_PROTOCOL_VERSION = "1.0.0";
const pendingAuthoritativeConversationResets = new Set<string>();

type ProviderOAuthEvent =
  | ProviderOAuthAuthEvent
  | ProviderOAuthDeviceCodeEvent
  | ProviderOAuthInfoEvent
  | ProviderOAuthProgressEvent;

const providerOAuthEventTime = (event: ServerEvent): number => {
  const parsed = typeof event.timestamp === "string" ? Date.parse(event.timestamp) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const setProviderOAuthProjection = (
  event: ProviderOAuthEvent & ServerEvent,
  patch: Omit<Partial<ProviderOAuthFlowProjection>, "conversationId" | "provider" | "updatedAt"> & Pick<ProviderOAuthFlowProjection, "phase">,
) => {
  const state = useAppStore.getState();
  const owner = event.conversation_id.trim();
  const provider = event.provider.trim();
  const existing = state.providerOAuthFlowsByConversation[owner]?.[provider];
  const updatedAt = providerOAuthEventTime(event);
  state.setProviderOAuthFlow({
    ...existing,
    conversationId: owner,
    provider,
    updatedAt,
    ...(Number.isSafeInteger(event.seq) ? { eventSeq: event.seq } : {}),
    ...patch,
  });
};

const maybeString = (value: string | null | undefined): string | undefined =>
  typeof value === "string" && value ? value : undefined;

const stringValue = (value: string | null | undefined): string =>
  maybeString(value) ?? "";

const setAvailableModelsForCurrentProvider = (
  models: string[] | undefined,
  currentModel: string,
  provider?: string,
  modelsSource?: string,
) => {
  if (!models) return;
  const state = useAppStore.getState();
  state.setAvailableModels(selectableModelsForProvider(
    models,
    currentModel || state.currentModel,
    provider || state.currentProvider,
    modelsSource ?? state.modelsSource,
  ));
  if (modelsSource !== undefined) {
    state.setModelsSource(modelsSource);
  }
};

export const applyUserMessageQueueUpdate = (event: UserMessageQueueUpdatedEvent) => {
  const conversationId = event.conversation_id;
  const steeredCurrentTurn = event.status === "dequeued" && (
    event.turn_mode === "steer" || event.reason === "steered_current_turn"
  );
  const updateMessages = (messages: ChatMessage[]): ChatMessage[] => {
    const steerTargetMessageId = steeredCurrentTurn
      ? event.target_message_id || messages.find((message) => (
          message.role === "assistant"
          && message.id !== event.message_id
          && Boolean(message.isStreaming || message.isThinkingStreaming)
        ))?.id
      : undefined;
    const cancelledPosition = event.status === "cancelled"
      ? messages.find((message) => (
          message.id === event.user_message_id || message.queueMessageId === event.message_id
        ))?.queuePosition
      : undefined;
    const updated = messages
    .filter((message) => {
      if (steeredCurrentTurn && message.role === "assistant" && message.id === event.message_id) {
        // A steer continues the already-streaming assistant turn. The queued
        // assistant placeholder must not become a second streaming answer.
        return false;
      }
      if (event.status !== "cancelled") return true;
      const queuedUser = message.role === "user" && (
        message.id === event.user_message_id || message.queueMessageId === event.message_id
      );
      const queuedAssistant = message.role === "assistant" && message.id === event.message_id;
      return !queuedUser && !queuedAssistant;
    })
    .map((message) => {
      const isUser = message.role === "user" && (
        message.id === event.user_message_id || message.queueMessageId === event.message_id
      );
      const isAssistant = message.role === "assistant" && message.id === event.message_id;
      if (!isUser && !isAssistant) {
        return cancelledPosition && message.queueState === "queued" && (message.queuePosition ?? 0) > cancelledPosition
          ? { ...message, queuePosition: (message.queuePosition ?? 1) - 1 }
          : message;
      }
      if (event.status === "queued") {
        return {
          ...message,
          queueState: "queued" as const,
          queuePosition: event.position,
          queueMessageId: event.message_id,
          ...(isAssistant ? { isStreaming: false, isThinkingStreaming: false } : {}),
        };
      }
      if (event.status === "dequeued") {
        return {
          ...message,
          queueState: undefined,
          queuePosition: undefined,
          ...(isUser && steeredCurrentTurn
            ? { steeredIntoMessageId: steerTargetMessageId }
            : {}),
          ...(isAssistant && !steeredCurrentTurn ? { isStreaming: true } : {}),
        };
      }
      return {
        ...message,
        queueState: "cancelled" as const,
        queuePosition: undefined,
      };
    });
    if (!steeredCurrentTurn || !steerTargetMessageId) return updated;
    const steeredUserIndex = updated.findIndex((message) => (
      message.role === "user"
      && (message.id === event.user_message_id || message.queueMessageId === event.message_id)
    ));
    const targetAssistantIndex = updated.findIndex((message) => (
      message.role === "assistant" && message.id === steerTargetMessageId
    ));
    if (steeredUserIndex < 0 || targetAssistantIndex < 0 || steeredUserIndex < targetAssistantIndex) {
      return updated;
    }
    const reordered = [...updated];
    const [steeredUser] = reordered.splice(steeredUserIndex, 1);
    const insertBefore = reordered.findIndex((message) => (
      message.role === "assistant" && message.id === steerTargetMessageId
    ));
    if (!steeredUser || insertBefore < 0) return updated;
    reordered.splice(insertBefore, 0, steeredUser);
    return reordered;
  };

  useAppStore.setState((state) => {
    if (state.sideChats[conversationId]) {
      const thread = state.sideChats[conversationId];
      return {
        sideChats: {
          ...state.sideChats,
          [conversationId]: {
            ...thread,
            messages: updateMessages(thread.messages),
            isStreaming: event.status === "dequeued" && !steeredCurrentTurn ? true : thread.isStreaming,
          },
        },
      };
    }

    const isActive = conversationId === state.conversationId;
    const source = isActive ? state.messages : state.conversationMessages[conversationId] ?? [];
    const messages = updateMessages(source);
    const streaming = event.status === "dequeued" && !steeredCurrentTurn
      ? true
      : messages.some((message) => message.isStreaming || message.isThinkingStreaming);
    const resetRunState = {
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
    };
    return {
      ...(isActive ? {
        messages,
        isStreaming: streaming,
        ...(event.status === "dequeued" && !steeredCurrentTurn ? resetRunState : {}),
      } : {}),
      conversationMessages: {
        ...state.conversationMessages,
        [conversationId]: messages,
      },
      conversationStreaming: {
        ...state.conversationStreaming,
        [conversationId]: streaming,
      },
      ...(event.status === "dequeued" && !steeredCurrentTurn ? {
        conversationAgentStates: {
          ...state.conversationAgentStates,
          [conversationId]: resetRunState,
        },
      } : {}),
    };
  });
};

const toConversationMeta = (conversation: ConversationSummary) => ({
  id: conversation.id,
  title: maybeString(conversation.title) ?? "未命名",
  updatedAt: maybeString(conversation.updated_at) ?? new Date().toISOString(),
  revision: Number.isSafeInteger(conversation.revision) && Number(conversation.revision) >= 0
    ? Number(conversation.revision)
    : undefined,
  summary: maybeString(conversation.summary),
  compactionState: maybeString(conversation.compaction_state),
  conversationType: conversation.conversation_type === "side_chat" ? "side_chat" as const : "main" as const,
  archived: conversation.archived,
  memoryMode: maybeString(conversation.memory_mode),
  memoryPolluted: conversation.memory_polluted === true,
  memoryPollutionSources: Array.isArray(conversation.memory_pollution_sources)
    ? conversation.memory_pollution_sources.filter((source): source is string => typeof source === "string" && Boolean(source.trim()))
    : [],
  workspaceRoot: maybeString(conversation.workspace_root),
  gitBranch: maybeString(conversation.git_branch),
  worktreePath: maybeString(conversation.worktree_path),
  gitIsolated: conversation.git_isolated,
  goal: toConversationGoal(conversation.goal),
  parentConversationId: maybeString(conversation.parent_conversation_id),
  parentMessageIndex: typeof conversation.parent_message_index === "number" ? conversation.parent_message_index : undefined,
  forkId: maybeString(conversation.fork_id),
  branchKind: maybeString(conversation.branch_kind),
  mergedIntoConversationId: maybeString(conversation.merged_into_conversation_id),
  mergedAt: maybeString(conversation.merged_at),
});

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value));

const PLAN_STEP_STATUSES = new Set<PlanState["plan"][number]["status"]>(["pending", "in_progress", "completed"]);
const TODO_STATUSES = new Set<TodoItem["status"]>(["pending", "in_progress", "completed", "blocked"]);
const SUBAGENT_STATUSES = new Set<SubagentState["status"]>(["pending", "running", "blocked", "done", "partial", "cancelled", "error"]);
const PROGRESS_STAGES = new Set<AgentProgressEntry["stage"]>([
  "status",
  "planning",
  "tool",
  "approval",
  "verification",
  "image_generation",
  "cache",
  "final",
]);
const PROGRESS_STATUSES = new Set<AgentProgressEntry["status"]>(["running", "completed", "partial", "failed", "info"]);

const normalizePlanFromSnapshot = (value: unknown): PlanState | null => {
  if (!isRecord(value) || !Array.isArray(value.plan)) return null;
  const plan: PlanState["plan"] = value.plan
    .filter(isRecord)
    .map((step) => {
      const rawStatus = String(step.status ?? "pending") as PlanState["plan"][number]["status"];
      return {
        step: String(step.step ?? ""),
        status: PLAN_STEP_STATUSES.has(rawStatus) ? rawStatus : "pending",
      };
    });
  const threadId = String(value.threadId ?? value.thread_id ?? "").trim();
  const turnId = String(value.turnId ?? value.turn_id ?? "").trim();
  if (!threadId || !turnId) return null;
  return {
    threadId,
    turnId,
    plan,
    explanation: typeof value.explanation === "string" ? value.explanation : undefined,
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
      const stringList = (candidate: unknown): string[] | undefined => {
        if (!Array.isArray(candidate)) return undefined;
        const values = candidate.filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
        return values.length ? values : undefined;
      };
      const messages: SubagentMessageState[] | undefined = Array.isArray(subagent.messages)
        ? subagent.messages.filter(isRecord).map((message) => ({
          messageId: String(message.messageId ?? message.message_id ?? ""),
          senderId: String(message.senderId ?? message.sender_id ?? ""),
          recipientId: String(message.recipientId ?? message.recipient_id ?? ""),
          content: String(message.content ?? ""),
          createdAt: Number(message.createdAt ?? message.created_at ?? Date.now()),
          seq: typeof message.seq === "number" ? message.seq : undefined,
          senderMailboxEpoch: typeof (message.senderMailboxEpoch ?? message.sender_mailbox_epoch) === "number"
            ? Number(message.senderMailboxEpoch ?? message.sender_mailbox_epoch)
            : undefined,
          recipientMailboxEpoch: typeof (message.recipientMailboxEpoch ?? message.recipient_mailbox_epoch) === "number"
            ? Number(message.recipientMailboxEpoch ?? message.recipient_mailbox_epoch)
            : undefined,
          deliveryStatus: message.deliveryStatus === "sending" || message.deliveryStatus === "sent" || message.deliveryStatus === "failed"
            ? message.deliveryStatus as SubagentMessageState["deliveryStatus"]
            : undefined,
        })).filter((message) => Boolean(message.messageId) && Boolean(message.content))
        : undefined;
      const normalizeDelegatedText = (candidate: unknown): string | undefined => {
        return maybeString(candidate as string | null | undefined);
      };
      return {
        id: String(subagent.id ?? subagent.subagent_id ?? "").trim(),
        role: String(subagent.role ?? "subagent"),
        status: SUBAGENT_STATUSES.has(status) ? status : "running",
        agentPath: maybeString((subagent.agentPath ?? subagent.agent_path) as string | null | undefined),
        mailboxEpoch: typeof (subagent.mailboxEpoch ?? subagent.mailbox_epoch) === "number"
          ? Number(subagent.mailboxEpoch ?? subagent.mailbox_epoch)
          : undefined,
        summary: normalizeDelegatedText(subagent.summary),
        parentRunId: maybeString((subagent.parentRunId ?? subagent.parent_run_id) as string | null | undefined),
        iteration: typeof subagent.iteration === "number" ? subagent.iteration : undefined,
        maxIterations: typeof subagent.maxIterations === "number"
          ? subagent.maxIterations
          : typeof subagent.max_iterations === "number"
            ? subagent.max_iterations
            : undefined,
        currentTool: maybeString((subagent.currentTool ?? subagent.tool_name) as string | null | undefined),
        detail: normalizeDelegatedText(subagent.detail),
        nodeId: maybeString((subagent.nodeId ?? subagent.node_id) as string | null | undefined),
        taskId: maybeString((subagent.taskId ?? subagent.task_id) as string | null | undefined),
        dependsOn: stringList(subagent.dependsOn ?? subagent.depends_on),
        blockedBy: stringList(subagent.blockedBy ?? subagent.blocked_by),
        objective: maybeString(subagent.objective as string | null | undefined),
        currentActivity: normalizeDelegatedText(subagent.currentActivity ?? subagent.current_activity),
        waitingOn: maybeString((subagent.waitingOn ?? subagent.waiting_on) as string | null | undefined),
        background: typeof subagent.background === "boolean" ? subagent.background : undefined,
        resultAvailable: typeof (subagent.resultAvailable ?? subagent.result_available) === "boolean"
          ? Boolean(subagent.resultAvailable ?? subagent.result_available)
          : undefined,
        activityLog: stringList(subagent.activityLog ?? subagent.activity_log),
        messages,
        resultContent: maybeString((subagent.resultContent ?? subagent.result_content) as string | null | undefined),
        resultError: maybeString((subagent.resultError ?? subagent.result_error) as string | null | undefined),
        terminationReason: maybeString((subagent.terminationReason ?? subagent.termination_reason) as string | null | undefined),
        terminationInitiator: maybeString((subagent.terminationInitiator ?? subagent.termination_initiator) as string | null | undefined),
        checkpointId: maybeString((subagent.checkpointId ?? subagent.checkpoint_id) as string | null | undefined),
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
      const rawProviderState = progress.providerState ?? progress.provider_state;
      const providerState = isAgentProgressProviderState(rawProviderState)
        ? rawProviderState
        : undefined;
      const entry: AgentProgressEntry = {
        type: "progress" as const,
        id: String(progress.id ?? "").trim(),
        stage,
        ...(isAgentProgressPhase(phase) ? { phase } : {}),
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
        retryAttempt: typeof (progress.retryAttempt ?? progress.retry_attempt) === "number"
          ? Number(progress.retryAttempt ?? progress.retry_attempt)
          : undefined,
        maxRetries: typeof (progress.maxRetries ?? progress.max_retries) === "number"
          ? Number(progress.maxRetries ?? progress.max_retries)
          : undefined,
        retryAfterMs: typeof (progress.retryAfterMs ?? progress.retry_after_ms) === "number"
          ? Number(progress.retryAfterMs ?? progress.retry_after_ms)
          : undefined,
        errorMessage: maybeString((progress.errorMessage ?? progress.error_message) as string | null | undefined),
        operationId: maybeString((progress.operationId ?? progress.operation_id) as string | null | undefined),
        providerState,
        iterationId: maybeString((progress.iterationId ?? progress.iteration_id) as string | null | undefined),
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

const contextLedgerFromSnapshot = (conversation: ConversationPayload | null | undefined) => {
  const snapshot = conversation?.context_snapshot;
  if (!isRecord(snapshot) || !isRecord(snapshot.context_ledger)) return null;
  return normalizeContextLedger(snapshot.context_ledger);
};

const hydrateConversationAgentState = (
  conversationId: string,
  conversation: ConversationPayload | null | undefined,
) => {
  const agentState = agentStateFromSnapshot(conversation, conversationId);
  const contextLedger = contextLedgerFromSnapshot(conversation);
  if (contextLedger && useAppStore.getState().conversationId === conversationId) {
    useAppStore.setState((state) => ({
      contextUsage: {
        used: state.contextUsage?.used ?? contextLedger.actual_tokens,
        limit: state.contextUsage?.limit ?? 0,
        compactedAt: state.contextUsage?.compactedAt,
        compactSummary: state.contextUsage?.compactSummary,
        ledger: contextLedger,
      },
    }));
  }
  if (!agentState) return;
  if (hasStreamingAssistantForConversation(conversationId)) return;
  useAppStore.setState((state) => ({
    ...(state.conversationId === conversationId ? agentState : {}),
    conversationAgentStates: {
      ...(state.conversationAgentStates ?? {}),
      [conversationId]: agentState,
    },
  }));
};

const hasStreamingAssistantForConversation = (conversationId: string): boolean => {
  const state = useAppStore.getState();
  const cachedMessages = state.conversationId === conversationId
    ? state.messages
    : state.conversationMessages[conversationId] ?? [];
  return cachedMessages.some((message) =>
    message.role === "assistant" && Boolean(message.isStreaming),
  );
};

const isVisibleConversationMeta = (conversation: ConversationMeta | undefined | null): boolean =>
  Boolean(conversation && conversation.conversationType !== "side_chat" && !conversation.archived);

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

const normalizedConversationRevision = (value: unknown): number | undefined => (
  Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : undefined
);

const incomingConversationMetaIsStale = (
  incoming: Pick<ConversationMeta, "revision" | "updatedAt">,
  existing: Pick<ConversationMeta, "revision" | "updatedAt">,
): boolean => {
  const incomingRevision = normalizedConversationRevision(incoming.revision);
  const existingRevision = normalizedConversationRevision(existing.revision);
  if (incomingRevision !== undefined && existingRevision !== undefined) {
    return incomingRevision < existingRevision;
  }
  if (existingRevision !== undefined && incomingRevision === undefined) {
    return true;
  }
  const incomingUpdatedAt = Date.parse(String(incoming.updatedAt || ""));
  const existingUpdatedAt = Date.parse(String(existing.updatedAt || ""));
  return Number.isFinite(incomingUpdatedAt)
    && Number.isFinite(existingUpdatedAt)
    && incomingUpdatedAt < existingUpdatedAt;
};

const conversationSnapshotIsStale = (conversation: ConversationSummary): boolean => {
  const existing = useAppStore.getState().conversations.find((item) => item.id === conversation.id);
  if (!existing) return false;
  return incomingConversationMetaIsStale(toConversationMeta(conversation), existing);
};

const upsertConversationMeta = (
  conversation: ConversationSummary,
  options: { forceAuthoritative?: boolean } = {},
) => {
  useAppStore.setState((state) => {
    const meta = toConversationMeta(conversation);
    const existingIndex = state.conversations.findIndex((item) => item.id === meta.id);
    if (existingIndex >= 0) {
      const existing = state.conversations[existingIndex];
      if (!options.forceAuthoritative && incomingConversationMetaIsStale(meta, existing)) {
        return state;
      }
      const conversations = [...state.conversations];
      conversations[existingIndex] = meta;
      return { conversations };
    }
    return {
      conversations: [meta, ...state.conversations],
    };
  });
};

const applyQueuedUserMessageSnapshot = (
  entries: RuntimeSessionSnapshot["queued_user_messages"],
) => {
  if (!Array.isArray(entries) || entries.length === 0) return;
  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    let activeMessages = state.messages;
    for (const entry of entries) {
      const conversationId = String(entry?.conversation_id || "").trim();
      const messageId = String(entry?.message_id || "").trim();
      if (!conversationId || !messageId) continue;
      const userMessageId = String(entry.user_message_id || `user_${messageId}`).trim();
      const position = typeof entry.position === "number" ? entry.position : undefined;
      const content = String(entry.content || "");
      const current = conversationMessages[conversationId] ? [...conversationMessages[conversationId]] : [];
      const userIndex = current.findIndex((message) => message.id === userMessageId || message.queueMessageId === messageId);
      const assistantIndex = current.findIndex((message) => message.id === messageId);
      const timestamp = Date.now();
      if (userIndex >= 0) {
        current[userIndex] = {
          ...current[userIndex],
          queueState: "queued",
          queuePosition: position,
          queueMessageId: messageId,
        };
      } else {
        current.push({
          id: userMessageId,
          role: "user",
          content,
          artifacts: [],
          timestamp,
          queueState: "queued",
          queuePosition: position,
          queueMessageId: messageId,
        });
      }
      if (assistantIndex >= 0) {
        current[assistantIndex] = {
          ...current[assistantIndex],
          queueState: "queued",
          queuePosition: position,
          queueMessageId: messageId,
          isStreaming: false,
          isThinkingStreaming: false,
        };
      } else {
        current.push({
          id: messageId,
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp,
          queueState: "queued",
          queuePosition: position,
          queueMessageId: messageId,
          isStreaming: false,
        });
      }
      conversationMessages[conversationId] = current;
      if (state.conversationId === conversationId) activeMessages = current;
    }
    return { conversationMessages, messages: activeMessages };
  });
};

const attachmentRefsFromRuntimeInput = (
  attachments: Record<string, unknown>[] | undefined,
): NonNullable<ChatMessage["attachmentRefs"]> => {
  if (!Array.isArray(attachments)) return [];
  return attachments.flatMap((attachment) => {
    const name = String(attachment.file_name ?? attachment.name ?? "").trim();
    const artifactId = String(attachment.artifact_id ?? attachment.artifactId ?? "").trim();
    if (!name || !artifactId) return [];
    const mediaType = String(attachment.media_type ?? attachment.mediaType ?? "application/octet-stream");
    const rawKind = String(attachment.kind ?? (mediaType.startsWith("image/") ? "image" : "document"));
    return [{
      id: String(attachment.id ?? artifactId),
      name,
      kind: rawKind === "image" ? "image" as const : rawKind === "document" ? "document" as const : "file" as const,
      mediaType,
      sizeBytes: Number(attachment.size_bytes ?? attachment.sizeBytes ?? 0),
      artifactId,
      docId: String(attachment.doc_id ?? attachment.docId ?? ""),
      inputSource: attachment.input_source === "pasted_text" || attachment.inputSource === "pasted_text"
        ? "pasted_text" as const
        : "upload" as const,
    }];
  });
};

const applyPendingTurnInputSnapshot = (
  entries: RuntimeSessionSnapshot["pending_turn_inputs"],
) => {
  if (!Array.isArray(entries) || entries.length === 0) return;
  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    let activeMessages = state.messages;
    for (const entry of entries) {
      if (entry?.mode !== "steer") continue;
      const conversationId = String(entry.conversation_id || "").trim();
      const messageId = String(entry.message_id || "").trim();
      const userMessageId = String(entry.user_message_id || `user_${messageId}`).trim();
      const targetMessageId = String(entry.target_message_id || "").trim();
      if (!conversationId || !messageId || !userMessageId) continue;
      const current = conversationMessages[conversationId]
        ? [...conversationMessages[conversationId]]
        : state.conversationId === conversationId
          ? [...state.messages]
          : [];
      const withoutPlaceholder = current.filter((message) => !(
        message.role === "assistant" && message.id === messageId
      ));
      const existingUserIndex = withoutPlaceholder.findIndex((message) => (
        message.role === "user"
        && (message.id === userMessageId || message.queueMessageId === messageId)
      ));
      const attachmentRefs = attachmentRefsFromRuntimeInput(entry.attachments);
      const restoredUser: ChatMessage = existingUserIndex >= 0
        ? {
            ...withoutPlaceholder[existingUserIndex]!,
            queueState: undefined,
            queuePosition: undefined,
            queueMessageId: messageId,
            steeredIntoMessageId: targetMessageId || undefined,
            ...(attachmentRefs.length ? { attachmentRefs } : {}),
          }
        : {
            id: userMessageId,
            role: "user",
            content: String(entry.content || ""),
            attachmentRefs,
            artifacts: [],
            timestamp: Number(entry.queued_at_ms || Date.now()),
            queueMessageId: messageId,
            steeredIntoMessageId: targetMessageId || undefined,
          };
      if (existingUserIndex >= 0) withoutPlaceholder.splice(existingUserIndex, 1);
      const targetIndex = targetMessageId
        ? withoutPlaceholder.findIndex((message) => message.role === "assistant" && message.id === targetMessageId)
        : -1;
      if (targetIndex >= 0) withoutPlaceholder.splice(targetIndex, 0, restoredUser);
      else withoutPlaceholder.push(restoredUser);
      conversationMessages[conversationId] = withoutPlaceholder;
      if (state.conversationId === conversationId) activeMessages = withoutPlaceholder;
    }
    return { conversationMessages, messages: activeMessages };
  });
};

const applyActiveStreamSnapshot = (session: RuntimeSessionSnapshot) => {
  // The backend always emits this field as the authoritative set of live
  // conversation runs.  An empty array is therefore meaningful: it seals any
  // stale renderer-side stream left behind when the socket disconnected near
  // DONE.  Older snapshots that omit the field remain non-authoritative.
  if (!Array.isArray(session.active_stream_conversation_ids)) return;
  const activeStreamIds = session.active_stream_conversation_ids
    .map((id) => String(id || "").trim())
    .filter(Boolean);
  const activeStreamSet = new Set(activeStreamIds);
  const legacyActiveConversationId = String(session.active_conversation_id || "").trim();
  if (
    activeStreamSet.size === 0
    && String(session.active_task_id || "").trim()
    && legacyActiveConversationId
  ) {
    // Compatibility with snapshots produced before per-conversation run ids
    // were added.  A concrete active task still proves that owner is live.
    activeStreamSet.add(legacyActiveConversationId);
  }
  const steerTargets = new Map<string, Set<string>>();
  for (const entry of session.pending_turn_inputs ?? []) {
    const conversationId = String(entry?.conversation_id || "").trim();
    const targetMessageId = String(entry?.target_message_id || "").trim();
    if (!conversationId || !targetMessageId) continue;
    const targets = steerTargets.get(conversationId) ?? new Set<string>();
    targets.add(targetMessageId);
    steerTargets.set(conversationId, targets);
  }

  const projectStreamingAssistant = (
    messages: ChatMessage[],
    conversationId: string,
    isStreaming: boolean,
  ): ChatMessage[] => {
    if (!isStreaming) {
      return messages.map((message) => (
        message.isStreaming || message.isThinkingStreaming
          ? { ...message, isStreaming: false, isThinkingStreaming: false }
          : message
      ));
    }
    const targets = steerTargets.get(conversationId);
    let fallbackIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === "assistant") {
        fallbackIndex = index;
        break;
      }
    }
    return messages.map((message, index) => {
      const ownsActiveStream = message.role === "assistant"
        && ((targets?.has(message.id) ?? false) || (!targets?.size && index === fallbackIndex));
      if (ownsActiveStream) return { ...message, isStreaming: true };
      return message.isStreaming || message.isThinkingStreaming
        ? { ...message, isStreaming: false, isThinkingStreaming: false }
        : message;
    });
  };

  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    const conversationStreaming = { ...state.conversationStreaming };
    const sideChats = { ...state.sideChats };
    const knownConversationIds = new Set<string>([
      ...Object.keys(conversationMessages),
      ...Object.keys(conversationStreaming),
      ...Object.keys(sideChats),
      ...activeStreamSet,
      ...(state.conversationId ? [state.conversationId] : []),
    ]);
    let activeMessages = state.messages;
    for (const conversationId of knownConversationIds) {
      const isStreaming = activeStreamSet.has(conversationId);
      conversationStreaming[conversationId] = isStreaming;
      if (sideChats[conversationId]) {
        sideChats[conversationId] = {
          ...sideChats[conversationId],
          isStreaming,
          messages: projectStreamingAssistant(
            sideChats[conversationId].messages,
            conversationId,
            isStreaming,
          ),
        };
        continue;
      }
      const source = conversationMessages[conversationId]
        ?? (state.conversationId === conversationId ? state.messages : []);
      const messages = projectStreamingAssistant(source, conversationId, isStreaming);
      conversationMessages[conversationId] = messages;
      if (state.conversationId === conversationId) activeMessages = messages;
    }
    return {
      conversationMessages,
      conversationStreaming,
      sideChats,
      messages: activeMessages,
      isStreaming: Boolean(state.conversationId && activeStreamSet.has(state.conversationId)),
    };
  });
};

const hydrateActiveConversation = (
  conversation: ConversationPayload | null | undefined,
  activeConversationId?: string,
  fallbackMessages?: BackendTranscriptMessage[],
  options: {
    upsertMeta?: boolean;
    preserveStreamingAssistant?: boolean;
    forceAuthoritative?: boolean;
  } = {},
) => {
  const conversationId = maybeString(activeConversationId) || conversation?.id || "";
  if (!conversationId) return;
  const staleSnapshot = Boolean(
    conversation
    && !options.forceAuthoritative
    && conversationSnapshotIsStale(conversation),
  );

  if (conversation && options.upsertMeta !== false) {
    upsertConversationMeta(conversation, {
      forceAuthoritative: options.forceAuthoritative,
    });
  }
  useAppStore.getState().applyConversationSwitched({ conversationId });
  if (conversation && !staleSnapshot) {
    useAppStore.getState().setActiveGoal(
      toConversationGoal(conversation.goal),
      conversationId,
      normalizedConversationRevision(conversation.revision),
    );
  }
  const transcript = conversation?.messages ?? conversation?.transcript ?? fallbackMessages;
  if (transcript) {
    const cachedMessages = useAppStore.getState().conversationMessages[conversationId]
      ?? (useAppStore.getState().conversationId === conversationId ? useAppStore.getState().messages : []);
    if (
      Array.isArray(transcript)
      && transcript.length === 0
      && cachedMessages.length > 0
      && !options.forceAuthoritative
    ) {
      useAppStore.getState().hydrateConversationMessages(
        conversationId,
        cachedMessages,
        { activate: true, isStreaming: cachedMessages.some((message) => Boolean(message.isStreaming)) },
      );
    } else {
      const hydrated = hydrateMessages(transcript);
      const messages = options.preserveStreamingAssistant || staleSnapshot
        ? mergeHydratedWithStreamingAssistants(conversationId, hydrated)
        : hydrated;
      useAppStore.getState().hydrateConversationMessages(
        conversationId,
        messages,
        { activate: true, isStreaming: messages.some((message) => Boolean(message.isStreaming)) },
      );
    }
  }
  restoreProviderInspectorFromTranscript(
    conversationId,
    useAppStore.getState().messages,
  );
  // Agent state and transcript describe one durable snapshot.  Apply the
  // transcript first so a stale local streaming placeholder cannot suppress a
  // completed plan/progress snapshot; genuinely live streams are preserved by
  // the merge above and still block stale agent-state replacement.
  if (!staleSnapshot) hydrateConversationAgentState(conversationId, conversation);

  const workspaceRoot = maybeString(conversation?.worktree_path) || maybeString(conversation?.workspace_root);
  if (conversation && !staleSnapshot) {
    // "" is faithful, not a missing field: the backend's own
    // `_switch_workspace_for_conversation` calls `_clear_workspace_runtime()`
    // when a conversation has no workspace_root/worktree_path, so mirroring the
    // empty value is what keeps the renderer's workspace equal to the session's.
    useAppStore.getState().setWorkingDirectory(workspaceRoot || "");
  }
};

const restoreProviderInspectorFromTranscript = (
  conversationId: string,
  messages: ChatMessage[],
) => {
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    for (const [blockIndex, block] of (message.blocks ?? []).entries()) {
      if (block.type !== "text" || !block.providerRaw) continue;
      const providerRaw = block.finishReason && !block.providerRaw.finish_reason
        ? { ...block.providerRaw, finish_reason: block.finishReason }
        : block.providerRaw;
      const usage = message.usage
        ? {
            input: message.usage.input,
            ordinaryInput: message.usage.ordinaryInput,
            inputIncludesCacheRead: message.usage.inputIncludesCacheRead,
            inputIncludesCacheWrite: message.usage.inputIncludesCacheWrite,
            output: message.usage.output,
            cacheRead: message.usage.cacheRead ?? 0,
            cacheWrite: message.usage.cacheWrite ?? 0,
            promptCacheTotal: message.usage.promptCacheTotal,
            promptCacheHitRate: message.usage.promptCacheHitRate,
            reasoning: message.usage.reasoning ?? 0,
          }
        : undefined;
      const payload = providerTracePayloadFromDone(providerRaw, usage);
      if (!payload) continue;
      const targetId = String(
        payload.trace_id
        || `${message.id}:${block.itemId || blockIndex}:provider:transcript`,
      );
      useAppStore.getState().addInspectorEntry({
        targetKind: "provider",
        targetId,
        payload: {
          ...payload,
          conversationId,
          messageId: message.id,
          restored_from: "transcript",
        },
        timestamp: message.completedAt ?? message.timestamp,
      });
    }
  }
};

const mergeHydratedWithStreamingAssistants = (
  conversationId: string,
  hydratedMessages: ChatMessage[],
): ChatMessage[] => {
  const state = useAppStore.getState();
  const cachedMessages = state.conversationMessages[conversationId]
    ?? (state.conversationId === conversationId ? state.messages : []);

  // A delayed conversation snapshot must not clobber a fresher cached turn.
  // Preserve the in-flight version of the same message as well as newer tail
  // messages (including user steer/follow-up messages), not just assistant
  // messages missing from the snapshot.
  const messageMs = (message: ChatMessage): number => Math.max(
    Number((message as { completedAt?: number }).completedAt ?? 0),
    Number(message.timestamp ?? 0),
  );
  const hydratedLatest = hydratedMessages.reduce(
    (max, message) => Math.max(max, messageMs(message)),
    0,
  );
  const hydratedIds = new Set(hydratedMessages.map((message) => message.id));
  const cachedById = new Map(cachedMessages.map((message) => [message.id, message]));

  const merged = hydratedMessages.map((message) => {
    const cached = cachedById.get(message.id);
    if (!cached) return message;
    if (cached.isStreaming || cached.isThinkingStreaming) return cached;
    return messageMs(cached) > messageMs(message) ? cached : message;
  });

  const preserved = cachedMessages.filter((message) => {
    if (hydratedIds.has(message.id)) return false;
    if (Boolean(message.isStreaming || message.isThinkingStreaming)) return true;
    return messageMs(message) > hydratedLatest;
  });

  if (!preserved.length) return merged;
  return [...merged, ...preserved];
};

const activeConversationWorkspace = (): string => {
  const state = useAppStore.getState();
  const active = state.conversations.find((conversation) => conversation.id === state.conversationId);
  return active?.worktreePath || active?.workspaceRoot || "";
};

const clearActiveConversationView = () => {
  const state = useAppStore.getState();
  const hasCodeContext = Boolean(
    state.workingDirectory
    || state.editorTabs.length > 0
    || state.activeTabPath
    || state.activeEditorPath
  );
  writeLS(LS.conversation.activeId, "");
  useAppStore.setState({
    ...conversationResetPayload(),
    conversationId: null,
    activeGoal: null,
    messages: [],
    isStreaming: false,
    toolCallCount: 0,
    draft: "",
    attachments: [],
    quotedMessage: null,
    selectedMentions: [],
    selectedSkills: [],
    allowedRemoteImageDomains: [],
    actionChip: null,
    mentionResults: [],
    slashPanelOpen: false,
    mentionPanelOpen: false,
    prMonitor: null,
    ...(hasCodeContext ? { appMode: "code" as const } : {}),
  });
};

const applyRuntimeSessionSnapshot = (session: RuntimeSessionSnapshot | undefined | null) => {
  if (!session) return;
  useAppStore.getState().setRuntimeSession(session);
  applyQueuedUserMessageSnapshot(session.queued_user_messages);
  applyPendingTurnInputSnapshot(session.pending_turn_inputs);
  applyActiveStreamSnapshot(session);
  if (session.permission_mode) {
    useAppStore.setState({ permissionMode: fromBackendPermissionMode(session.permission_mode) });
  }
  if (session.capabilities) {
    const current = useAppStore.getState().runtimeCapabilities;
    const capabilities = mergeCapabilities(current ?? undefined, session.capabilities) ?? null;
    useAppStore.getState().setRuntimeCapabilities(capabilities);
    if (Array.isArray(capabilities?.skills)) {
      useAppStore.getState().setAvailableSkills(normalizeSkillList(capabilities.skills));
    }
    if (Array.isArray(capabilities?.composer_commands)) {
      useAppStore.getState().setSlashCommands(normalizeSlashCommands(capabilities.composer_commands));
    }
  }
};

export const handleSessionEvent = (
  e: ServerEvent,
  buffers: { textStreamBuffer: StreamBuffer; thinkingStreamBuffer: StreamBuffer },
): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "user_message.queue.updated": {
      applyUserMessageQueueUpdate(e as UserMessageQueueUpdatedEvent);
      return true;
    }
    case "llm.model.updated": {
      const ev = e as LlmModelUpdatedEvent;
      const model = stringValue(ev.current_model) || stringValue(ev.model);
      if (model) s.setCurrentModel(model);
      if (ev.provider) s.setCurrentProvider(ev.provider);
      const effectiveReasoningEffort = String(
        ev.effective_reasoning_effort || "",
      ).trim().toLowerCase();
      // The backend emits an effective value only after matching it against
      // this model's provider-declared catalog, including MiniCode custom levels.
      if (effectiveReasoningEffort) {
        useAppStore.setState({ effortLevel: effectiveReasoningEffort as EffortLevel });
      }
      s.setCurrentProviderMeta({
        providerId: stringValue(ev.provider_id),
        baseUrl: stringValue(ev.base_url),
        wireApi: stringValue(ev.wire_api),
      });
            setAvailableModelsForCurrentProvider(ev.available_models, model, maybeString(ev.provider), maybeString(ev.models_source));
      const workingDirectory = maybeString(ev.working_directory);
      if (workingDirectory && !activeConversationWorkspace()) {
        s.setWorkingDirectory(workingDirectory);
      }
      return true;
    }
    case "llm.provider.oauth.auth": {
      const ev = e as ProviderOAuthAuthEvent & ServerEvent;
      setProviderOAuthProjection(ev, {
        phase: "auth_url",
        url: ev.url,
        instructions: ev.instructions,
      });
      if (!isReplayedEvent(e) && ev.conversation_id === s.conversationId) {
        pushToast(`${ev.provider} 的 OAuth 授权链接已就绪，请在提供商设置中确认并打开。`, "info", 12_000);
      }
      return true;
    }
    case "llm.provider.oauth.device_code": {
      const ev = e as ProviderOAuthDeviceCodeEvent & ServerEvent;
      const updatedAt = providerOAuthEventTime(e);
      setProviderOAuthProjection(ev, {
        phase: "device_code",
        userCode: ev.userCode,
        verificationUri: ev.verificationUri,
        intervalSeconds: ev.intervalSeconds,
        expiresInSeconds: ev.expiresInSeconds,
        ...(ev.expiresInSeconds
          ? { expiresAt: updatedAt + ev.expiresInSeconds * 1_000 }
          : {}),
      });
      if (!isReplayedEvent(e) && ev.conversation_id === s.conversationId) {
        pushToast(`${ev.provider} 设备授权码：${ev.userCode}。完整步骤已显示在提供商设置中。`, "info", 12_000);
      }
      return true;
    }
    case "llm.provider.oauth.info": {
      const ev = e as ProviderOAuthInfoEvent & ServerEvent;
      setProviderOAuthProjection(ev, {
        phase: "info",
        message: ev.message,
        links: ev.links,
      });
      if (!isReplayedEvent(e) && ev.conversation_id === s.conversationId) {
        pushToast(ev.message.slice(0, 240), "info", 8_000);
      }
      return true;
    }
    case "llm.provider.oauth.progress": {
      const ev = e as ProviderOAuthProgressEvent & ServerEvent;
      setProviderOAuthProjection(ev, {
        phase: "progress",
        message: ev.message,
      });
      return true;
    }
    case "session.restored":
    case "session.synced": {
      const ev = e as SessionRestoredEvent | SessionSyncedEvent;
      if (
        ev.type === "session.synced"
        && ev.protocol_version
        && ev.protocol_version !== RUNTIME_PROTOCOL_VERSION
      ) {
        s.setConnected(false);
        pushToast(
        `协议版本不匹配：界面为 ${RUNTIME_PROTOCOL_VERSION}，后端为 ${ev.protocol_version}。更新 MiniCode 后请重新加载。`,
          "error",
        );
        return true;
      }
      const authoritativeEpochReset = ev.cursor_reset === true;
      if (authoritativeEpochReset) {
        // A server restart or durable-store rollback can legitimately reuse
        // the same inventory instance with a lower revision. Drop the old
        // comparison barrier before applying the authoritative restore and
        // the conversation.list snapshot requested immediately afterwards.
        useAppStore.setState({
          conversationInventoryInstanceId: null,
          conversationInventoryRevision: 0,
        });
      }
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
      const activeConversationIsHidden = Boolean(
        activeConversation?.archived || activeConversation?.conversation_type === "side_chat",
      );
      const switchEventWillHydrate = ev.type === "session.restored" && ev.conversation_switched_follows === true;
      if (ev.type === "session.restored") {
        pendingAuthoritativeConversationResets.clear();
        if (switchEventWillHydrate && activeConversationId) {
          // session.restore is followed immediately by a canonical
          // conversation.switched payload loaded from the durable repository.
          // It is authoritative even when the event cursor itself did not
          // reset: a renderer may still hold a newer optimistic/local revision
          // from before refresh. Treating only cursor-reset restores as
          // authoritative restores the transcript but drops the matching
          // durable Agent state (for example Provider activity in Context).
          pendingAuthoritativeConversationResets.add(activeConversationId);
        }
      }
      const activeTaskId = stringValue(ev.session?.active_task_id);
      const activeStreamIds = Array.isArray(ev.session?.active_stream_conversation_ids)
        ? ev.session.active_stream_conversation_ids
        : [];
      const hasActiveStream = Boolean(
        activeConversationId
        && (
          activeStreamIds.includes(activeConversationId)
          || (activeStreamIds.length === 0 && activeTaskId)
        )
      );
      if (switchEventWillHydrate || hasActiveStream) {
        buffers.textStreamBuffer.destroy();
        buffers.thinkingStreamBuffer.destroy();
      } else {
        // The session snapshot reports no live stream, so any locally streaming
        // turn was cut off while this client was away. Its real outcome is
        // unknown, so it is sealed as partial: never a fabricated success, and
        // never a fabricated failure for a turn that may have finished fine on
        // the server.
        clearStreamingState(buffers, {
          conversationId: activeConversationId || s.conversationId,
          terminalStatus: "partial",
        });
      }

      if (model) s.setCurrentModel(model);
      const provider = maybeString(ev.provider);
      if (provider) s.setCurrentProvider(provider);
      s.setCurrentProviderMeta({
        providerId: stringValue(ev.provider_id),
        baseUrl: stringValue(ev.base_url),
        wireApi: stringValue(ev.wire_api),
      });
      if (workspaceRoot && !switchEventWillHydrate) s.setWorkingDirectory(workspaceRoot);
            setAvailableModelsForCurrentProvider(ev.available_models, model, provider, maybeString(ev.models_source));
      if (activeConversationIsHidden) {
        clearActiveConversationView();
      } else if (switchEventWillHydrate) {
        // The backend will immediately emit the canonical conversation.switched
        // event. Keep conversation activation on that single path so restore and
        // manual switching cannot diverge.
      } else if (activeConversationId) {
        hydrateActiveConversation(
          activeConversation,
          activeConversationId,
          fallbackMessages,
          { forceAuthoritative: authoritativeEpochReset },
        );
      } else {
        clearActiveConversationView();
      }
      applyRuntimeSessionSnapshot(ev.session);
      if (ev.type === "session.restored" && ev.error) {
      pushToast(`恢复会话时出现警告：${ev.error}`, "warning", 5000);
      }
      if (ev.type === "session.restored" && ev.missed_events) {
      pushToast("任务运行期间连接曾中断，部分事件可能缺失。", "warning", 8000);
        // Replay has a bounded server window. Re-fetch the durable transcript
        // before accepting more deltas so timeline/tool cards cannot remain
        // silently absent after a long disconnect.
        if (activeConversationId) {
          sendClientCommand({ type: "conversation.switch", conversation_id: activeConversationId });
        }
      }
      return true;
    }
    case "conversation.list": {
      const ev = e as ConversationListEvent;
      if (ev.conversations) {
        const incomingConversationMetas = ev.conversations
          .map(toConversationMeta);
        const requestedActiveConversationId = maybeString(ev.active_conversation_id);
        const storeState = useAppStore.getState();
        const incomingInventoryInstanceId = maybeString(ev.inventory_instance_id)?.trim();
        const currentInventoryInstanceId = maybeString(
          storeState.conversationInventoryInstanceId,
        )?.trim();
        const incomingInventoryRevision = normalizedConversationRevision(ev.inventory_revision);
        const inventoryEpochChanged = Boolean(
          incomingInventoryInstanceId
          && incomingInventoryInstanceId !== currentInventoryInstanceId,
        );
        // Once a renderer has observed a durable inventory epoch, an
        // unversioned list cannot replace it. This also rejects malformed
        // half-versioned snapshots defensively if validation is bypassed.
        if (
          (currentInventoryInstanceId && !incomingInventoryInstanceId)
          || (incomingInventoryInstanceId && incomingInventoryRevision === undefined)
          || (!incomingInventoryInstanceId && incomingInventoryRevision !== undefined)
        ) {
          return true;
        }
        if (
          !inventoryEpochChanged
          && (
            (incomingInventoryRevision !== undefined
              && incomingInventoryRevision < storeState.conversationInventoryRevision)
            || (incomingInventoryRevision === undefined
              && storeState.conversationInventoryRevision > 0)
          )
        ) {
          return true;
        }
        const existingById = new Map(storeState.conversations.map((conversation) => [conversation.id, conversation]));
        const snapshotAt = Date.parse(String(ev.snapshot_at || e.timestamp || ""));
        const mergedIncomingMetas = incomingConversationMetas.map((incoming) => {
          const existing = existingById.get(incoming.id);
          if (!existing || inventoryEpochChanged) return incoming;
          return incomingConversationMetaIsStale(incoming, existing) ? existing : incoming;
        });
        const incomingIds = new Set(incomingConversationMetas.map((conversation) => conversation.id));
        const newerThanSnapshot = !incomingInventoryInstanceId
          && incomingInventoryRevision === undefined
          && Number.isFinite(snapshotAt)
          ? storeState.conversations.filter((conversation) => {
              if (incomingIds.has(conversation.id)) return false;
              const updatedAt = Date.parse(String(conversation.updatedAt || ""));
              return Number.isFinite(updatedAt) && updatedAt > snapshotAt;
            })
          : [];
        const conversationMetas = [...mergedIncomingMetas, ...newerThanSnapshot];
        const currentEffectiveActiveConversationId = visibleActiveConversationId(
          storeState.conversationId ?? undefined,
          conversationMetas,
        );
        const requestedEffectiveActiveConversationId = visibleActiveConversationId(
          requestedActiveConversationId,
          conversationMetas,
        );
        // conversation.list is an inventory snapshot, not an activation
        // command. Keep an existing visible active conversation authoritative;
        // switching is committed by conversation.switched/session restore.
        // The list may choose a backend/fallback owner only for cold start or
        // after the current owner was actually removed/archived.
        const fallbackActiveConversationId = fallbackVisibleConversationId(
          storeState.conversationId ?? undefined,
          conversationMetas,
        );
        const effectiveActiveConversationId = currentEffectiveActiveConversationId
          ?? requestedEffectiveActiveConversationId
          ?? fallbackActiveConversationId;
        const activationNeedsBackendSync = Boolean(
          effectiveActiveConversationId
          && !currentEffectiveActiveConversationId
          && !requestedEffectiveActiveConversationId,
        );
        const nextConversationMetas = conversationMetas;
        const knownConversationIds = new Set(nextConversationMetas.map((conversation) => conversation.id));
        const removedConversationIds = storeState.conversations
          .map((conversation) => conversation.id)
          .filter((id) => !knownConversationIds.has(id));
        for (const removedId of removedConversationIds) {
          releasePreviewScope(removedId);
          useAppStore.getState().clearPendingProviderProgress(removedId);
          useAppStore.getState().clearConversationControlPlaneState(removedId);
          if (isDesktop()) {
            void ptyKillConversation(removedId);
          }
        }
        useAppStore.setState((state) => {
          let pendingApproval = state.pendingApproval;
          let approvalQueue = state.approvalQueue;
          let pendingDiffReview = state.pendingDiffReview;
          let diffReviewQueue = state.diffReviewQueue;
          let pendingAskUser = state.pendingAskUser;
          let askUserQueue = state.askUserQueue;
          const removedDiffRequestIds = new Set<string>();
          for (const removedId of removedConversationIds) {
            const approvals = removeConversationOwnedPrompts(pendingApproval, approvalQueue, removedId);
            pendingApproval = approvals.pending;
            approvalQueue = approvals.queue;
            const removedDiffs = [pendingDiffReview, ...diffReviewQueue]
              .filter((item) => item?.conversationId?.trim() === removedId);
            for (const item of removedDiffs) removedDiffRequestIds.add(item!.requestId);
            const diffs = removeConversationOwnedPrompts(pendingDiffReview, diffReviewQueue, removedId);
            pendingDiffReview = diffs.pending;
            diffReviewQueue = diffs.queue;
            const questions = removeConversationOwnedPrompts(pendingAskUser, askUserQueue, removedId);
            pendingAskUser = questions.pending;
            askUserQueue = questions.queue;
          }
          return {
            conversations: nextConversationMetas,
            conversationInventoryInstanceId: incomingInventoryInstanceId
              ?? state.conversationInventoryInstanceId,
            conversationInventoryRevision: incomingInventoryRevision
              ?? (inventoryEpochChanged ? 0 : state.conversationInventoryRevision),
            conversationMessages: Object.fromEntries(
              Object.entries(state.conversationMessages).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationStreaming: Object.fromEntries(
              Object.entries(state.conversationStreaming).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationAgentStates: Object.fromEntries(
              Object.entries(state.conversationAgentStates ?? {}).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationWorkbenchStates: Object.fromEntries(
              Object.entries(state.conversationWorkbenchStates ?? {}).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationRecallTruncations: Object.fromEntries(
              Object.entries(state.conversationRecallTruncations ?? {}).filter(([id]) => knownConversationIds.has(id)),
            ),
            pendingApproval,
            approvalQueue,
            pendingDiffReview,
            diffReviewQueue,
            pendingAskUser,
            askUserQueue,
            diffReview: visibleDiffReviewForConversation(
              effectiveActiveConversationId,
              pendingDiffReview,
              diffReviewQueue,
            ) ?? (state.diffReview && !removedDiffRequestIds.has(state.diffReview.requestId)
              ? state.diffReview
              : null),
          };
        });
        const eventActiveConversation = ev.active_conversation ?? null;
        const activeConversation = eventActiveConversation
          && eventActiveConversation.id === effectiveActiveConversationId
          && !eventActiveConversation.archived
          && eventActiveConversation.conversation_type !== "side_chat"
            ? eventActiveConversation
            : null;
        if (effectiveActiveConversationId) {
          const alreadyActive = storeState.conversationId === effectiveActiveConversationId;
          if (alreadyActive) {
            const activeMeta = conversationMetas.find((conversation) => conversation.id === effectiveActiveConversationId);
            if (activeMeta?.goal !== undefined) {
              useAppStore.getState().setActiveGoal(
                activeMeta.goal ?? null,
                effectiveActiveConversationId,
                activeMeta.revision,
              );
            }
            if (activeConversation) {
              hydrateConversationAgentState(effectiveActiveConversationId, activeConversation);
            }
          } else {
            hydrateActiveConversation(activeConversation, effectiveActiveConversationId, undefined, { upsertMeta: false });
          }
          if (activationNeedsBackendSync) {
            sendClientCommand({ type: "conversation.switch", conversation_id: effectiveActiveConversationId });
          }
        } else {
          clearActiveConversationView();
        }
        applyRuntimeSessionSnapshot(ev.session);
      }
      return true;
    }
    case "goal.updated": {
      const ev = e as GoalUpdatedEvent & { goal?: GoalInfo | null; updated_at?: string };
      const conversationId = maybeString(ev.conversation_id);
      if (!conversationId) return true;
      const incomingGoal = toConversationGoal(ev.goal);
      const currentConversation = useAppStore.getState().conversations.find(
        (conversation) => conversation.id === conversationId,
      );
      const currentGoal = currentConversation?.goal;
      const incomingRevision = normalizedConversationRevision(ev.revision);
      const currentRevision = normalizedConversationRevision(currentConversation?.revision);
      const incomingUpdatedAt = Date.parse(String(incomingGoal?.updatedAt || ev.updated_at || ""));
      const currentUpdatedAt = Date.parse(String(
        currentGoal?.updatedAt || currentConversation?.updatedAt || "",
      ));
      // Goal updates are durable state, not append-only notifications.  A
      // delayed replay from before a newer edit must not regress the visible
      // goal or overwrite the conversation metadata with stale text.
      if (
        (incomingRevision !== undefined
          && currentRevision !== undefined
          && incomingRevision < currentRevision)
        || (currentRevision !== undefined && incomingRevision === undefined)
        || (
          incomingRevision === undefined
          && currentRevision === undefined
          && Number.isFinite(incomingUpdatedAt)
          && Number.isFinite(currentUpdatedAt)
          && incomingUpdatedAt < currentUpdatedAt
        )
      ) {
        return true;
      }
      useAppStore.getState().setActiveGoal(incomingGoal, conversationId, incomingRevision);
      return true;
    }
    case "conversation.switched": {
      const ev = e as ConversationSwitchedEvent;
      const switchedConversationId = maybeString(ev.conversation_id) ?? ev.conversation?.id;
      const forceAuthoritative = Boolean(
        switchedConversationId
        && pendingAuthoritativeConversationResets.delete(switchedConversationId),
      );
      if (switchedConversationId) {
        useAppStore.getState().setConversationHydration(
          switchedConversationId,
          ev.is_hydrating === true,
          Date.now(),
        );
      }
      if (ev.conversation?.archived || ev.conversation?.conversation_type === "side_chat") {
        clearActiveConversationView();
      } else if (ev.conversation) {
        const nextConversationId = switchedConversationId ?? ev.conversation.id;
        const activeStreamIds = Array.isArray(ev.session?.active_stream_conversation_ids)
          ? ev.session.active_stream_conversation_ids
          : [];
        const preserveStreamingAssistant = activeStreamIds.includes(nextConversationId)
          || (activeStreamIds.length === 0 && Boolean(ev.session?.active_task_id));
        hydrateActiveConversation(ev.conversation, nextConversationId, undefined, {
          preserveStreamingAssistant,
          forceAuthoritative,
        });
      }
      applyRuntimeSessionSnapshot(ev.session);
      if (!isReplayedEvent(e)) {
        // Extension/project commands are conversation-scoped. A switch must
        // replace the palette even when the transport itself did not reconnect.
        sendClientCommand({ type: "commands.list" });
      }
      return true;
    }
    default:
      return false;
  }
};
