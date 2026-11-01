import { useAppStore } from "../stores";
import { composerDraftOf, composerWorkspaceKey, emptyComposerDraft, isComposerConversationDeleted } from "../stores/composer-drafts";
import { selectPendingDraftUserMessage } from "../stores/conversation-drafts";
import { captureCommittedUserMessage, capturePendingUserMessageQueueUpdate } from "./sendChatMessage";
import { subagentModelPatch } from "../lib/subagent-model";
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
import { isRequestedForegroundNavigation, isSupersededCommandProjection } from "../protocol/command-projection";
import type { StreamBuffer } from "../lib/stream-buffer";
import { pushToast } from "../overlays/ToastContainer";
import {
  hydrateMessages,
  normalizeMessageContextRefs,
  normalizeQuotedMessage,
  normalizeTranscriptAttachmentRefs,
  type BackendTranscriptMessage,
} from "./transcriptHydration";
import type {
  AgentProgressEntry,
  ChatMessage,
  ConversationAgentState,
  PlanState,
  SubagentMessageState,
  SubagentState,
  TodoItem,
  ProviderOAuthFlowProjection,
  InspectorEntry,
} from "../stores/types";
import { toConversationGoal } from "../stores/types";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { mergeCapabilities } from "../protocol/capabilities";
import { workspaceRootsEqual } from "../lib/workspace-path";
import {
  conversationResetPayload,
  conversationWorkspacePath,
  LS,
  readLS,
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
import { incomingConversationMetaIsStale, isVisibleConversationMeta, normalizedConversationRevision } from "./activeConversation";
import { modelSelectionPatch, runtimeModelSelectionPatch } from "../lib/model-selection";
import { releaseConversationReadingState } from "./transcriptReadingState";

type ConversationSummary = ConversationSummaryPayload;
type ConversationPayload = ConversationRecordPayload;
const UI_AGENT_STATE_KEY = "ui_agent_state";
const RUNTIME_PROTOCOL_VERSION = "1.0.0";
const pendingAuthoritativeConversationResets = new Set<string>();
const deferredSwitchCatalogs = new Map<string, { cancel: () => void }>();


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
  if (isComposerConversationDeleted(conversationId)) return;
  capturePendingUserMessageQueueUpdate(event);
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
    const source = event.status === "queued" && event.input
      && !messages.some(message => message.id === event.user_message_id || message.queueMessageId === event.message_id)
      ? [...messages, { id: event.user_message_id || `user_${event.message_id}`, role: "user" as const,
          content: event.input.display_content ?? event.input.content, artifacts: [], timestamp: Date.now() },
        { id: event.message_id, role: "assistant" as const, content: "", artifacts: [], timestamp: Date.now() }]
      : messages;
    const updated = source
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
          ...(isUser && event.input ? {
            content: event.input.display_content ?? event.input.content,
            backendContent: event.input.content,
            attachmentRefs: normalizeTranscriptAttachmentRefs(event.input.attachments),
            contextRefs: normalizeMessageContextRefs(event.input.context_refs),
            quotedMessage: normalizeQuotedMessage(event.input.quoted_message),
            codeReview: event.input.code_review,
            asyncRequestId: event.input.async_request_id,
          } : {}),
          queueState: "queued" as const,
          queuePosition: event.position,
          queuePaused: event.paused ?? false,
          queueMessageId: event.message_id,
          ...(isAssistant ? { isStreaming: false, isThinkingStreaming: false } : {}),
        };
      }
      if (event.status === "dequeued") {
        return {
          ...message,
          queueState: undefined,
          queuePosition: undefined,
          queuePaused: undefined,
          ...(isUser && steeredCurrentTurn
            ? { steeredIntoMessageId: steerTargetMessageId }
            : {}),
        };
      }
      return {
        ...message,
        queueState: "cancelled" as const,
        queuePosition: undefined,
      };
    });
    return updated;
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
            isStreaming: thread.isStreaming,
          },
        },
      };
    }

    const isActive = conversationId === state.conversationId;
    const source = isActive ? state.messages : state.conversationMessages[conversationId] ?? [];
    const messages = updateMessages(source);
    const streaming = messages.some((message) => message.isStreaming || message.isThinkingStreaming);
    return {
      ...(isActive ? {
        messages,
        isStreaming: streaming,
      } : {}),
      conversationMessages: {
        ...state.conversationMessages,
        [conversationId]: messages,
      },
      conversationStreaming: {
        ...state.conversationStreaming,
        [conversationId]: streaming,
      },
    };
  });
};

