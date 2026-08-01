import { useAppStore } from "../stores";
import type {
  ConversationListEvent,
  ConversationRecordPayload,
  ConversationSummaryPayload,
  ConversationSwitchedEvent,
  GoalInfo,
  GoalUpdatedEvent,
  LlmModelUpdatedEvent,
  UserMessageQueueUpdatedEvent,
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
  SubagentMessageState,
  SubagentState,
  TodoItem,
} from "../stores/types";
import { toConversationGoal } from "../stores/types";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { mergeCapabilities } from "../protocol/capabilities";
import { conversationResetPayload, LS, writeLS } from "../stores/shared-helpers";
import type { ConversationMeta } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { selectableModelsForProvider } from "../lib/provider-models";
import { normalizeContextLedger } from "./contextLedger";
import {
  isAgentProgressPhase,
} from "../protocol/streaming-types";

type ConversationSummary = ConversationSummaryPayload;
type ConversationPayload = ConversationRecordPayload;
const UI_AGENT_STATE_KEY = "ui_agent_state";

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
  title: maybeString(conversation.title) ?? "Untitled",
  updatedAt: maybeString(conversation.updated_at) ?? new Date().toISOString(),
  archived: conversation.archived,
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

const PLAN_STATUSES = new Set<PlanState["status"]>(["draft", "accepted", "executing", "completed", "cancelled"]);
const PLAN_STEP_STATUSES = new Set<PlanState["steps"][number]["status"]>(["pending", "running", "done", "skipped", "failed"]);
const TODO_STATUSES = new Set<TodoItem["status"]>(["pending", "in_progress", "completed", "blocked"]);
const SUBAGENT_STATUSES = new Set<SubagentState["status"]>(["pending", "running", "blocked", "done", "partial", "cancelled", "error"]);
const PROGRESS_STAGES = new Set<AgentProgressEntry["stage"]>(["status", "planning", "tool", "approval", "final"]);
const PROGRESS_STATUSES = new Set<AgentProgressEntry["status"]>(["running", "completed", "partial", "failed", "info"]);

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
    const existingIndex = state.conversations.findIndex((item) => item.id === meta.id);
    if (existingIndex >= 0) {
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
  const activeStreamIds = Array.isArray(session.active_stream_conversation_ids)
    ? session.active_stream_conversation_ids.map((id) => String(id || "").trim()).filter(Boolean)
    : [];
  if (activeStreamIds.length === 0) return;
  const activeStreamSet = new Set(activeStreamIds);
  const steerTargets = new Map<string, Set<string>>();
  for (const entry of session.pending_turn_inputs ?? []) {
    const conversationId = String(entry?.conversation_id || "").trim();
    const targetMessageId = String(entry?.target_message_id || "").trim();
    if (!conversationId || !targetMessageId) continue;
    const targets = steerTargets.get(conversationId) ?? new Set<string>();
    targets.add(targetMessageId);
    steerTargets.set(conversationId, targets);
  }

  const markStreamingAssistant = (messages: ChatMessage[], conversationId: string): ChatMessage[] => {
    const targets = steerTargets.get(conversationId);
    let fallbackIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === "assistant") {
        fallbackIndex = index;
        break;
      }
    }
    return messages.map((message, index) => (
      message.role === "assistant"
      && ((targets?.has(message.id) ?? false) || (!targets?.size && index === fallbackIndex))
        ? { ...message, isStreaming: true }
        : message
    ));
  };

  useAppStore.setState((state) => {
    const conversationMessages = { ...state.conversationMessages };
    const conversationStreaming = { ...state.conversationStreaming };
    const sideChats = { ...state.sideChats };
    let activeMessages = state.messages;
    for (const conversationId of activeStreamIds) {
      conversationStreaming[conversationId] = true;
      if (sideChats[conversationId]) {
        sideChats[conversationId] = {
          ...sideChats[conversationId],
          isStreaming: true,
          messages: markStreamingAssistant(sideChats[conversationId].messages, conversationId),
        };
        continue;
      }
      const source = conversationMessages[conversationId]
        ?? (state.conversationId === conversationId ? state.messages : []);
      const messages = markStreamingAssistant(source, conversationId);
      conversationMessages[conversationId] = messages;
      if (state.conversationId === conversationId) activeMessages = messages;
    }
    return {
      conversationMessages,
      conversationStreaming,
      sideChats,
      messages: activeMessages,
      ...(state.conversationId && activeStreamSet.has(state.conversationId) ? { isStreaming: true } : {}),
    };
  });
};

