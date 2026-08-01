import { useAppStore } from "../stores";
import type {
  AgentProgressEvent,
  AgentRunCompletedEvent,
  AgentRunStartedEvent,
  PlanStepUpdatedEvent,
  PlanUpdatedEvent,
  RateLimitEvent,
  RuntimeSpanEvent,
  ServerEvent,
  SessionStateEvent,
  StreamEventEvent,
  SubagentEventEvent,
  SubagentMailboxEvent,
  SubagentProgressEvent,
  SubagentStartEvent,
  TaskUpdateEvent,
  ToolUseSummaryEvent,
} from "../protocol/events";
import type { McpServerStatus, SubagentMessageState, SubagentState, TodoItem } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { withDerivedCapabilitySummary } from "../protocol/capabilities";
import { pushToast } from "../overlays/ToastContainer";
import { addInspectorPayload } from "./inspectorEntries";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { normalizeContextLedger } from "./contextLedger";

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

const TURN_SCOPED_RUNTIME_EVENTS = new Set<string>([
  "agent.progress",
  "runtime.span",
  "agent.run.started",
  "agent.run.completed",
  "plan_updated",
  "plan_step_updated",
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
    return true;
  }
  switch (e.type) {
    case "agent.progress": {
      const ev = e as AgentProgressEvent;
      const message = String(ev.message ?? "").trim();
      // Main chat is owned by typed message/tool/approval events. Keep legacy
      // progress available to the activity/inspector surfaces without creating
      // a second transcript item for the same lifecycle.
      if (ev.tool_call_id) return true;
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
        s.appendAgentProgress(progress, conversationId);
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
      if (!isActiveConversationEvent(conversationId)) return true;
      const compacted = e as unknown as {
        summary?: string;
        ledger?: unknown;
      };
      const summary = compacted.summary ?? "Context compacted.";
      const currentUsage = useAppStore.getState().contextUsage;
      s.setContextUsage({
        used: currentUsage?.used ?? 0,
        limit: currentUsage?.limit ?? 0,
        compactedAt: Date.now(),
        compactSummary: summary,
        ledger: normalizeContextLedger(compacted.ledger) ?? currentUsage?.ledger,
      });
      sendClientCommand({ type: "session.usage.inspect" });
      s.upsertSystemMessage(
        "system-compact-status",
        "上下文已压缩，摘要已保存到会话记忆中。",
        { conversationId, replacePrefix: "Compacting context" },
      );
      return true;
    }
    case "context_ledger": {
      if (!isActiveConversationEvent(conversationId)) return true;
      const ledger = normalizeContextLedger((e as unknown as { data?: unknown }).data ?? e);
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
      if (!isActiveConversationEvent(conversationId)) return true;
      const data = (e as unknown as { data?: Record<string, unknown> }).data ?? {};
      const forkId = String(data.fork_id || "").trim();
      const historyLength = Number(data.history_length || 0);
      s.upsertSystemMessage(
        `context-forked:${forkId || Date.now()}`,
        `上下文已分叉${forkId ? `（${forkId}）` : ""}，保留 ${historyLength} 条历史消息。`,
        { conversationId },
      );
      return true;
    }
    case "context_side_query_result": {
      if (!isActiveConversationEvent(conversationId)) return true;
      const data = (e as unknown as { data?: Record<string, unknown> }).data ?? {};
      const result = String(data.result || "").trim();
      if (result) {
        s.upsertSystemMessage(
          `context-side-query:${Date.now()}`,
          result,
          { conversationId },
        );
      }
      return true;
    }
    case "plan_updated": {
      const ev = e as PlanUpdatedEvent;
      if (!Array.isArray(ev.steps)) return true;
      const validStatus = new Set(["pending", "running", "done", "skipped", "failed"]);
      const planId = ev.plan_id || "plan";
      const normalizedStatus = ev.status === "completed" || ev.status === "cancelled" || ev.status === "draft" || ev.status === "accepted"
        ? ev.status
        : "executing";
      s.setPlan({
        planId,
        status: normalizedStatus,
        currentStep: typeof ev.current_step === "number" ? ev.current_step : 0,
        steps: ev.steps.map((step, idx) => ({
          id: step.id || `step-${idx}`,
          title: step.title || `Step ${idx + 1}`,
          detail: step.detail,
          status: (step.status && validStatus.has(step.status) ? step.status : "pending") as
            "pending" | "running" | "done" | "skipped" | "failed",
        })),
      }, conversationId);
      // Keep plan snapshots in the dedicated plan/task UI. Mirroring every
      // snapshot into generic progress makes the process stream read like a
      // repeated narration of the same checklist.
      return true;
    }
    case "plan_step_updated": {
      const ev = e as PlanStepUpdatedEvent;
      const currentState = useAppStore.getState();
      const current = conversationId && conversationId !== currentState.conversationId
        ? currentState.conversationAgentStates?.[conversationId]?.plan
        : currentState.plan;
      if (!current || (ev.plan_id && current.planId !== ev.plan_id)) return true;
      const index = typeof ev.step_index === "number"
        ? ev.step_index
        : current.steps.findIndex((step) => step.id === ev.step_id);
      if (index < 0 || index >= current.steps.length || !ev.status) return true;
      const steps = current.steps.slice();
      steps[index] = {
        ...steps[index],
        status: ev.status,
        ...(ev.title ? { title: ev.title } : {}),
        ...(ev.detail ? { detail: ev.detail } : {}),
      };
      s.setPlan({
        ...current,
        steps,
        currentStep: ev.current_step ?? index,
        status: current.status === "completed" || current.status === "cancelled" ? current.status : "executing",
      }, conversationId);
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
        const activity = content ? `协作消息：${content}` : "协作消息";
        const currentState = useAppStore.getState();
        const visibleSubagents = conversationId && conversationId !== currentState.conversationId
          ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
          : currentState.subagents;
        const existing = visibleSubagents.find((subagent) => subagent.id === targetId);
        if (existing) {
          const normalizedMessage = swarmMessageState(message);
          s.updateSubagent(targetId, {
            currentActivity: activity,
            detail: existing.detail || content,
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
      const durationMs = maybeNumber(result?.duration_ms);
      const toolCallCount = maybeNumber(result?.tool_call_count);
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
      const duplicateTerminal = existingTerminal && (
        (!resultContent && Boolean(existing?.resultContent || existing?.resultError))
        || (Boolean(resultContent) && resultContent === existing?.resultContent
          && resultError === (existing?.resultError || ""))
      );
      if (duplicateTerminal) return true;
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
      if (!isActiveConversationEvent(conversationId)) return true;
      const used = e.used as number | undefined;
      const total = e.total as number | undefined;
      const breakdown = e.breakdown as Record<string, number> | undefined;
      if (used != null && total != null) {
        const currentUsage = useAppStore.getState().contextUsage;
        const buckets = breakdown
          ? Object.entries(breakdown).map(([name, tokens]) => ({ name, used: tokens, limit: 0 }))
          : [];
        const percent = total > 0 ? used / total : 0;
        s.setBudget(buckets, percent);
        s.setContextUsage({
          used,
          limit: total,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
        });
      }
      return true;
    }
    case "budget.warning": {
      if (!isActiveConversationEvent(conversationId)) return true;
      const current = useAppStore.getState();
      s.setBudget(current.budgetBuckets, e.percent);
      // The backend decides when a budget warning is warranted. The renderer
      // only presents that event; it must not add its own percentage trigger.
      const percentLabel = Number.isFinite(e.percent)
        ? ` (${Math.round(e.percent * 100)}%)`
        : "";
      pushToast(
        e.will_compact
          ? `Context compaction is scheduled${percentLabel}`
          : `Token budget warning${percentLabel}`,
        "warning",
      );
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
    case "permission.mode.updated": {
      const ev = e as unknown as { mode?: string };
      if (ev.mode) {
        const permissionMode = fromBackendPermissionMode(ev.mode);
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
      // SDK-only events: stream_event is forwarded but not rendered
      if (e.type === "stream_event") return true;
      // Delivery acknowledgement is durable control-plane state. Subagent
      // start/progress/done events already own its user-facing projection.
      if (e.type === "parent.notifications") return true;
      if (e.type === "session.state_changed") {
        const ev = e as unknown as SessionStateEvent;
        const isWorking = ev.state === "working";
        const setState = useAppStore.setState as (partial: Partial<typeof useAppStore.getState> | ((s: typeof useAppStore.getState) => Partial<typeof useAppStore.getState>)) => void;
        setState((s) => ({ ...s, _sessionState: ev.state }) as Partial<typeof useAppStore.getState>);
        if (!isWorking) {
          // Idle releases session busy state only. Message terminal ownership
          // belongs to the authoritative done event and may arrive later.
          const targetConvId = maybeString(ev.conversation_id) || conversationId;
          const state = useAppStore.getState();
          const hasStreaming = targetConvId ? state.conversationStreaming[targetConvId] : false;
          if (hasStreaming && targetConvId) {
            useAppStore.setState((current) => ({
              conversationStreaming: {
                ...current.conversationStreaming,
                [targetConvId]: false,
              },
              ...(targetConvId === current.conversationId ? { isStreaming: false } : {}),
            }));
          }
        }
        return true;
      }
      if (e.type === "rate_limit") {
        const ev = e as unknown as RateLimitEvent;
        const msg = ev.message || "模型暂时繁忙或达到并发限制";
        pushToast(msg, "warning", 5000);
        return true;
      }
      if (e.type === "tool_use_summary") {
        const ev = e as unknown as ToolUseSummaryEvent;
        const summary = String(ev.summary || "").trim();
        if (!conversationId) {
          addInspectorPayload("message", `unowned:tool-use-summary:${ev.iteration_id || "event"}`, {
            event: e.type,
            unowned: true,
            summary,
          });
          return true;
        }
        if (summary) {
          s.appendProcessItem({
            id: `tool-use-summary:${ev.iteration_id || Date.now()}`,
            itemKind: "action_summary",
            content: summary,
            title: "工具摘要",
            summary,
            source: "runtime",
            role: "runtime",
            status: "completed",
            visibility: "compact",
            toolCallIds: ev.tool_call_ids,
          }, conversationId, messageId);
        }
        return true;
      }
      return false;
  }
};
