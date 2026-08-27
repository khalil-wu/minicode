import { useAppStore } from "../stores";
import type {
  AgentProgressEvent,
  AgentRunCompletedEvent,
  AgentRunStartedEvent,
  ContextForkedEvent,
  ContextLedgerEvent,
  ContextSideQueryResultEvent,
  ParentNotificationsEvent,
  TurnPlanUpdatedEvent,
  RateLimitEvent,
  RuntimeSpanEvent,
  ServerEvent,
  SessionStateEvent,
  StreamEventEvent,
  SubagentEventEvent,
  SubagentMailboxEvent,
  SubagentPlanApprovalRequestedEvent,
  SubagentProgressEvent,
  SubagentStartEvent,
  TaskUpdateEvent,
} from "../protocol/events";
import type { McpServerStatus, SubagentMessageState, SubagentState, TodoItem } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { withDerivedCapabilitySummary } from "../protocol/capabilities";
import { pushToast } from "../overlays/ToastContainer";
import { addInspectorPayload } from "./inspectorEntries";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { normalizeContextLedger } from "./contextLedger";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { LS, writeLS } from "../stores/shared-helpers";

const userVisibleSubagentProgress = (value?: string, explicitVisible?: boolean): string => {
  const text = String(value ?? "").trim();
  if (explicitVisible === false) return "";
  return text;
};

const appendSubagentActivity = (existing: SubagentState | undefined, activity?: string): string[] => {
  const clean = userVisibleSubagentProgress(activity);
  const current = existing?.activityLog ?? [];
  if (!clean || current.at(-1) === clean) return current;
  return [...current, clean].slice(-12);
};

const swarmMessageState = (value: unknown): SubagentMessageState | null => {
  const message = maybeObject(value);
  if (!message) return null;
  const messageId = maybeString(message.message_id);
  const content = maybeString(message.content);
  if (!messageId || content == null) return null;
  return {
    messageId,
    senderId: maybeString(message.sender_id) ?? "agent",
    recipientId: maybeString(message.recipient_id) ?? "",
    content,
    createdAt: maybeNumber(message.created_at) ?? Date.now(),
    seq: maybeNumber(message.seq),
    senderMailboxEpoch: maybeNumber(message.sender_mailbox_epoch),
    recipientMailboxEpoch: maybeNumber(message.recipient_mailbox_epoch),
    deliveryStatus: "sent",
  };
};

const mergeSubagentMessages = (
  current: SubagentMessageState[] | undefined,
  incoming: Array<SubagentMessageState | null>,
): SubagentMessageState[] => {
  const merged = [...(current ?? [])];
  for (const message of incoming) {
    if (!message) continue;
    const index = merged.findIndex((item) => item.messageId === message.messageId);
    if (index >= 0) merged[index] = { ...merged[index], ...message };
    else merged.push(message);
  }
  return merged
    .sort((a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER) || a.createdAt - b.createdAt)
    .slice(-100);
};

const snapshotMessages = (event: unknown): SubagentMessageState[] => {
  const snapshot = maybeObject((event as { snapshot?: unknown }).snapshot);
  const raw = snapshot?.messages;
  return Array.isArray(raw) ? raw.map(swarmMessageState).filter((item): item is SubagentMessageState => Boolean(item)) : [];
};

const explicitSubagentActivity = (
  currentActivity?: string,
  detail?: string,
  activitySummary?: string,
  explicitVisible?: boolean,
): string => {
  return userVisibleSubagentProgress(activitySummary, explicitVisible)
    || userVisibleSubagentProgress(currentActivity, explicitVisible)
    || userVisibleSubagentProgress(detail, explicitVisible);
};

const transcriptSnapshotPatch = (
  event: unknown,
  existing?: SubagentState,
): Pick<SubagentState, "transcriptMessages" | "transcriptSeq"> | Record<string, never> => {
  const snapshot = maybeObject((event as { transcript_snapshot?: unknown }).transcript_snapshot);
  const seq = maybeNumber(snapshot?.seq);
  const rawMessages = snapshot?.messages;
  if (seq == null || !Array.isArray(rawMessages) || seq <= (existing?.transcriptSeq ?? -1)) return {};
  return {
    transcriptSeq: seq,
    transcriptMessages: hydrateMessages(rawMessages as BackendTranscriptMessage[]),
  };
};

const subagentProgressSummary = (ev: {
  detail?: string;
  tool_name?: string;
  iteration?: number;
  max_iterations?: number;
  activity_summary?: string;
  user_visible?: boolean;
}): string => {
  const detail = userVisibleSubagentProgress(ev.activity_summary, ev.user_visible)
    || userVisibleSubagentProgress(ev.detail, ev.user_visible);
  if (detail) return detail;
  return "正在执行子任务";
};

const subagentResultPayload = (event: unknown): Record<string, unknown> | null => {
  const source = event as {
    result?: unknown;
    record?: unknown;
    snapshot?: unknown;
  };
  const direct = source.result;
  if (direct && typeof direct === "object") return direct as Record<string, unknown>;
  const snapshot = source.snapshot;
  if (snapshot && typeof snapshot === "object") {
    const result = (snapshot as { result?: unknown }).result;
    if (result && typeof result === "object") return result as Record<string, unknown>;
  }
  // Durable status snapshots and older live events may carry only the
  // completed runtime record. Treat its result summary as a successful result
  // payload instead of manufacturing a generic failure row when the live
  // result field was lost during reconnect.
  const record = source.record
    ?? (snapshot && typeof snapshot === "object"
      ? (snapshot as { record?: unknown }).record
      : undefined);
  if (!record || typeof record !== "object") return null;
  const recordObject = record as Record<string, unknown>;
  const content = recordObject.content
    ?? recordObject.result_content
    ?? recordObject.result_summary;
  const error = recordObject.error ?? recordObject.result_error;
  if (content == null && error == null) return null;
  return {
    content,
    error,
    status: recordObject.status,
    duration_ms: recordObject.duration_ms,
    tool_call_count: recordObject.tool_call_count ?? recordObject.tool_count,
  };
};

const maybeString = (value: unknown): string | undefined => {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed || undefined;
};

const maybeNumber = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const maybeBoolean = (value: unknown): boolean | undefined =>
  typeof value === "boolean" ? value : undefined;

const maybeStringList = (value: unknown): string[] | undefined => {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? [trimmed] : undefined;
  }
  if (!Array.isArray(value)) return undefined;
  const result = value
    .map((item) => String(item ?? "").trim())
    .filter(Boolean);
  return result.length ? Array.from(new Set(result)) : undefined;
};

const maybeObject = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" ? value as Record<string, unknown> : null;

const subagentStatusFromTask = (status: string): SubagentState["status"] => {
  if (status === "completed" || status === "done") return "done";
  if (status === "cancelled") return "cancelled";
  if (status === "partial") return "partial";
  if (status === "failed" || status === "error") return "error";
  if (status === "blocked") return "blocked";
  if (status === "pending") return "pending";
  return "running";
};