const hydrateActiveConversation = (
  conversation: ConversationPayload | null | undefined,
  activeConversationId?: string,
  fallbackMessages?: BackendTranscriptMessage[],
  options: { upsertMeta?: boolean; preserveStreamingAssistant?: boolean } = {},
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
      const messages = options.preserveStreamingAssistant
        ? mergeHydratedWithStreamingAssistants(conversationId, hydrated)
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

const mergeHydratedWithStreamingAssistants = (
  conversationId: string,
  hydratedMessages: ChatMessage[],
): ChatMessage[] => {
  const state = useAppStore.getState();
  const cachedMessages = state.conversationMessages[conversationId]
    ?? (state.conversationId === conversationId ? state.messages : []);

  // A delayed or stale conversation.switched snapshot must not clobber a
  // fresher cached answer. Preserve cached assistant messages the snapshot is
  // missing when they are in-flight OR newer than the snapshot's latest
  // completed message (i.e. the cache is ahead of the snapshot).
  const completedMs = (message: ChatMessage): number =>
    Number((message as { completedAt?: number }).completedAt ?? 0);
  const hydratedLatest = hydratedMessages.reduce(
    (max, message) => Math.max(max, completedMs(message)),
    0,
  );
  const hydratedIds = new Set(hydratedMessages.map((message) => message.id));

  const preserved = cachedMessages.filter((message) => {
    if (message.role !== "assistant") return false;
    if (hydratedIds.has(message.id)) return false;
    if (Boolean(message.isStreaming)) return true; // in-flight: always preserve
    return completedMs(message) > hydratedLatest; // cache is ahead of snapshot
  });

  if (!preserved.length) return hydratedMessages;
  return [...hydratedMessages, ...preserved];
};

const activeConversationWorkspace = (): string => {
  const state = useAppStore.getState();
  const active = state.conversations.find((conversation) => conversation.id === state.conversationId);
  return active?.worktreePath || active?.workspaceRoot || "";
};

const clearActiveConversationView = () => {
  writeLS(LS.conversation.activeId, "");
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
        clearStreamingState(buffers, { conversationId: activeConversationId || s.conversationId });
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
      if (ev.type === "session.restored" && ev.missed_events) {
        pushToast("Connection was lost during an active run; some events may be missing.", "warning", 8000);
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
        const conversationMetas = ev.conversations.map(toConversationMeta);
        const requestedActiveConversationId = maybeString(ev.active_conversation_id);
        const storeState = useAppStore.getState();
        const requestedEffectiveActiveConversationId = visibleActiveConversationId(requestedActiveConversationId, conversationMetas);
        const pendingLocalActive = storeState.conversationId
          ? storeState.conversations.find((conversation) =>
              conversation.id === storeState.conversationId &&
              conversation.title === "New chat" &&
              !conversationMetas.some((item) => item.id === conversation.id)
            )
          : undefined;
        const effectiveActiveConversationId = pendingLocalActive
          ? undefined
          : (requestedEffectiveActiveConversationId ?? fallbackVisibleConversationId(storeState.conversationId, conversationMetas));
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
          conversationRecallTruncations: Object.fromEntries(
            Object.entries(state.conversationRecallTruncations ?? {}).filter(([id]) => knownConversationIds.has(id)),
          ),
        }));
        const eventActiveConversation = ev.active_conversation ?? null;
        const activeConversation = eventActiveConversation
          && eventActiveConversation.id === effectiveActiveConversationId
          && !eventActiveConversation.archived
            ? eventActiveConversation
            : null;
        if (effectiveActiveConversationId) {
          const alreadyActive = storeState.conversationId === effectiveActiveConversationId;
          if (alreadyActive) {
            const activeMeta = conversationMetas.find((conversation) => conversation.id === effectiveActiveConversationId);
            if (activeMeta?.goal !== undefined) {
              useAppStore.getState().setActiveGoal(activeMeta.goal ?? null, effectiveActiveConversationId);
            }
            if (activeConversation) {
              hydrateConversationAgentState(effectiveActiveConversationId, activeConversation);
            }
          } else {
            hydrateActiveConversation(activeConversation, effectiveActiveConversationId, undefined, { upsertMeta: false });
          }
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
        const activeStreamIds = Array.isArray(ev.session?.active_stream_conversation_ids)
          ? ev.session.active_stream_conversation_ids
          : [];
        const preserveStreamingAssistant = activeStreamIds.includes(nextConversationId)
          || (activeStreamIds.length === 0 && Boolean(ev.session?.active_task_id));
        hydrateActiveConversation(ev.conversation, nextConversationId, undefined, { preserveStreamingAssistant });
      }
      applyRuntimeSessionSnapshot(ev.session);
      return true;
    }
    default:
      return false;
  }
};