const toConversationMeta = (conversation: ConversationSummary) => ({
  id: conversation.id,
  title: maybeString(conversation.title) ?? "未命名",
  updatedAt: maybeString(conversation.updated_at) ?? new Date().toISOString(),
  createdAt: maybeString(conversation.created_at),
  revision: Number.isSafeInteger(conversation.revision) && Number(conversation.revision) >= 0
    ? Number(conversation.revision)
    : undefined,
  summary: maybeString(conversation.summary),
  compactionState: maybeString(conversation.compaction_state),
  conversationType: conversation.conversation_type === "side_chat" ? "side_chat" as const : "main" as const,
  threadSource: conversation.thread_source,
  archived: conversation.archived,
  pinned: conversation.pinned,
  pinnedAt: conversation.pinned_at,
  resourceReferences: conversation.resource_references,
  hasUnreadTurn: conversation.has_unread_turn,
  projectId: conversation.project_id,
  memoryMode: maybeString(conversation.memory_mode),
  memoryPolluted: conversation.memory_polluted === true,
  memoryPollutionSources: Array.isArray(conversation.memory_pollution_sources)
    ? conversation.memory_pollution_sources.filter((source): source is string => typeof source === "string" && Boolean(source.trim()))
    : [],
  workspaceRoot: maybeString(conversation.workspace_root),
  executionCwd: maybeString(conversation.execution_cwd),
  projectlessOutputDir: maybeString(conversation.projectless_output_dir),
  projectlessBrowserRoot: maybeString(conversation.projectless_browser_root),
  gitBranch: maybeString(conversation.git_branch),
  worktreePath: maybeString(conversation.worktree_path),
  gitIsolated: conversation.git_isolated,
  agentMode: conversation.agent_mode,
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
      const currentTool = subagent.currentTool ?? subagent.current_tool ?? subagent.tool_name;
      const lastProgressAt = subagent.lastProgressAt ?? subagent.last_progress_at;
      const acceptedAt = subagent.acceptedAt ?? subagent.accepted_at;
      const startedAt = subagent.startedAt ?? subagent.started_at;
      const completedAt = subagent.completedAt ?? subagent.completed_at;
      return {
        id: String(subagent.id ?? subagent.subagent_id ?? "").trim(),
        ...subagentModelPatch(subagent),
        role: String(subagent.role ?? "subagent"),
        status: SUBAGENT_STATUSES.has(status) ? status : "running",
        cleanupPending: typeof (subagent.cleanupPending ?? subagent.cleanup_pending) === "boolean"
          ? Boolean(subagent.cleanupPending ?? subagent.cleanup_pending)
          : undefined,
        cleanupReason: maybeString((subagent.cleanupReason ?? subagent.cleanup_reason) as string | null | undefined),
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
        currentTool: typeof currentTool === "string" ? currentTool : undefined,
        lastProgressAt: typeof lastProgressAt === "number" ? lastProgressAt : undefined,
        acceptedAt: typeof acceptedAt === "number" ? acceptedAt : undefined,
        startedAt: typeof startedAt === "number" ? startedAt : undefined,
        completedAt: typeof completedAt === "number" ? completedAt : undefined,
        detail: normalizeDelegatedText(subagent.detail),
        nodeId: maybeString((subagent.nodeId ?? subagent.node_id) as string | null | undefined),
        taskId: maybeString((subagent.taskId ?? subagent.task_id) as string | null | undefined),
        dependsOn: stringList(subagent.dependsOn ?? subagent.depends_on),
        blockedBy: stringList(subagent.blockedBy ?? subagent.blocked_by),
        objective: maybeString(subagent.objective as string | null | undefined),
        currentActivity: normalizeDelegatedText(subagent.currentActivity ?? subagent.current_activity),
        waitingOn: maybeString((subagent.waitingOn ?? subagent.waiting_on) as string | null | undefined),
        needsInput: typeof (subagent.needsInput ?? subagent.needs_input) === "boolean"
          ? Boolean(subagent.needsInput ?? subagent.needs_input) : undefined,
        teammateName: maybeString((subagent.teammateName ?? subagent.teammate_name) as string | null | undefined),
        displayName: maybeString((subagent.displayName ?? subagent.display_name) as string | null | undefined),
        workspaceRoot: maybeString((subagent.workspaceRoot ?? subagent.workspace_root) as string | null | undefined),
        teamName: maybeString((subagent.teamName ?? subagent.team_name) as string | null | undefined),
        awaitingPlanApproval: typeof (subagent.awaitingPlanApproval ?? subagent.awaiting_plan_approval) === "boolean"
          ? Boolean(subagent.awaitingPlanApproval ?? subagent.awaiting_plan_approval) : undefined,
        activePlanRequestId: maybeString((subagent.activePlanRequestId ?? subagent.active_plan_request_id) as string | null | undefined),
        isIdle: typeof (subagent.isIdle ?? subagent.is_idle) === "boolean"
          ? Boolean(subagent.isIdle ?? subagent.is_idle) : undefined,
        readOnly: typeof (subagent.readOnly ?? subagent.read_only) === "boolean"
          ? Boolean(subagent.readOnly ?? subagent.read_only) : undefined,
        writeScope: stringList(subagent.writeScope ?? subagent.write_scope),
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
      const statusCode = progress.statusCode ?? progress.status_code;
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
        statusCode: typeof statusCode === "number" && Number.isInteger(statusCode) && statusCode >= 100 && statusCode <= 599 ? statusCode : undefined,
        providerErrorCode: maybeString((progress.providerErrorCode ?? progress.provider_error_code) as string | null | undefined),
        operationId: maybeString((progress.operationId ?? progress.operation_id) as string | null | undefined),
        subagentId: maybeString((progress.subagentId ?? progress.subagent_id) as string | null | undefined),
        subagentName: maybeString((progress.subagentName ?? progress.subagent_name) as string | null | undefined),
        subagentIdentity: maybeString((progress.subagentIdentity ?? progress.subagent_identity) as string | null | undefined),
        subagentStatus: (progress.subagentStatus ?? progress.subagent_status) as SubagentState["status"] | undefined,
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
  const parentStreaming = hasStreamingAssistantForConversation(conversationId);
  useAppStore.setState((state) => {
    const previousState = state.conversationId === conversationId
      ? { plan: state.plan, todos: state.todos, subagents: state.subagents, agentProgress: state.agentProgress }
      : state.conversationAgentStates[conversationId] ?? { plan: null, todos: [], subagents: [], agentProgress: [] };
    const previousAgents = previousState.subagents;
    const restoredAgents = agentState.subagents.map(agent => {
      const previous = previousAgents.find(candidate => candidate.id === agent.id);
      const sameIncarnation = previous && previous.agentPath === agent.agentPath && previous.mailboxEpoch === agent.mailboxEpoch;
      if (parentStreaming && previous) {
        if (previous.agentPath && typeof previous.mailboxEpoch === "number") {
          if (!agent.agentPath || typeof agent.mailboxEpoch !== "number"
            || agent.mailboxEpoch < previous.mailboxEpoch
            || (agent.mailboxEpoch === previous.mailboxEpoch && agent.agentPath !== previous.agentPath)) return previous;
          if (sameIncarnation
            && ["done", "partial", "cancelled", "error"].includes(previous.status)
            && !["done", "partial", "cancelled", "error"].includes(agent.status)) return previous;
        }
        if (sameIncarnation && previous.completedAt !== undefined && agent.completedAt !== undefined
          && agent.completedAt < previous.completedAt) return previous;
      }
      // Durable UI snapshots carry metadata, while transcript pushes own the
      // working record. A return to the same incarnation must retain that record.
      return sameIncarnation
        ? { ...agent, displayName: agent.displayName ?? previous.displayName,
          teammateName: agent.teammateName ?? previous.teammateName,
          transcriptMessages: previous.transcriptMessages, transcriptSeq: previous.transcriptSeq }
        : agent;
    });
    // A live parent owns its current plan/progress, not the child's lifecycle.
    // Keep locally observed children omitted by an older snapshot while
    // accepting durable child completion and newer incarnations independently.
    const subagents = parentStreaming
      ? [...previousAgents.map(previous => restoredAgents.find(agent => agent.id === previous.id) ?? previous),
        ...restoredAgents.filter(agent => !previousAgents.some(previous => previous.id === agent.id))]
      : restoredAgents;
    const hydrated = { ...(parentStreaming ? previousState : agentState), subagents };
    return {
      ...(state.conversationId === conversationId ? hydrated : {}),
      conversationAgentStates: { ...state.conversationAgentStates, [conversationId]: hydrated },
    };
  });
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

type QueuedSnapshotEntry = NonNullable<RuntimeSessionSnapshot["queued_user_messages"]>[number];

const applyQueuedSnapshotToMessages = (messages: ChatMessage[], entry: QueuedSnapshotEntry): ChatMessage[] => {
  const current = [...messages];
  const messageId = String(entry.message_id || "").trim();
  const userMessageId = String(entry.user_message_id || `user_${messageId}`).trim();
  const position = typeof entry.position === "number" ? entry.position : undefined;
  const queuePaused = entry.paused ?? false;
  const backendContent = String(entry.content ?? "");
  const input = {
    content: entry.display_content ?? backendContent,
    ...(entry.async_request_id !== undefined ? { asyncRequestId: entry.async_request_id } : {}),
    ...(entry.display_content !== undefined ? { backendContent } : {}),
    ...(entry.context_refs !== undefined ? { contextRefs: normalizeMessageContextRefs(entry.context_refs) } : {}),
    ...(entry.quoted_message !== undefined ? { quotedMessage: normalizeQuotedMessage(entry.quoted_message) } : {}),
    ...(entry.attachments !== undefined ? { attachmentRefs: normalizeTranscriptAttachmentRefs(entry.attachments) } : {}),
  };
  const userIndex = current.findIndex((message) => message.role === "user"
    && (message.id === userMessageId || message.queueMessageId === messageId));
  const timestamp = Date.now();
  if (userIndex >= 0) {
    current[userIndex] = {
      ...current[userIndex],
      ...input,
      ...(entry.display_content === undefined ? { content: current[userIndex].content } : {}),
      queueState: "queued",
      queuePosition: position,
      queuePaused,
      queueMessageId: messageId,
    };
  } else {
    const queuedAssistantIndex = current.findIndex((message) => message.id === messageId);
    current.splice(queuedAssistantIndex >= 0 ? queuedAssistantIndex : current.length, 0, {
      id: userMessageId,
      role: "user",
      ...input,
      artifacts: [],
      timestamp,
      queueState: "queued",
      queuePosition: position,
      queuePaused,
      queueMessageId: messageId,
    });
  }
  const assistantIndex = current.findIndex((message) => message.id === messageId);
  if (assistantIndex >= 0) {
    current[assistantIndex] = {
      ...current[assistantIndex],
      queueState: "queued",
      queuePosition: position,
      queuePaused,
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
      queuePaused,
      queueMessageId: messageId,
      isStreaming: false,
    });
  }
  return current;
};

export const applyQueuedUserMessageSnapshot = (
  entries: RuntimeSessionSnapshot["queued_user_messages"],
) => {
  if (!Array.isArray(entries) || entries.length === 0) return;
  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    let activeMessages = state.messages;
    let sideChats = state.sideChats;
    for (const entry of entries) {
      const conversationId = String(entry?.conversation_id || "").trim();
      const messageId = String(entry?.message_id || "").trim();
      if (!conversationId || !messageId) continue;
      if (isComposerConversationDeleted(conversationId)) continue;
      const base = conversationMessages[conversationId] ? [...conversationMessages[conversationId]]
        : state.conversationId === conversationId ? [...state.messages] : [];
      const current = applyQueuedSnapshotToMessages(base, entry);
      conversationMessages[conversationId] = current;
      if (state.conversationId === conversationId) activeMessages = current;
      // The visible side chat renders its own message list; the authoritative
      // queue snapshot must reach it too, not only the hidden cache.
      const sideThread = sideChats[conversationId];
      if (sideThread) {
        sideChats = { ...sideChats, [conversationId]: {
          ...sideThread, messages: applyQueuedSnapshotToMessages(sideThread.messages, entry) } };
      }
    }
    return { conversationMessages, messages: activeMessages, sideChats };
  });
  for (const entry of entries) if (entry.conversation_id && entry.message_id && entry.user_message_id
    && !isComposerConversationDeleted(entry.conversation_id)) captureCommittedUserMessage(entry.conversation_id, entry.user_message_id);
};