const isTerminalSubagentStatus = (status: SubagentState["status"]): boolean =>
  status === "done" || status === "partial" || status === "cancelled" || status === "error";

const subagentIncarnation = (
  payload: Record<string, unknown>,
  record?: Record<string, unknown> | null,
): { agentPath?: string; mailboxEpoch?: number } => ({
  agentPath: maybeString(payload.agent_path) ?? maybeString(record?.agent_path),
  mailboxEpoch: maybeNumber(payload.mailbox_epoch) ?? maybeNumber(record?.mailbox_epoch),
});

const isStaleSubagentIncarnation = (
  existing: SubagentState | undefined,
  payload: Record<string, unknown>,
  record?: Record<string, unknown> | null,
): boolean => {
  if (!existing) return false;
  const incoming = subagentIncarnation(payload, record);
  const currentHasFence = Boolean(existing.agentPath) && typeof existing.mailboxEpoch === "number";
  const incomingHasFence = Boolean(incoming.agentPath) && typeof incoming.mailboxEpoch === "number";
  if (!currentHasFence) return false;
  if (!incomingHasFence) return true;
  if (incoming.mailboxEpoch! < existing.mailboxEpoch!) return true;
  return incoming.mailboxEpoch === existing.mailboxEpoch
    && incoming.agentPath !== existing.agentPath;
};

const subagentMetadataPatch = (
  payload: Record<string, unknown>,
  record?: Record<string, unknown> | null,
): Partial<SubagentState> => {
  const value = (key: string, altKey?: string): unknown => payload[key] ?? (altKey ? payload[altKey] : undefined) ?? record?.[key] ?? (altKey ? record?.[altKey] : undefined);
  const now = Date.now();
  const patch: Partial<SubagentState> = { lastEventAt: now, lastProgressAt: now };
  const taskId = maybeString(value("task_id"));
  const objective = maybeString(value("objective")) ?? maybeString(value("prompt_summary"));
  const currentActivity = userVisibleSubagentProgress(maybeString(value("current_activity"))) || undefined;
  const waitingOn = userVisibleSubagentProgress(maybeString(value("waiting_on", "waitingOn"))) || undefined;
  const lastProgressAt = maybeNumber(value("last_progress_at", "lastProgressAt"));
  const order = maybeNumber(value("order"));
  const dependsOn = maybeStringList(value("depends_on"));
  const blockedBy = maybeStringList(value("blocked_by"));
  const writeScope = maybeStringList(value("write_scope"));
  const background = maybeBoolean(value("background"));
  const readOnly = maybeBoolean(value("read_only"));
  const { agentPath, mailboxEpoch } = subagentIncarnation(payload, record);
  if (agentPath) patch.agentPath = agentPath;
  if (typeof mailboxEpoch === "number") patch.mailboxEpoch = mailboxEpoch;
  if (taskId) patch.taskId = taskId;
  if (objective) patch.objective = objective;
  if (currentActivity) patch.currentActivity = currentActivity;
  if (waitingOn) patch.waitingOn = waitingOn;
  if (typeof lastProgressAt === "number") patch.lastProgressAt = lastProgressAt;
  if (typeof order === "number") patch.order = order;
  if (dependsOn) patch.dependsOn = dependsOn;
  if (blockedBy) patch.blockedBy = blockedBy;
  if (writeScope) patch.writeScope = writeScope;
  if (typeof background === "boolean") patch.background = background;
  if (typeof readOnly === "boolean") patch.readOnly = readOnly;
  return patch;
};

const visibleSubagentsForConversation = (conversationId?: string): SubagentState[] => {
  const currentState = useAppStore.getState();
  if (!conversationId?.trim()) return [];
  return conversationId !== currentState.conversationId
    ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
    : currentState.subagents;
};

const isActiveConversationEvent = (conversationId?: string): boolean => {
  if (!conversationId?.trim()) return false;
  return useAppStore.getState().conversationId === conversationId.trim();
};

const isReplayedRuntimeEvent = (event: ServerEvent): boolean =>
  (event as ServerEvent & { __replayed?: boolean }).__replayed === true;

const eventTimestampMs = (event: ServerEvent): number => {
  const parsed = Date.parse(String(event.timestamp || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const rateLimitMessage = (event: RateLimitEvent): string => {
  const reason = event.error_type === "quota_exceeded"
    ? "模型额度已用尽"
    : event.error_type === "concurrency_limit"
      ? "模型并发额度已满"
      : event.error_type === "busy"
        ? "模型服务暂时繁忙"
        : "模型请求受到速率限制";
  const details: string[] = [];
  const provider = String(event.provider || "").trim();
  if (provider) details.push(`提供商：${provider}`);
  if (typeof event.retry_after_seconds === "number" && event.retry_after_seconds > 0) {
    details.push(`${Math.ceil(event.retry_after_seconds)} 秒后重试`);
  } else if (typeof event.retry_at === "number" && event.retry_at > Date.now()) {
    details.push(`${Math.max(1, Math.ceil((event.retry_at - Date.now()) / 1000))} 秒后重试`);
  }
  if (event.recoverable === false) details.push("无法自动恢复");
  const message = String(event.message || "").trim();
  if (message && message !== reason) details.push(message);
  return [reason, ...details].join(" · ");
};

const TURN_SCOPED_RUNTIME_EVENTS = new Set<string>([
  "agent.progress",
  "runtime.span",
  "agent.run.started",
  "agent.run.completed",
  "turn.plan.updated",
  "task.update",
  "subagent.start",
  "subagent.event",
  "subagent.mailbox",
  "subagent.progress",
  "subagent.done",
]);

const LATE_TURN_TERMINAL_EVENTS = new Set<string>([
  "agent.run.completed",
  "task.update",
]);

const runtimeSpanBasePhase = (ev: RuntimeSpanEvent): string => {
  const explicit = String(ev.phase || "").trim().toLowerCase();
  if (explicit) return explicit;
  return String(ev.event || "").split(".")[0]?.trim().toLowerCase() || "";
};

const inspectorTargetForRuntimeSpan = (ev: RuntimeSpanEvent): { kind: "message" | "tool_call" | "provider" | "subagent" | "cache"; id: string } => {
  const toolCallId = String(ev.tool_call_id || "").trim();
  if (toolCallId) return { kind: "tool_call", id: toolCallId };
  const phase = runtimeSpanBasePhase(ev);
  if (phase === "provider" || String(ev.event || "").toLowerCase().startsWith("provider.")) {
    return { kind: "provider", id: ev.span_id };
  }
  if (phase === "subagent") {
    return { kind: "subagent", id: String(ev.agent_id || ev.span_id).trim() || ev.span_id };
  }
  if (phase === "cache") {
    return { kind: "cache", id: ev.span_id };
  }
  return { kind: "message", id: ev.span_id };
};

const eventMessageId = (event: unknown): string | undefined => {
  const value = (event as { message_id?: unknown; messageId?: unknown }).message_id
    ?? (event as { message_id?: unknown; messageId?: unknown }).messageId;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const hasStreamingAssistantForMessage = (conversationId: string | undefined, messageId: string): boolean => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return false;
  const messages = targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  return messages.some((message) =>
    message.id === messageId &&
    message.role === "assistant" &&
    Boolean(message.isStreaming || message.isThinkingStreaming),
  );
};

const canApplyLateTerminalEvent = (
  conversationId: string | undefined,
  messageId: string,
  eventType: string,
): boolean => {
  if (!LATE_TURN_TERMINAL_EVENTS.has(eventType)) return false;
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return false;
  const messages = targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  const matchingAssistant = messages.find((message) => message.role === "assistant" && message.id === messageId);
  if (!matchingAssistant?.terminalStatus) return false;
  return !messages.some((message) =>
    message.role === "assistant"
    && message.id !== messageId
    && Boolean(message.isStreaming || message.isThinkingStreaming),
  );
};

const todoPatchFromEvent = (
  ev: TaskUpdateEvent & { todo_id?: string; status?: string; content?: string; activeForm?: string },
  activeFormFallback: string,
) => ({
  status: ev.status as "pending" | "in_progress" | "completed" | "blocked",
  content: ev.content ?? "",
  activeForm: ev.activeForm ?? activeFormFallback,
});

const todosFromSnapshot = (
  ev: TaskUpdateEvent & { todos?: unknown },
) => {
  if (!Array.isArray(ev.todos)) return null;
  const todos = ev.todos
    .filter((todo): todo is {
      todo_id?: string;
      id?: string;
      status?: string;
      content?: string;
      activeForm?: string;
    } => Boolean(todo && typeof todo === "object"))
    .map((todo) => ({
      id: String(todo.id ?? todo.todo_id ?? "").trim(),
      status: todo.status as "pending" | "in_progress" | "completed" | "blocked",
      content: String(todo.content ?? ""),
      activeForm: String(todo.activeForm ?? todo.content ?? ""),
    }))
    .filter((todo) =>
      Boolean(todo.id) &&
      ["pending", "in_progress", "completed", "blocked"].includes(todo.status),
    );
  return dedupeSnapshotTodos(todos);
};

type SnapshotTodo = {
  id: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
  content: string;
  activeForm: string;
};

const SNAPSHOT_STATUS_PRIORITY: Record<SnapshotTodo["status"], number> = {
  blocked: 4,
  in_progress: 3,
  pending: 2,
  completed: 1,
};

const dedupeSnapshotTodos = (todos: SnapshotTodo[]): SnapshotTodo[] => {
  const result: SnapshotTodo[] = [];
  const seen = new Map<string, number>();
  for (const todo of todos) {
    const key = normalizeSnapshotTodoText(todo.content || todo.activeForm || todo.id);
    const existingIndex = seen.get(key);
    if (existingIndex == null) {
      seen.set(key, result.length);
      result.push(todo);
      continue;
    }
    result[existingIndex] = preferSnapshotTodo(result[existingIndex], todo);
  }
  return result;
};

const preferSnapshotTodo = (current: SnapshotTodo, next: SnapshotTodo): SnapshotTodo => {
  if (SNAPSHOT_STATUS_PRIORITY[next.status] > SNAPSHOT_STATUS_PRIORITY[current.status]) {
    return {
      ...next,
      activeForm: next.activeForm || current.activeForm,
    };
  }
  if (
    SNAPSHOT_STATUS_PRIORITY[next.status] === SNAPSHOT_STATUS_PRIORITY[current.status] &&
    next.activeForm.length > current.activeForm.length
  ) {
    return {
      ...current,
      activeForm: next.activeForm,
    };
  }
  return current;
};

const normalizeSnapshotTodoText = (value: string): string =>
  value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[`*_~"'“”‘’]/g, "")
    .replace(/[()[\]{}<>:;,.!?/\\|，。！？、；：（）《》【】]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();

const TODO_PROGRESS_RANK: Record<TodoItem["status"], number> = {
  pending: 0,
  in_progress: 1,
  completed: 2,
  blocked: 3,
};

const todoProgressCursor = (todos: TodoItem[]): number => {
  if (todos.length === 0) return 0;
  const blockedIndex = todos.findIndex((todo) => todo.status === "blocked");
  if (blockedIndex >= 0) return blockedIndex + 1;
  const runningIndex = todos.findIndex((todo) => todo.status === "in_progress");
  if (runningIndex >= 0) return runningIndex + 1;
  const pendingIndex = todos.findIndex((todo) => todo.status === "pending");
  if (pendingIndex >= 0) return pendingIndex + 1;
  return todos.length + 1;
};

const todoProgressScore = (todos: TodoItem[]): number => {
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const blocked = todos.some((todo) => todo.status === "blocked") ? 1 : 0;
  return completed * 100 + todoProgressCursor(todos) + blocked * 1000;
};

const mergeRegressiveTodoSnapshot = (incoming: TodoItem[], current: TodoItem[]): TodoItem[] => {
  if (incoming.length === 0 || current.length === 0) return incoming;
  if (incoming.length !== current.length) return incoming;
  if (todoProgressScore(incoming) >= todoProgressScore(current)) return incoming;

  let inProgressKept = false;
  return incoming.map((todo, index) => {
    const previous = current[index];
    if (!previous) return todo;
    const previousRank = TODO_PROGRESS_RANK[previous.status] ?? 0;
    const incomingRank = TODO_PROGRESS_RANK[todo.status] ?? 0;
    const status = previousRank > incomingRank ? previous.status : todo.status;
    const activeForm = status === todo.status
      ? todo.activeForm
      : previous.activeForm || todo.activeForm;
    if (status !== "in_progress") {
      return { ...todo, status, activeForm };
    }
    if (inProgressKept) {
      return { ...todo, status: "pending", activeForm: todo.activeForm };
    }
    inProgressKept = true;
    return { ...todo, status, activeForm };
  });
};

export const handleRuntimeEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const eventOwner = (e as unknown as { conversation_id?: unknown }).conversation_id;
  conversationId = typeof eventOwner === "string" && eventOwner.trim()
    ? eventOwner.trim()
    : conversationId?.trim() || undefined;
  const s = useAppStore.getState();
  const messageId = eventMessageId(e);
  const globalSessionSnapshot = e.type === "task.update"
    && Boolean((e as unknown as { session?: unknown }).session);
  if (TURN_SCOPED_RUNTIME_EVENTS.has(e.type) && !conversationId && !globalSessionSnapshot) {
    addInspectorPayload("message", `unowned:${e.type}:${messageId || "event"}`, {
      event: e.type,
      unowned: true,
      payload: e,
    });
    return true;
  }
  if (
    messageId
    && TURN_SCOPED_RUNTIME_EVENTS.has(e.type)
    && !hasStreamingAssistantForMessage(conversationId, messageId)
    && !canApplyLateTerminalEvent(conversationId, messageId, e.type)
  ) {
    // `done` is the turn's delivery fence, so these rows cannot be rendered.
    // They still describe work that happened — `runtime.span` and
    // `agent.progress` arriving late can carry status:"failed" — and dropping
    // them with a bare `return true` left no record anywhere that the turn had
    // a failure the transcript never showed.
    addInspectorPayload("message", `late:${conversationId || "session"}:${messageId}:${e.type}`, {
      event: e.type,
      dropped: true,
      reason: "turn_already_terminal",
      detail: "事件在本轮 done 之后到达，已越过投递栅栏",
      conversation_id: conversationId,
      message_id: messageId,
      status: (e as unknown as { status?: unknown }).status,
      payload: e,
    });
    return true;
  }
  switch (e.type) {
    case "agent.progress": {
      const ev = e as AgentProgressEvent;
      const message = String(ev.message ?? "").trim();
      // Main chat is owned by typed message/tool/approval events. Keep legacy
      // progress available to the activity/inspector surfaces without creating
      // a second transcript item for the same lifecycle.
      if (ev.tool_call_id) {
        addInspectorPayload("tool_call", ev.tool_call_id, {
          event: "agent.progress",
          conversation_id: conversationId,
          message_id: messageId,
          id: ev.id,
          stage: ev.stage,
          phase: ev.phase,
          status: ev.status,
          message,
          label: ev.label,
          summary: ev.summary,
          detail: ev.detail,
          tool_name: ev.tool_name,
          group_id: ev.group_id,
          step_id: ev.step_id,
          iteration_id: ev.iteration_id,
          count: ev.count,
          ephemeral: ev.ephemeral,
          replayed: isReplayedRuntimeEvent(e),
        });
        return true;
      }
      if (message) {
        const progress = {
          id: String(ev.id || `${ev.stage || "status"}:${message}`),
          stage: ev.stage || "status",
          phase: ev.phase,
          status: ev.status || "info",
          message,
          label: ev.label,
          summary: ev.summary,
          visibility: ev.visibility,
          detail: ev.detail,
          toolCallId: ev.tool_call_id,
          toolName: ev.tool_name,
          groupId: ev.group_id,
          stepId: ev.step_id,
          count: ev.count,
          ephemeral: ev.ephemeral,
        };
        if (ev.stage === "image_generation") {
          s.upsertMessageProgress(progress, conversationId, messageId);
          addInspectorPayload("provider", progress.id, {
            event: "agent.progress",
            conversation_id: conversationId,
            message_id: messageId,
            ...progress,
            replayed: isReplayedRuntimeEvent(e),
          });
        } else {
          s.appendAgentProgress(progress, conversationId);
        }
      }
      return true;
    }
    case "runtime.span": {
      const ev = e as RuntimeSpanEvent;
      const spanId = String(ev.span_id || "").trim();
      if (!spanId) return true;
      const target = inspectorTargetForRuntimeSpan(ev);
      addInspectorPayload(target.kind, target.id, {
        ...ev,
        span_event: ev.event,
        event: "runtime.span",
      });
      return true;
    }
    case "agent.run.started":
    case "agent.run.completed": {
      const ev = e as AgentRunStartedEvent | AgentRunCompletedEvent;
      const runId = String(ev.run_id || "").trim();
      if (!runId) return true;
      const runMessageId = String((ev as unknown as { message_id?: string }).message_id || "").trim();
      s.bindStreamingTurn(ev.conversation_id || conversationId, runMessageId || undefined, runId);
      addInspectorPayload("message", runMessageId || runId, {
        ...ev,
        event: ev.type,
      });
      if (ev.type === "agent.run.completed") {
        // The durable runtime completion is authoritative even when the
        // transport-level `done` envelope was lost during disconnect/replay.
        // Only the active assistant is eligible; a late completion for an
        // already sealed turn remains inspector metadata and cannot close a
        // newer turn.
        const owner = String(ev.conversation_id || conversationId || "").trim();
        const targetMessageId = runMessageId || undefined;
        if (owner && targetMessageId && hasStreamingAssistantForMessage(owner, targetMessageId)) {
          const rawStatus = String(ev.status || "completed").trim().toLowerCase();
          const terminalStatus = rawStatus === "partial"
            ? "partial" as const
            : rawStatus === "failed"
              ? "failed" as const
              : rawStatus === "cancelled" || rawStatus === "interrupted"
                ? "interrupted" as const
                : "completed" as const;
          const reason = String(
            (ev as unknown as { terminal_reason?: unknown }).terminal_reason
              || ev.summary
              || ev.error
              || "",
          ).trim();
          s.finishStreaming(
            owner,
            undefined,
            terminalStatus,
            targetMessageId,
            terminalStatus === "failed" ? (ev.error || ev.summary || reason) : undefined,
            false,
            undefined,
            reason,
          );
          s.finishAgentProgress(
            owner,
            terminalStatus === "failed" || terminalStatus === "interrupted"
              ? "failed"
              : terminalStatus === "partial" ? "partial" : "completed",
          );
        }
      }
      return true;
    }
    case "context_usage": {
      if (!isActiveConversationEvent(conversationId)) return true;
      const ev = e as unknown as {
        used?: number;
        limit?: number;
        ledger?: unknown;
      };
      if (ev.used != null && ev.limit != null) {
        const currentUsage = useAppStore.getState().contextUsage;
        s.setContextUsage({
          used: ev.used,
          limit: ev.limit,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
          ledger: normalizeContextLedger(ev.ledger) ?? currentUsage?.ledger,
        });
      }
      return true;
    }
    case "context_compacted": {
      const compacted = e as unknown as {
        summary?: string;
        before_tokens?: number;
        after_tokens?: number;
        retained_categories?: string[];
        ledger?: unknown;
      };
      const summary = compacted.summary ?? "上下文已压缩。";
      const ledger = normalizeContextLedger(compacted.ledger);
      const beforeTokens = maybeNumber(compacted.before_tokens);
      const afterTokens = maybeNumber(compacted.after_tokens);
      const savedTokens = beforeTokens != null && afterTokens != null
        ? Math.max(0, beforeTokens - afterTokens)
        : undefined;
      addInspectorPayload("budget", `compaction:${conversationId || "unowned"}`, {
        event: "context_compacted",
        conversation_id: conversationId,
        summary,
        before_tokens: beforeTokens,
        after_tokens: afterTokens,
        saved_tokens: savedTokens,
        retained_categories: compacted.retained_categories,
        ledger,
        replayed: isReplayedRuntimeEvent(e),
      });
      if (!isActiveConversationEvent(conversationId)) return true;
      const currentUsage = useAppStore.getState().contextUsage;
      s.setContextUsage({
        used: afterTokens ?? currentUsage?.used ?? 0,
        limit: currentUsage?.limit ?? 0,
        compactedAt: eventTimestampMs(e),
        compactSummary: summary,
        ledger: ledger ?? currentUsage?.ledger,
      });
      if (!isReplayedRuntimeEvent(e)) {
        sendClientCommand({ type: "session.usage.inspect" });
      }
      const tokenSummary = beforeTokens != null && afterTokens != null
        ? `，从 ${beforeTokens.toLocaleString()} 降至 ${afterTokens.toLocaleString()} tokens${savedTokens ? `，节省 ${savedTokens.toLocaleString()}` : ""}`
        : "";
      s.upsertSystemMessage(
        "system-compact-status",
        `上下文已压缩${tokenSummary}，摘要已保存到会话记忆中。`,
        { conversationId, replacePrefix: "正在压缩上下文" },
      );
      return true;
    }
    case "context_ledger": {
      if (!isActiveConversationEvent(conversationId)) return true;
      const ev = e as ContextLedgerEvent;
      const ledger = normalizeContextLedger(ev);
      if (ledger) {
        const currentUsage = useAppStore.getState().contextUsage;
        s.setContextUsage({
          used: currentUsage?.used ?? ledger.actual_tokens,
          limit: currentUsage?.limit ?? 0,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
          ledger,
        });
        addInspectorPayload("budget", `ledger:${conversationId || "active"}`, {
          event: "context_ledger",
          ledger,
        });
      }
      return true;
    }
    case "context_forked": {
      if (!conversationId) return true;
      const ev = e as ContextForkedEvent;
      const branchId = String(ev.branch_conversation_id || "").trim();
      const destination = ev.branch_created
        ? ev.branch_activated
          ? "已创建并切换到上下文分支"
          : "已创建上下文分支"
        : "已创建临时上下文分叉";
      const identity = branchId || ev.fork_id;
      s.upsertSystemMessage(
        `context-forked:${ev.fork_id}`,
        `${destination}（${identity}），从第 ${ev.message_index + 1} 条可见消息分叉，保留 ${ev.history_length} 条模型历史，估算 ${ev.estimated_tokens.toLocaleString()} tokens。`,
        { conversationId },
      );
      return true;
    }
    case "context_side_query_result": {
      if (!conversationId) return true;
      const ev = e as ContextSideQueryResultEvent;
      const result = ev.result.trim() || "未返回文本结果。";
      const focus = ev.focus.trim();
      const eventIdentity = String(e.event_id || (Number.isSafeInteger(e.seq) ? `seq-${e.seq}` : `${ev.query}:${ev.focus}`));
      s.upsertSystemMessage(
        `context-side-query:${eventIdentity}`,
        [
          focus ? `上下文旁路查询（聚焦：${focus}）` : "上下文旁路查询",
          `问题：${ev.query}`,
          `结果：${result}`,
        ].join("\n\n"),
        { conversationId },
      );
      return true;
    }
    case "turn.plan.updated": {
      const ev = e as TurnPlanUpdatedEvent;
      const owner = String(ev.conversation_id || conversationId || "").trim();
      const threadId = String(ev.thread_id || "").trim();
      const turnId = String(ev.turn_id || "").trim();
      if (!owner || threadId !== owner || !turnId || !Array.isArray(ev.plan)) return true;
      const currentState = useAppStore.getState();
      const messages = owner === currentState.conversationId
        ? currentState.messages
        : currentState.conversationMessages[owner] ?? [];
      const ownerMessage = messages.find((message) =>
        message.role === "assistant" && message.turnId === turnId,
      );
      if (!ownerMessage) return true;
      if (ev.message_id && ownerMessage.id !== ev.message_id) return true;
      const validStatus = new Set(["pending", "in_progress", "completed"]);
      s.setPlan({
        threadId,
        turnId,
        explanation: typeof ev.explanation === "string" ? ev.explanation : undefined,
        plan: ev.plan.map((step) => ({
          step: String(step.step ?? ""),
          status: (step.status && validStatus.has(step.status) ? step.status : "pending") as
            "pending" | "in_progress" | "completed",
        })),
      }, owner);
      return true;
    }
    case "task.update": {
      const ev = e as TaskUpdateEvent;

      if ("session" in ev && ev.session) {
        s.setRuntimeSession(ev.session);
        if (ev.session.permission_mode) {
          useAppStore.setState({ permissionMode: fromBackendPermissionMode(ev.session.permission_mode) });
        }
        return true;
      }
      const snapshotTodos = todosFromSnapshot(ev);
      if (snapshotTodos) {
        const currentState = useAppStore.getState();
        const visibleTodos = conversationId && conversationId !== currentState.conversationId
          ? currentState.conversationAgentStates?.[conversationId]?.todos ?? []
          : currentState.todos;
        s.setTodos(mergeRegressiveTodoSnapshot(snapshotTodos, visibleTodos), conversationId);
        return true;
      }
      if (!("todo_id" in ev) || !ev.todo_id) return true;

      const currentState = useAppStore.getState();
      const visibleTodos = conversationId && conversationId !== currentState.conversationId
        ? currentState.conversationAgentStates?.[conversationId]?.todos ?? []
        : currentState.todos;
      const existing = visibleTodos.find((todo) => todo.id === ev.todo_id);
      if (existing) {
        const patch = todoPatchFromEvent(ev, existing.activeForm);
        if (
          existing.status !== patch.status ||
          existing.content !== patch.content ||
          existing.activeForm !== patch.activeForm
        ) {
          s.updateTodo(ev.todo_id, patch, conversationId);
        }
      } else {
        const patch = todoPatchFromEvent(ev, "");
        s.addTodo({
          id: ev.todo_id,
          ...patch,
        }, conversationId);
      }
      return true;
    }
    case "subagent.start": {
      const ev = e as SubagentStartEvent;
      const record = maybeObject(ev.record);
      const currentSubagents = visibleSubagentsForConversation(conversationId);
      const existingById = currentSubagents.find((subagent) => subagent.id === ev.subagent_id);
      if (isStaleSubagentIncarnation(
        existingById,
        ev as unknown as Record<string, unknown>,
        record,
      )) return true;
      // A delayed start can arrive after the durable done event (the real
      // provider path can flush the start envelope after the child settles).
      // Starting it again would erase the terminal result and leave the UI
      // claiming that a completed worker is still running.  Only a fenced,
      // strictly newer incarnation may replace a terminal row.
      if (existingById && isTerminalSubagentStatus(existingById.status)) {
        const incoming = subagentIncarnation(ev as unknown as Record<string, unknown>, record);
        const newerIncarnation = Boolean(
          existingById.agentPath
          && typeof existingById.mailboxEpoch === "number"
          && incoming.agentPath
          && typeof incoming.mailboxEpoch === "number"
          && incoming.mailboxEpoch > existingById.mailboxEpoch,
        );
        if (!newerIncarnation) return true;
      }
      const metadata = subagentMetadataPatch(ev as unknown as Record<string, unknown>, record);
      const stableMetadata = {
        ...metadata,
        objective: metadata.objective ?? ev.prompt,
      };
      s.addSubagent({
        id: ev.subagent_id,
        role: ev.role || "subagent",
        status: "running",
        summary: ev.prompt,
        parentRunId: ev.parent_run_id,
        turnId: ev.turn_id,
        resultAvailable: false,
        resultContent: undefined,
        resultError: undefined,
        activityLog: [],
        terminationReason: undefined,
        terminationInitiator: undefined,
        ...transcriptSnapshotPatch(ev, existingById),
        ...stableMetadata,
      }, conversationId);
      addInspectorPayload("subagent", ev.subagent_id, {
        event: "subagent.start",
        role: ev.role,
        prompt: ev.prompt,
        parent_run_id: ev.parent_run_id,
        task_id: metadata.taskId,
        record: ev.record,
      });
      return true;
    }
    case "subagent.event": {
      const ev = e as SubagentEventEvent;
      const eventPayload = maybeObject(ev.event);
      const eventType = String(eventPayload?.type || "event");
      const message = maybeObject(eventPayload?.message);
      const task = maybeObject(eventPayload?.task);
      const output = maybeObject(eventPayload?.output);

      if (eventType === "message" && message) {
        const sender = maybeString(message.sender_id) ?? "agent";
        const recipient = maybeString(message.recipient_id) ?? ev.subagent_id;
        const content = maybeString(message.content) ?? "";
        const targetId = recipient === "parent" ? sender : recipient || ev.subagent_id;
        const currentState = useAppStore.getState();
        const visibleSubagents = conversationId && conversationId !== currentState.conversationId
          ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
          : currentState.subagents;
        const existing = visibleSubagents.find((subagent) => subagent.id === targetId);
        if (existing) {
          const normalizedMessage = swarmMessageState(message);
          s.updateSubagent(targetId, {
            lastEventAt: Date.now(),
            messages: mergeSubagentMessages(existing.messages, [normalizedMessage]),
          }, conversationId);
        }
      } else if (task) {
        const taskId = maybeString(task.task_id) ?? ev.subagent_id;
        const assignee = maybeString(task.assignee);
        const title = maybeString(task.title) ?? taskId;
        const status = maybeString(task.status) ?? "pending";
        const latestOutput = maybeString(output?.content);
        const taskMetadata = {
          ...task,
          task_id: taskId,
          current_activity: eventType.replace("_", " "),
        };
        const metadata = subagentMetadataPatch(taskMetadata, null);
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const targetId = assignee || ev.subagent_id || taskId;
        const subagentStatus = subagentStatusFromTask(status);
        const summary = `${eventType.replace("_", " ")}: [${status}] ${title}`;
        const existing = visibleSubagents.find((subagent) => subagent.id === targetId);
        if (existing) {
          s.updateSubagent(targetId, {
            status: subagentStatus,
            summary,
            detail: latestOutput || maybeString(task.description),
            resultAvailable: Boolean(latestOutput) || existing.resultAvailable,
            ...metadata,
          }, conversationId);
        }
      }
      addInspectorPayload("subagent", ev.subagent_id || "swarm", {
        event: "subagent.event",
        payload: ev.event,
      });
      return true;
    }
    case "subagent.progress": {
      const ev = e as SubagentProgressEvent;
      const subagentId = String(ev.subagent_id || "").trim();
      if (!subagentId) return true;
      const currentState = useAppStore.getState();
      const visibleSubagents = conversationId && conversationId !== currentState.conversationId
        ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
        : currentState.subagents;
      const existing = visibleSubagents.find((subagent) => subagent.id === subagentId);
      if (isStaleSubagentIncarnation(
        existing,
        ev as unknown as Record<string, unknown>,
      )) return true;
      const now = Date.now();
      const lastProgressAt = maybeNumber((ev as unknown as Record<string, unknown>).last_progress_at);
      if (existing && isTerminalSubagentStatus(existing.status)) {
        addInspectorPayload("subagent", subagentId, {
          event: "subagent.progress",
          ignored: "terminal_state_is_sticky",
          terminal_status: existing.status,
          last_progress_at: lastProgressAt,
          detail: ev.detail,
        });
        return true;
      }
      const currentActivity = userVisibleSubagentProgress(ev.activity_summary, ev.user_visible)
        || userVisibleSubagentProgress(ev.current_activity, ev.user_visible)
        || userVisibleSubagentProgress(ev.detail, ev.user_visible)
        || undefined;
      const reportedStatus = maybeString((ev as unknown as Record<string, unknown>).status);
      const progressStatus: SubagentState["status"] = reportedStatus === "pending" || reportedStatus === "running" || reportedStatus === "blocked"
        ? reportedStatus
        : "running";
      const detail = userVisibleSubagentProgress(ev.detail, ev.user_visible) || undefined;
      const waitingOn = userVisibleSubagentProgress(
        maybeString((ev as unknown as Record<string, unknown>).waiting_on),
        ev.user_visible,
      ) || (ev.tool_name ? "tool" : undefined);
      const patch = {
        status: progressStatus,
        summary: subagentProgressSummary(ev),
        iteration: ev.iteration,
        maxIterations: ev.max_iterations,
        currentTool: ev.tool_name,
        currentToolCallId: maybeString((ev as unknown as Record<string, unknown>).tool_call_id),
        progressSource: maybeString((ev as unknown as Record<string, unknown>).source_event_type),
        detail,
        currentActivity,
        activityLog: appendSubagentActivity(
          existing,
          explicitSubagentActivity(
            ev.current_activity,
            ev.detail,
            ev.activity_summary,
            ev.user_visible,
          ),
        ),
        waitingOn,
        lastEventAt: now,
        lastProgressAt: lastProgressAt ?? now,
        messages: mergeSubagentMessages(existing?.messages, snapshotMessages(e)),
        ...transcriptSnapshotPatch(ev, existing),
      };
      if (existing) {
        s.updateSubagent(subagentId, patch, conversationId);
      } else {
        s.addSubagent({ id: subagentId, role: "subagent", ...patch }, conversationId);
      }
      addInspectorPayload("subagent", subagentId, {
        event: "subagent.progress",
        iteration: ev.iteration,
        max_iterations: ev.max_iterations,
        tool_name: ev.tool_name,
        tool_call_id: maybeString((ev as unknown as Record<string, unknown>).tool_call_id),
        source_event_type: maybeString((ev as unknown as Record<string, unknown>).source_event_type),
        detail: ev.detail,
        activity_kind: ev.activity_kind,
        activity_summary: ev.activity_summary,
        user_visible: ev.user_visible,
      });
      return true;
    }
    case "subagent.done": {
      const result = subagentResultPayload(e);
      const resultContent = maybeString(result?.content);
      const resultError = maybeString(result?.error);
      const durationMs = maybeNumber(result?.duration_ms) ?? maybeNumber(e.duration_ms);
      const toolCallCount = maybeNumber(result?.tool_call_count) ?? maybeNumber(e.tool_call_count);
      const resultStatus = maybeString(result?.status);
      const eventStatus = maybeString((e as unknown as Record<string, unknown>).status) || resultStatus;
      const terminationReason = maybeString((e as unknown as Record<string, unknown>).termination_reason);
      const terminationInitiator = maybeString((e as unknown as Record<string, unknown>).initiator);
      const checkpointId = maybeString((e as unknown as Record<string, unknown>).checkpoint_id);
      const timedOut = Boolean(e.timed_out);
      const eventError = maybeString((e as unknown as Record<string, unknown>).error);
      const failed = Boolean(eventError || resultError || eventStatus === "failed" || eventStatus === "error");
      const uiStatus = eventStatus === "partial" || timedOut
        ? "partial"
        : eventStatus === "cancelled"
          ? "cancelled"
          : failed
            ? "error"
            : "done";
      const summary = eventError || e.summary;
      const record = maybeObject((e as unknown as { record?: unknown }).record);
      const metadata = subagentMetadataPatch(e as unknown as Record<string, unknown>, record);
      const currentState = useAppStore.getState();
      const visibleSubagents = conversationId && conversationId !== currentState.conversationId
        ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
        : currentState.subagents;
      // Completion events may use the runtime subagent id while task snapshots
      // were keyed by assignee/task id. Resolve by stable task metadata first
      // so a completed worker cannot leave its original row stuck at running.
      const existing = visibleSubagents.find((subagent) =>
        subagent.id === e.subagent_id || (metadata.taskId && subagent.taskId === metadata.taskId),
      );
      if (isStaleSubagentIncarnation(
        existing,
        e as unknown as Record<string, unknown>,
        record,
      )) return true;
      const targetSubagentId = existing?.id || e.subagent_id;
      const existingTerminal = Boolean(
        existing && ["done", "partial", "cancelled", "error"].includes(existing.status),
      );
      const transcriptPatch = transcriptSnapshotPatch(e, existing);
      const hasTranscriptUpdate = "transcriptSeq" in transcriptPatch;
      const duplicateTerminal = existingTerminal && (
        (!resultContent && Boolean(existing?.resultContent || existing?.resultError))
        || (Boolean(resultContent) && resultContent === existing?.resultContent
          && resultError === (existing?.resultError || ""))
      );
      if (duplicateTerminal && !hasTranscriptUpdate) return true;
      const activityLog = existing?.activityLog ?? [];
      const messages = mergeSubagentMessages(existing?.messages, snapshotMessages(e));
      if (existing) {
        s.updateSubagent(targetSubagentId, {
          status: uiStatus,
          summary: summary || existing.summary,
          resultContent,
          resultError,
          durationMs,
          toolCallCount,
          resultAvailable: Boolean(resultContent || resultError),
          terminationReason: timedOut ? "deadline_exceeded" : terminationReason,
          terminationInitiator,
          checkpointId,
          activityLog,
          messages,
          ...transcriptPatch,
          ...metadata,
        }, conversationId);
      } else if (e.subagent_id !== "parallel-batch") {
        s.addSubagent({
          id: e.subagent_id,
          role: "subagent",
          status: uiStatus,
          summary: summary || "",
          resultContent,
          resultError,
          durationMs,
          toolCallCount,
          resultAvailable: Boolean(resultContent || resultError),
          terminationReason: timedOut ? "deadline_exceeded" : terminationReason,
          terminationInitiator,
          checkpointId,
          activityLog,
          messages,
          ...transcriptPatch,
          ...metadata,
        }, conversationId);
      }
      addInspectorPayload("subagent", e.subagent_id, {
        event: "subagent.done",
        summary: e.summary,
        error: e.error,
        record: (e as unknown as { record?: unknown }).record,
        prompt_cache_fork: (e as unknown as { prompt_cache_fork?: unknown }).prompt_cache_fork,
        cancel_requested: (e as unknown as { cancel_requested?: boolean }).cancel_requested,
        cancelled: (e as unknown as { cancelled?: boolean }).cancelled,
        status: eventStatus,
        termination_reason: terminationReason,
        initiator: terminationInitiator,
        checkpoint_id: checkpointId,
      });
      return true;
    }
    case "budget_update": {
      const used = e.used as number | undefined;
      const total = e.total as number | undefined;
      const breakdown = e.breakdown as Record<string, number> | undefined;
      const percent = used != null && total != null && total > 0 ? used / total : 0;
      addInspectorPayload("budget", `budget:${conversationId || "unowned"}`, {
        event: "budget_update",
        conversation_id: conversationId,
        used,
        total,
        percent,
        breakdown: breakdown ?? {},
        replayed: isReplayedRuntimeEvent(e),
      });
      if (!isActiveConversationEvent(conversationId)) return true;
      if (used != null && total != null) {
        const currentUsage = useAppStore.getState().contextUsage;
        const buckets = breakdown
          ? Object.entries(breakdown).map(([name, tokens]) => ({ name, used: tokens, limit: total }))
          : [];
        s.setBudget(buckets, percent);
        s.setContextUsage({
          used,
          limit: total,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
          ledger: currentUsage?.ledger,
        });
      }
      return true;
    }
    case "budget.warning": {
      addInspectorPayload("budget", `budget-warning:${conversationId || "unowned"}:${e.bucket}`, {
        event: "budget.warning",
        conversation_id: conversationId,
        bucket: e.bucket,
        percent: e.percent,
        will_compact: e.will_compact,
        replayed: isReplayedRuntimeEvent(e),
      });
      if (!isActiveConversationEvent(conversationId)) return true;
      const current = useAppStore.getState();
      s.setBudget(current.budgetBuckets, e.percent);
      // The backend decides when a budget warning is warranted. The renderer
      // only presents that event; it must not add its own percentage trigger.
      const percentLabel = Number.isFinite(e.percent)
        ? ` (${Math.round(e.percent * 100)}%)`
        : "";
      if (!isReplayedRuntimeEvent(e)) {
        pushToast(
          e.will_compact
          ? `已安排压缩上下文${percentLabel}`
          : `令牌预算提醒${percentLabel}`,
          "warning",
        );
      }
      return true;
    }
    case "subagent.mailbox": {
      const ev = e as SubagentMailboxEvent;
      const subagentId = String(ev.subagent_id || "").trim();
      if (!subagentId) return true;
      const visibleSubagents = visibleSubagentsForConversation(conversationId);
      const existing = visibleSubagents.find((subagent) => subagent.id === subagentId);
      if (existing && !isStaleSubagentIncarnation(
        existing,
        ev as unknown as Record<string, unknown>,
      )) {
        s.updateSubagent(subagentId, {
          mailboxEpoch: ev.mailbox_epoch ?? existing.mailboxEpoch,
          lastEventAt: Date.now(),
        }, conversationId);
      }
      addInspectorPayload("subagent", subagentId, {
        event: "subagent.mailbox",
        count: ev.count,
        high_water: ev.high_water,
        mailbox_epoch: ev.mailbox_epoch,
        stale_sealed: ev.stale_sealed,
      });
      return true;
    }
    case "subagent.plan_approval_requested": {
      const ev = e as SubagentPlanApprovalRequestedEvent;
      const subagentId = String(ev.subagent_id || "").trim();
      const requestId = String(ev.request_id || "").trim();
      if (!subagentId || !requestId || !conversationId) {
        // A prompt without an owner or routing ids can never be answered, so
        // record why it was dropped instead of stranding the teammate quietly.
        addInspectorPayload("subagent", subagentId || "plan_approval", {
          event: "subagent.plan_approval_requested",
          dropped: "missing_owner_or_request_id",
          request_id: requestId,
          conversation_id: conversationId,
        });
        return true;
      }
      const teammateName = String(ev.teammate_name || "").trim();
      const teamName = String(ev.team_name || "").trim();
      const planFilePath = String(ev.plan_file_path || "").trim();
      const planContent = String(ev.plan_content || "").trim();
      // The teammate is blocked on this decision until its own deadline turns
      // silence into a rejection, so it becomes a blocking prompt.
      s.setAskUser({
        requestId,
        conversationId,
        question: teammateName
          ? `子智能体 ${teammateName} 提交了计划，需要你批准后才能开始实现。`
          : "子智能体提交了计划，需要你批准后才能开始实现。",
        planReview: {
          subagentId,
          teammateName,
          teamName,
          plan_file_path: planFilePath,
          planContent,
        },
      });
      addInspectorPayload("subagent", subagentId, {
        event: "subagent.plan_approval_requested",
        request_id: requestId,
        teammate_name: teammateName,
        team_name: teamName,
        plan_file_path: planFilePath,
      });
      return true;
    }
    case "permission.mode.updated": {
      const ev = e as unknown as { mode?: string };
      if (ev.mode) {
        const permissionMode = fromBackendPermissionMode(ev.mode);
        writeLS(LS.permissionMode, permissionMode);
        const state = useAppStore.getState();
        if (permissionMode === "plan" && state.agentMode !== "plan") state.setAgentMode("plan");
        if (permissionMode !== "plan" && state.agentMode === "plan") state.setAgentMode("build");
        useAppStore.setState({ permissionMode });
      }
      return true;
    }
    case "runtime.capabilities": {
      const ev = e as unknown as { capabilities?: Parameters<typeof withDerivedCapabilitySummary>[0] };
      const capabilities = withDerivedCapabilitySummary(ev.capabilities) ?? null;
      s.setRuntimeCapabilities(capabilities);
      if (Array.isArray(capabilities?.skills)) {
        s.setAvailableSkills(normalizeSkillList(capabilities.skills));
      }
      if (Array.isArray(capabilities?.composer_commands)) {
        s.setSlashCommands(normalizeSlashCommands(capabilities.composer_commands));
      }
      return true;
    }
    case "mcp.lifecycle": {
      // Per-server lifecycle: update the matching connector compactly. Consumed
      // here (returns true) so it is never rendered as chat text.
      const ev = e as unknown as {
        server_name?: string;
        status?: string;
        phase?: McpServerStatus["phase"];
        message?: string;
        auth_status?: McpServerStatus["authStatus"];
        recoverable?: boolean;
        requires_user_action?: boolean;
        setup_hint?: string;
        docs_url?: string;
      };
      const name = String(ev.server_name ?? "").trim();
      if (!name) return true;
      const servers = useAppStore.getState().mcpServers;
      const idx = servers.findIndex((srv) => srv.name === name);
      const errorish = ev.phase === "failed" || ev.phase === "auth_required" || ev.phase === "expired";
      const patch = {
        ...(ev.status ? { status: ev.status as McpServerStatus["status"] } : {}),
        phase: ev.phase,
        authStatus: ev.auth_status,
        recoverable: ev.recoverable,
        requiresUserAction: ev.requires_user_action,
        setupHint: ev.setup_hint,
        docsUrl: ev.docs_url,
        lastError: errorish ? ev.message : undefined,
      };
      if (idx >= 0) {
        const next = servers.slice();
        next[idx] = { ...next[idx], ...patch };
        s.setMcpServers(next);
      } else {
        s.setMcpServers([...servers, { name, status: "offline", ...patch }]);
      }
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "mcp.lifecycle" });
      return true;
    }
    case "mcp.progress": {
      // Coarse connect/reconnect progress; stored on the server, not in chat.
      const ev = e as unknown as {
        server_name?: string;
        operation?: string;
        message?: string;
        progress?: number;
        status?: "running" | "completed" | "failed";
      };
      const name = String(ev.server_name ?? "").trim();
      if (!name) return true;
      const servers = useAppStore.getState().mcpServers;
      const idx = servers.findIndex((srv) => srv.name === name);
      if (idx < 0) return true;
      const next = servers.slice();
      next[idx] = {
        ...next[idx],
        progress: {
          operation: String(ev.operation ?? "operation"),
          message: ev.message,
          progress: typeof ev.progress === "number" ? ev.progress : undefined,
          status: ev.status ?? "running",
        },
      };
      s.setMcpServers(next);
      return true;
    }
    default:
      // Raw provider events are explicitly SDK-only. They remain available to
      // programmatic consumers without copying a potentially large provider
      // payload into the renderer store or fabricating user-facing activity.
      if (e.type === "stream_event") {
        const ev = e as StreamEventEvent;
        if (!ev.sdk_only && isActiveConversationEvent(conversationId)) {
          addInspectorPayload(
            "provider",
            `stream:${conversationId}:${String(e.event_id || e.seq || ev.event_type)}`,
            {
              event: ev.type,
              conversation_id: conversationId,
              provider: ev.provider,
              event_type: ev.event_type,
              sdk_only: false,
              data_keys: Object.keys(ev.data).slice(0, 50),
            },
          );
        }
        return true;
      }
      // Delivery acknowledgement is durable control-plane state. Subagent
      // start/progress/done events already own its user-facing projection.
      if (e.type === "parent.notifications") {
        const ev = e as ParentNotificationsEvent;
        if (isActiveConversationEvent(ev.conversation_id)) {
          addInspectorPayload(
            "session",
            `parent-notifications:${ev.conversation_id}:${ev.parent_run_id}`,
            {
              event: ev.type,
              conversation_id: ev.conversation_id,
              parent_run_id: ev.parent_run_id,
              delivered_count: ev.count,
              replayed: isReplayedRuntimeEvent(e),
              received_at: eventTimestampMs(e),
            },
          );
        }
        return true;
      }
      if (e.type === "session.state_changed") {
        const ev = e as SessionStateEvent;
        const isWorking = ev.state === "working";
        const targetConvId = maybeString(ev.conversation_id) || conversationId;
        if (!targetConvId) return true;
        // This signal owns conversation busy state, not the terminal status of
        // an individual assistant message. `done` remains authoritative for
        // completed/partial/failed message presentation.
        useAppStore.setState((current) => ({
          conversationStreaming: {
            ...current.conversationStreaming,
            [targetConvId]: isWorking,
          },
          ...(targetConvId === current.conversationId ? { isStreaming: isWorking } : {}),
        }));
        return true;
      }
      if (e.type === "rate_limit") {
        const ev = e as RateLimitEvent;
        if (!isActiveConversationEvent(conversationId)) return true;
        addInspectorPayload(
          "provider",
          `rate-limit:${conversationId}:${String(e.event_id || e.seq || ev.retry_at || ev.error_type)}`,
          {
            event: ev.type,
            conversation_id: conversationId,
            provider: ev.provider,
            error_type: ev.error_type,
            retry_after_seconds: ev.retry_after_seconds,
            retry_at: ev.retry_at,
            recoverable: ev.recoverable,
            message: ev.message,
          },
        );
        if (!isReplayedRuntimeEvent(e)) {
          pushToast(rateLimitMessage(ev), "warning", 5000);
        }
        return true;
      }
      return false;
  }
};