type PendingTurnInputSnapshotEntry = NonNullable<RuntimeSessionSnapshot["pending_turn_inputs"]>[number];

const applySteerSnapshotToMessages = (messages: ChatMessage[], entry: PendingTurnInputSnapshotEntry): ChatMessage[] => {
  const messageId = String(entry.message_id || "").trim();
  const userMessageId = String(entry.user_message_id || `user_${messageId}`).trim();
  const targetMessageId = String(entry.target_message_id || "").trim();
  const withoutPlaceholder = messages.filter((message) => !(
    message.role === "assistant" && message.id === messageId
  ));
  const existingUserIndex = withoutPlaceholder.findIndex((message) => (
    message.role === "user"
    && (message.id === userMessageId || message.queueMessageId === messageId)
  ));
  const attachmentRefs = normalizeTranscriptAttachmentRefs(entry.attachments);
  const backendContent = String(entry.content ?? "");
  const input = {
    content: entry.display_content ?? backendContent,
    ...(entry.async_request_id !== undefined ? { asyncRequestId: entry.async_request_id } : {}),
    ...(entry.display_content !== undefined ? { backendContent } : {}),
    ...(entry.context_refs !== undefined ? { contextRefs: normalizeMessageContextRefs(entry.context_refs) } : {}),
    ...(entry.quoted_message !== undefined ? { quotedMessage: normalizeQuotedMessage(entry.quoted_message) } : {}),
  };
  const restoredUser: ChatMessage = existingUserIndex >= 0
    ? {
        ...withoutPlaceholder[existingUserIndex]!,
        ...input,
        ...(entry.display_content === undefined ? { content: withoutPlaceholder[existingUserIndex]!.content } : {}),
        queueState: undefined,
        queuePosition: undefined,
        queueMessageId: messageId,
        steeredIntoMessageId: targetMessageId || undefined,
        ...(entry.attachments !== undefined ? { attachmentRefs } : {}),
      }
    : {
        id: userMessageId,
        role: "user",
        ...input,
        attachmentRefs,
        artifacts: [],
        timestamp: Number(entry.queued_at_ms || Date.now()),
        queueMessageId: messageId,
        steeredIntoMessageId: targetMessageId || undefined,
      };
  if (existingUserIndex >= 0) withoutPlaceholder[existingUserIndex] = restoredUser;
  else withoutPlaceholder.push(restoredUser);
  return withoutPlaceholder;
};

export const applyPendingTurnInputSnapshot = (
  entries: RuntimeSessionSnapshot["pending_turn_inputs"],
) => {
  if (!Array.isArray(entries) || entries.length === 0) return;
  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    let activeMessages = state.messages;
    let sideChats = state.sideChats;
    for (const entry of entries) {
      if (entry?.mode !== "steer") continue;
      const conversationId = String(entry.conversation_id || "").trim();
      const messageId = String(entry.message_id || "").trim();
      const userMessageId = String(entry.user_message_id || `user_${messageId}`).trim();
      if (!conversationId || !messageId || !userMessageId) continue;
      if (isComposerConversationDeleted(conversationId)) continue;
      const base = conversationMessages[conversationId]
        ? [...conversationMessages[conversationId]]
        : state.conversationId === conversationId
          ? [...state.messages]
          : [];
      const restored = applySteerSnapshotToMessages(base, entry);
      conversationMessages[conversationId] = restored;
      if (state.conversationId === conversationId) activeMessages = restored;
      // The visible side chat renders its own message list; the authoritative
      // steer snapshot must reach it too, not only the hidden cache.
      const sideThread = sideChats[conversationId];
      if (sideThread) {
        sideChats = { ...sideChats, [conversationId]: {
          ...sideThread, messages: applySteerSnapshotToMessages(sideThread.messages, entry) } };
      }
    }
    return { conversationMessages, messages: activeMessages, sideChats };
  });
  for (const entry of entries) if (entry.mode === "steer" && entry.conversation_id && entry.message_id && entry.user_message_id
    && !isComposerConversationDeleted(entry.conversation_id)) captureCommittedUserMessage(entry.conversation_id, entry.user_message_id);
};

export const applyActiveStreamSnapshot = (session: RuntimeSessionSnapshot) => {
  // The backend always emits this field as the authoritative set of live
  // conversation runs. An empty array stops stale running presentation after
  // a disconnect; it does not supply the missing turn/tool outcome. Older
  // snapshots that omit the field remain non-authoritative.
  if (!Array.isArray(session.active_stream_conversation_ids)) return;
  const activeStreamIds = session.active_stream_conversation_ids
    .map((id) => String(id || "").trim())
    .filter(id => id && !isComposerConversationDeleted(id));
  const activeStreamSet = new Set(activeStreamIds);
  const legacyActiveConversationId = String(session.active_conversation_id || "").trim();
  if (
    activeStreamSet.size === 0
    && String(session.active_task_id || "").trim()
    && legacyActiveConversationId
    && !isComposerConversationDeleted(legacyActiveConversationId)
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
    let latestUserIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "user" && !messages[index].queueState && !messages[index].steeredIntoMessageId) {
        latestUserIndex = index;
        break;
      }
    }
    let liveIndex = -1;
    // A conversation-level running flag cannot revive a checkpoint from a
    // previous user turn. Its exact current assistant arrives in run-start or
    // stream_resume; retain earlier partial evidence while waiting for it.
    for (let index = messages.length - 1; index > latestUserIndex; index -= 1) {
      const message = messages[index];
      if (message.role !== "assistant" || message.queueState) continue;
      if (message.isStreaming || message.isThinkingStreaming) {
        liveIndex = index;
        break;
      }
    }
    return messages.map((message, index) => {
      const ownsActiveStream = message.role === "assistant"
        && !message.queueState
        && ((targets?.has(message.id) ?? false) || (!targets?.size && index === liveIndex));
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
      if (isComposerConversationDeleted(conversationId)) continue;
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
    activate?: boolean;
  } = {},
) => {
  const conversationId = maybeString(activeConversationId) || conversation?.id || "";
  if (!conversationId) return;
  const previousWorkspace = useAppStore.getState().workingDirectory;
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
  const selection = useAppStore.getState();
  const activate = options.activate !== false;
  const selectedDraft = selection.activeConversationDraftId ? selection.conversationDrafts[selection.activeConversationDraftId] : undefined;
  const adoptingDraft = selectedDraft?.conversationId === conversationId
    && workspaceRootsEqual(selectedDraft.executionCwd || selectedDraft.workspaceRoot, conversationWorkspacePath(selection.conversations.find(item => item.id === conversationId)));
  const nextConversation = selection.conversations.find(item => item.id === conversationId);
  // Hydration completion refreshes the current snapshot; it is not another
  // navigation and must not reset and restore every conversation-owned slice.
  if (activate && (selection.conversationId !== conversationId || selection.pendingConversationSwitchId === conversationId
    || !workspaceRootsEqual(selection.workingDirectory, conversationWorkspacePath(nextConversation)))) {
    selection.applyConversationSwitched({ conversationId });
  }
  const modeMeta = useAppStore.getState().conversations.find(item => item.id === conversationId);
  if (modeMeta?.agentMode !== undefined) useAppStore.getState().syncConversationAgentMode(modeMeta.agentMode, conversationId, undefined, modeMeta.revision);
  if (activate && !workspaceRootsEqual(previousWorkspace, useAppStore.getState().workingDirectory)) {
    useAppStore.setState({ ...(!adoptingDraft ? { availableSkills: [], slashCommands: [], runtimeCapabilities: null } : {}),
      scheduledTasks: [], scheduledTaskRuns: [] });
  }
  if (conversation && !staleSnapshot) {
    useAppStore.getState().setActiveGoal(
      toConversationGoal(conversation.goal),
      conversationId,
      normalizedConversationRevision(conversation.revision),
    );
  }
  const transcript = conversation?.messages ?? conversation?.transcript ?? fallbackMessages;
  if (conversation && (!staleSnapshot || !useAppStore.getState().conversationHistoryPages[conversationId])) {
    const page = conversation.transcript_page;
    useAppStore.setState((state) => ({
      conversationHistoryPages: { ...state.conversationHistoryPages, [conversationId]: {
        beforeMessageId: page?.before_message_id ?? "",
        hasMore: page?.has_more ?? false,
        loading: false,
      } },
    }));
  }
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
        { activate, isStreaming: cachedMessages.some((message) => Boolean(message.isStreaming)), confirmedUserMessageIds: [] },
      );
    } else {
      const hydrated = hydrateMessages(transcript);
      const messages = options.preserveStreamingAssistant || staleSnapshot
        ? mergeHydratedWithStreamingAssistants(conversationId, hydrated)
        : hydrated;
      useAppStore.getState().hydrateConversationMessages(
        conversationId,
        messages,
        { activate, isStreaming: messages.some((message) => Boolean(message.isStreaming)),
          confirmedUserMessageIds: hydrated.filter(message => message.role === "user").map(message => message.id) },
      );
    }
  }
  restoreProviderInspectorFromTranscript(
    conversationId,
    useAppStore.getState().conversationMessages[conversationId] ?? [],
  );
  // Agent state and transcript describe one durable snapshot.  Apply the
  // transcript first so a stale local streaming placeholder cannot suppress a
  // completed plan/progress snapshot; genuinely live streams are preserved by
  // the merge above and still block stale agent-state replacement.
  if (!staleSnapshot) hydrateConversationAgentState(conversationId, conversation);

  const workspaceRoot = maybeString(conversation?.execution_cwd) || maybeString(conversation?.worktree_path) || maybeString(conversation?.workspace_root);
  if (activate && conversation && !staleSnapshot) {
    // Project membership stays in workspace_root. File operations, attachments
    // and terminals follow the conversation's persisted execution directory.
    useAppStore.getState().setWorkingDirectory(workspaceRoot || "");
  }
};

const restoreProviderInspectorFromTranscript = (
  conversationId: string,
  messages: ChatMessage[],
) => {
  const entries: InspectorEntry[] = [];
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
      entries.push({
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
  useAppStore.getState().addInspectorEntries(entries);
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
  return active?.executionCwd || active?.worktreePath || active?.workspaceRoot || "";
};

const clearActiveConversationView = (options: { preserveMode?: boolean } = {}) => {
  const state = useAppStore.getState();
  state.snapshotWorkbenchState();
  const composer = state.conversationId
    ? state.workspaceComposerDrafts[composerWorkspaceKey(state.workingDirectory)] ?? emptyComposerDraft()
    : composerDraftOf(state);
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
    ...composer,
    actionChip: null,
    mentionResults: [],
    slashPanelOpen: false,
    mentionPanelOpen: false,
    prMonitor: null,
    prStatusIssue: null,
    prStatus: null,
    ciChecks: [],
    ...(hasCodeContext && state.conversationId && !options.preserveMode ? { appMode: "code" as const } : {}),
  });
};

export const applyRuntimeSessionSnapshot = (session: RuntimeSessionSnapshot | undefined | null, eventOwner: string | null,
  event?: ServerEvent, partial = false) => {
  if (!session) return;
  const state = useAppStore.getState();
  const permissionOwner = session.active_conversation_id === undefined
    ? (partial ? state.runtimeSession?.active_conversation_id : eventOwner) : session.active_conversation_id;
  if (permissionOwner && isComposerConversationDeleted(permissionOwner)) return;
  // A delayed navigation snapshot still hydrates its conversation above; it
  // cannot replace a different reader's current session or global run set.
  const restoresPendingOwner = event?.type === "session.restored" && Boolean(state.pendingConversationSwitchId)
    && state.pendingConversationSwitchId === permissionOwner
    && !isSupersededCommandProjection(event);
  if (state.conversationId && permissionOwner !== state.conversationId && !restoresPendingOwner) return;
  const runtimeSession = partial ? { ...state.runtimeSession, ...session } : session;
  state.setRuntimeSession({ ...runtimeSession,
    ...(runtimeSession.active_stream_conversation_ids ? { active_stream_conversation_ids: runtimeSession.active_stream_conversation_ids.filter(id => !isComposerConversationDeleted(id)) } : {}),
    ...(runtimeSession.queued_user_messages ? { queued_user_messages: runtimeSession.queued_user_messages.filter(input => !isComposerConversationDeleted(input.conversation_id)) } : {}),
    ...(runtimeSession.pending_turn_inputs ? { pending_turn_inputs: runtimeSession.pending_turn_inputs.filter(input => !isComposerConversationDeleted(input.conversation_id)) } : {}),
  });
  const projectsCurrentOwner = !state.activeConversationDraftId;
  if (projectsCurrentOwner && session.active_conversation_id === state.conversationId && session.workspace_root !== undefined) {
    state.setWorkingDirectory(session.workspace_root ?? "");
  }
  if (projectsCurrentOwner) useAppStore.setState(runtimeModelSelectionPatch(session, useAppStore.getState()));
  applyQueuedUserMessageSnapshot(session.queued_user_messages);
  applyPendingTurnInputSnapshot(session.pending_turn_inputs);
  applyActiveStreamSnapshot(session);
  if (session.agent_mode && permissionOwner && permissionOwner === state.conversationId) {
    state.syncConversationAgentMode(session.agent_mode, permissionOwner,
      event?.client_command_type === "conversation.agent_mode.set" ? event.client_command_id : undefined,
      session.agent_mode_revision);
  }
  if (projectsCurrentOwner && session.permission_mode && (permissionOwner || null) === (state.conversationId || null)
    && (session.workspace_root === undefined || workspaceRootsEqual(session.workspace_root ?? "", useAppStore.getState().workingDirectory))) {
    useAppStore.setState({ permissionMode: fromBackendPermissionMode(session.permission_mode) });
  }
  if (projectsCurrentOwner && session.capabilities && session.active_conversation_id === state.conversationId
    && workspaceRootsEqual(session.workspace_root ?? "", useAppStore.getState().workingDirectory)) {
    const current = useAppStore.getState().runtimeCapabilities;
    const capabilities = mergeCapabilities(session.capabilities, current ?? undefined) ?? null;
    useAppStore.getState().setRuntimeCapabilities(capabilities);
    if (Array.isArray(capabilities?.skills)) {
      useAppStore.getState().setAvailableSkills(normalizeSkillList(capabilities.skills, session.workspace_root ?? ""));
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
      if (ev.conversation_id && s.sideChats[ev.conversation_id]) {
        const id = ev.conversation_id;
        const model = stringValue(ev.current_model) || stringValue(ev.model);
        useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [id]: { ...state.sideChats[id], model } } }));
        return true;
      }
      if (s.activeConversationDraftId) return true;
      if (ev.conversation_id !== undefined && (ev.conversation_id || null) !== (s.conversationId || null)) return true;
      const model = stringValue(ev.current_model) || stringValue(ev.model);
      useAppStore.setState(modelSelectionPatch(ev));
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
          pinnedConversationIds: [],
          pinnedConversationSortMode: "manual",
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
      const pendingNavigation = useAppStore.getState();
      const pendingConversationId = pendingNavigation.pendingConversationSwitchId ?? pendingNavigation.pendingConversationCreateId;
      const activate = !pendingNavigation.activeConversationDraftId
        && !isSupersededCommandProjection(e)
        && (!pendingConversationId || activeConversationId === pendingConversationId);
      const activeConversationIsHidden = Boolean(
        activeConversation?.archived || activeConversation?.conversation_type === "side_chat",
      );
      const switchEventWillHydrate = ev.type === "session.restored" && ev.conversation_switched_follows === true;
      if (ev.type === "session.restored") {
        if (activate && switchEventWillHydrate && activeConversationId) {
          useAppStore.setState({ pendingConversationSwitchId: activeConversationId });
        }
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
      // Flush pending text before projecting ownership. An idle session is not
      // a turn outcome: only the durable transcript or a terminal event can
      // seal its tools and reject late same-turn evidence.
      buffers.textStreamBuffer.destroy();
      buffers.thinkingStreamBuffer.destroy();

      if (activate) {
        if (model) s.setCurrentModel(model);
        const provider = maybeString(ev.provider);
        if (provider) s.setCurrentProvider(provider);
        s.setCurrentProviderMeta({ providerId: stringValue(ev.provider_id), baseUrl: stringValue(ev.base_url), wireApi: stringValue(ev.wire_api) });
        if (workspaceRoot && !switchEventWillHydrate) s.setWorkingDirectory(workspaceRoot);
        setAvailableModelsForCurrentProvider(ev.available_models, model, provider, maybeString(ev.models_source));
      }
      if (!activate) {
        if (activeConversationId) hydrateActiveConversation(activeConversation, activeConversationId, fallbackMessages,
          { activate: false, forceAuthoritative: authoritativeEpochReset, preserveStreamingAssistant: hasActiveStream });
      } else if (activeConversationIsHidden) {
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
          { forceAuthoritative: authoritativeEpochReset, preserveStreamingAssistant: hasActiveStream },
        );
      } else {
        clearActiveConversationView();
      }
      applyRuntimeSessionSnapshot(ev.session, activeConversationId, e);
      if (ev.type === "session.restored" && ev.error) {
      pushToast(`恢复会话时出现警告：${ev.error}`, "warning", 5000);
      }
      if (ev.type === "session.restored" && ev.missed_events) {
      pushToast("任务运行期间连接曾中断，部分事件可能缺失。", "warning", 8000);
        // Replay has a bounded server window. Re-fetch the durable transcript
        // before accepting more deltas so timeline/tool cards cannot remain
        // silently absent after a long disconnect.
        if (activate && activeConversationId) {
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
        if (ev.sidebar_sections) window.dispatchEvent(new CustomEvent("minicode-sidebar-sections", { detail: ev.sidebar_sections }));
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
        // A backend owner can hydrate on cold start. A local fallback must
        // wait for conversation.switched before becoming active.
        const fallbackActiveConversationId = fallbackVisibleConversationId(
          storeState.conversationId ?? undefined,
          conversationMetas,
        );
        const activationNeedsBackendSync = Boolean(
          fallbackActiveConversationId
          && !currentEffectiveActiveConversationId
          && !requestedEffectiveActiveConversationId
          && Boolean(storeState.conversationId || readLS(LS.conversation.activeId) !== "")
          && !((ev as unknown as { archive_navigation?: string }).archive_navigation === "home" && ev.active_conversation_id === null),
        );
        const effectiveActiveConversationId = currentEffectiveActiveConversationId
          ?? (storeState.activeConversationDraftId ? undefined : requestedEffectiveActiveConversationId);
        const nextConversationMetas = conversationMetas;
        const knownConversationIds = new Set(nextConversationMetas.map((conversation) => conversation.id));
        const removedConversationIds = storeState.conversations
          .map((conversation) => conversation.id)
          .filter((id) => !knownConversationIds.has(id));
        for (const removedId of removedConversationIds) {
          releaseConversationReadingState(removedId);
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
            pinnedConversationIds: ev.pinned_conversation_ids
              ?? (inventoryEpochChanged ? [] : state.pinnedConversationIds),
            pinnedConversationSortMode: ev.pinned_sort_mode
              ?? (inventoryEpochChanged ? "manual" : state.pinnedConversationSortMode),
            sidebarOrder: ev.sidebar_order && (inventoryEpochChanged || !state.sidebarOrder || ev.sidebar_order.revision >= state.sidebarOrder.revision)
              ? ev.sidebar_order : inventoryEpochChanged ? null : state.sidebarOrder,
            conversationMessages: Object.fromEntries(
              Object.entries(state.conversationMessages).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationHistoryPages: Object.fromEntries(
              Object.entries(state.conversationHistoryPages).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationStreaming: Object.fromEntries(
              Object.entries(state.conversationStreaming).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationAgentStates: Object.fromEntries(
              Object.entries(state.conversationAgentStates ?? {}).filter(([id]) => knownConversationIds.has(id)),
            ),
            conversationWorkbenchStates: state.conversationWorkbenchStates,
            conversationRecallTruncations: Object.fromEntries(
              Object.entries(state.conversationRecallTruncations ?? {}).filter(([id]) => knownConversationIds.has(id)),
            ),
            turnDiffs: Object.fromEntries(
              Object.entries(state.turnDiffs).filter(([id]) => knownConversationIds.has(id) || Boolean(state.sideChats[id])),
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
        // Each inventory mode belongs to its conversation, including hidden
        // composers. Otherwise a later background send would reuse their old
        // workbench selection after another window changed the durable mode.
        for (const meta of conversationMetas) {
          if (meta.agentMode !== undefined) useAppStore.getState().syncConversationAgentMode(meta.agentMode, meta.id, undefined, meta.revision);
        }
        // A create/switch request already owns navigation. An inventory reply
        // received during that request may refresh the sidebar, but must not
        // restore an older owner or send a competing fallback switch.
        if (storeState.activeConversationDraftId || storeState.pendingConversationSwitchId || storeState.pendingConversationCreateId) {
          if (ev.active_conversation) hydrateActiveConversation(ev.active_conversation, ev.active_conversation.id, undefined,
            { upsertMeta: false, activate: false, preserveStreamingAssistant: true });
          applyRuntimeSessionSnapshot(ev.session, requestedActiveConversationId ?? null, e);
          return true;
        }
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
        } else {
          clearActiveConversationView({ preserveMode: (ev as unknown as { archive_navigation?: string }).archive_navigation === "home" });
          if (activationNeedsBackendSync && fallbackActiveConversationId) {
            sendClientCommand({ type: "conversation.switch", conversation_id: fallbackActiveConversationId });
          }
        }
        applyRuntimeSessionSnapshot(ev.session, requestedActiveConversationId ?? null, e);
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
      const pendingNavigation = useAppStore.getState();
      const pendingConversationId = pendingNavigation.pendingConversationSwitchId ?? pendingNavigation.pendingConversationCreateId;
      const matchingDraftOwner = pendingNavigation.activeConversationDraftId
        ? (pendingNavigation.conversationDrafts[pendingNavigation.activeConversationDraftId]?.conversationId === switchedConversationId
          && pendingNavigation.pendingConversationSwitchId === switchedConversationId) || isRequestedForegroundNavigation(e)
        : true;
      const superseded = isSupersededCommandProjection(e);
      const activate = !superseded && matchingDraftOwner
        && (!pendingConversationId || switchedConversationId === pendingConversationId);
      const forceAuthoritative = Boolean(
        switchedConversationId
        && pendingAuthoritativeConversationResets.delete(switchedConversationId),
      );
      if (switchedConversationId && !superseded) {
        useAppStore.getState().setConversationHydration(
          switchedConversationId,
          ev.is_hydrating === true,
          Date.now(),
        );
      }
      if (!activate) {
        if (ev.conversation) hydrateActiveConversation(ev.conversation, switchedConversationId, undefined,
          { activate: false, preserveStreamingAssistant: true, forceAuthoritative });
      } else if (ev.conversation_id === null && ev.conversation === null) {
        clearActiveConversationView({ preserveMode: (ev as unknown as { archive_navigation?: string }).archive_navigation === "home" });
      } else if (ev.conversation?.archived || ev.conversation?.conversation_type === "side_chat") {
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
      applyRuntimeSessionSnapshot(ev.session, switchedConversationId ?? null, e);
      if (activate && !isReplayedEvent(e) && !ev.context_pending) {
        const active = useAppStore.getState();
        const owner = active.conversationId;
        const root = active.workingDirectory;
        const hasSkills = Array.isArray(ev.session?.capabilities?.skills);
        const ownsSwitch = (current: ReturnType<typeof useAppStore.getState>) =>
          current.conversationId === owner && !current.activeConversationDraftId
          && !current.pendingConversationSwitchId && workspaceRootsEqual(current.workingDirectory, root);
        const publish = () => {
          if (!ownsSwitch(useAppStore.getState())) return;
          sendClientCommand({ type: "commands.list" });
          if (owner) {
            if (ev.capabilities_deferred) sendClientCommand({ type: "runtime.capabilities.inspect",
              source: "workspace.activate.conversation.switch" }, { silent: true });
            if (!hasSkills) sendClientCommand({ type: "skills.list", conversation_id: owner,
              workspace_root: root }, { silent: true });
            sendClientCommand({ type: "mcp.list", conversation_id: owner,
              workspace_root: root || undefined }, { silent: true });
          }
        };
        if (owner) deferredSwitchCatalogs.get(owner)?.cancel();
        if (owner && ev.capabilities_deferred && selectPendingDraftUserMessage(active)) {
          // Keep background catalog work off the active stream. A request
          // for composer commands, mentions or attachments refreshes it now.
          const interact = (event: Event) => {
            if (!(event.target instanceof Element)) return;
            if (event.type === "pointerdown" && event.target.closest(".composer-attach-btn")) release();
            if (event instanceof KeyboardEvent && ["/", "@", "$"].includes(event.key)
              && event.target.closest(".composer-container")) release();
          };
          const cancel = () => {
            stop();
            document.removeEventListener("pointerdown", interact, true);
            document.removeEventListener("keydown", interact, true);
            deferredSwitchCatalogs.delete(owner);
          };
          const release = () => { cancel(); publish(); };
          const stop = useAppStore.subscribe(next => {
            if (!ownsSwitch(next) || !next.isConnected) { cancel(); return; }
            if (next.slashPanelOpen || next.mentionPanelOpen
              || (!selectPendingDraftUserMessage(next) && !next.isStreaming)) release();
          });
          document.addEventListener("pointerdown", interact, true);
          document.addEventListener("keydown", interact, true);
          deferredSwitchCatalogs.set(owner, { cancel });
        } else {
          window.setTimeout(publish, 0);
        }
      }
      return true;
    }
    default:
      return false;
  }
};
