import { useAppStore } from "../stores";
import type {
  AgentPhaseUpdatedEvent,
  AgentProgressEvent,
  AgentRunCompletedEvent,
  AgentRunStartedEvent,
  AgentRunUpdatedEvent,
  PlanStepUpdatedEvent,
  PlanUpdatedEvent,
  RateLimitEvent,
  RuntimeSpanEvent,
  ServerEvent,
  SessionStateEvent,
  StreamEventEvent,
  SubagentEventEvent,
  SubagentProgressEvent,
  SubagentStartEvent,
  TaskUpdateEvent,
  ToolUseSummaryEvent,
  VerificationResultEvent,
  VerificationStartedEvent,
} from "../protocol/events";
import type { McpServerStatus, ProgressContentBlock, SubagentState, TodoItem } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { withDerivedCapabilitySummary } from "../protocol/capabilities";
import { pushToast } from "../overlays/ToastContainer";
import { addInspectorPayload, maybeAutoRoutePanel } from "./displayRouting";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { normalizeContextLedger } from "./contextLedger";

const INTERNAL_SUBAGENT_PROGRESS_RE = /\bcall_[a-z0-9_-]{8,}\b/i;
const ELAPSED_ONLY_RE = /^\d+(?:\.\d+)?s elapsed$/i;

const userVisibleSubagentProgress = (value?: string): string => {
  const text = String(value ?? "").trim();
  if (
    !text
    || INTERNAL_SUBAGENT_PROGRESS_RE.test(text)
    || ELAPSED_ONLY_RE.test(text)
    || /^tool started\s*:/i.test(text)
  ) {
    return "";
  }
  return text;
};

const subagentProgressSummary = (ev: {
  detail?: string;
  tool_name?: string;
  iteration?: number;
  max_iterations?: number;
}): string => {
  const detail = userVisibleSubagentProgress(ev.detail);
  if (detail) return detail;
  const toolName = userVisibleSubagentProgress(ev.tool_name);
  if (toolName) return `Running ${toolName}`;
  return "正在执行子任务";
};

const subagentResultPayload = (event: unknown): Record<string, unknown> | null => {
  const direct = (event as { result?: unknown }).result;
  if (direct && typeof direct === "object") return direct as Record<string, unknown>;
  const snapshot = (event as { snapshot?: unknown }).snapshot;
  if (!snapshot || typeof snapshot !== "object") return null;
  const result = (snapshot as { result?: unknown }).result;
  return result && typeof result === "object" ? result as Record<string, unknown> : null;
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

const subagentMetadataPatch = (
  payload: Record<string, unknown>,
  record?: Record<string, unknown> | null,
): Partial<SubagentState> => {
  const value = (key: string, altKey?: string): unknown => payload[key] ?? (altKey ? payload[altKey] : undefined) ?? record?.[key] ?? (altKey ? record?.[altKey] : undefined);
  const now = Date.now();
  const patch: Partial<SubagentState> = { lastEventAt: now, lastProgressAt: now };
  const workflowId = maybeString(value("workflow_id"));
  const workflowName = maybeString(value("workflow_name"));
  const workflowMode = maybeString(value("workflow_mode"));
  const nodeId = maybeString(value("node_id"));
  const taskId = maybeString(value("task_id"));
  const objective = maybeString(value("objective")) ?? maybeString(value("prompt_summary"));
  const currentActivity = userVisibleSubagentProgress(maybeString(value("current_activity"))) || undefined;
  const waitingOn = userVisibleSubagentProgress(maybeString(value("waiting_on", "waitingOn"))) || undefined;
  const lastProgressAt = maybeNumber(value("last_progress_at", "lastProgressAt"));
  const order = maybeNumber(value("order"));
  const dependsOn = maybeStringList(value("depends_on"));
  const blockedBy = maybeStringList(value("blocked_by"));
  const writeScope = maybeStringList(value("write_scope"));
  const requiredForFinal = maybeBoolean(value("required_for_final"));
  const blocksFinalReply = maybeBoolean(value("blocks_final_reply", "blocksFinalReply"));
  const readOnly = maybeBoolean(value("read_only"));
  if (workflowId) patch.workflowId = workflowId;
  if (workflowName) patch.workflowName = workflowName;
  if (workflowMode) patch.workflowMode = workflowMode;
  if (nodeId) patch.nodeId = nodeId;
  if (taskId) patch.taskId = taskId;
  if (objective) patch.objective = objective;
  if (currentActivity) patch.currentActivity = currentActivity;
  if (waitingOn) patch.waitingOn = waitingOn;
  if (typeof lastProgressAt === "number") patch.lastProgressAt = lastProgressAt;
  if (typeof order === "number") patch.order = order;
  if (dependsOn) patch.dependsOn = dependsOn;
  if (blockedBy) patch.blockedBy = blockedBy;
  if (writeScope) patch.writeScope = writeScope;
  if (typeof requiredForFinal === "boolean") patch.requiredForFinal = requiredForFinal;
  if (typeof blocksFinalReply === "boolean") patch.blocksFinalReply = blocksFinalReply;
  else if (typeof requiredForFinal === "boolean") patch.blocksFinalReply = requiredForFinal;
  if (typeof readOnly === "boolean") patch.readOnly = readOnly;
  return patch;
};

const visibleSubagentsForConversation = (conversationId?: string): SubagentState[] => {
  const currentState = useAppStore.getState();
  return conversationId && conversationId !== currentState.conversationId
    ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
    : currentState.subagents;
};

const findWorkflowNodeSubagent = (
  subagents: SubagentState[],
  workflowId: string,
  nodeId?: string,
  taskId?: string,
): SubagentState | undefined =>
  subagents.find((subagent) =>
    Boolean(taskId && (subagent.id === taskId || subagent.taskId === taskId)) ||
    Boolean(workflowId && nodeId && subagent.workflowId === workflowId && subagent.nodeId === nodeId),
  );

const isActiveConversationEvent = (conversationId?: string): boolean => {
  if (!conversationId) return true;
  return useAppStore.getState().conversationId === conversationId;
};

const TURN_SCOPED_RUNTIME_EVENTS = new Set<string>([
  "agent.progress",
  "runtime.span",
  "agent.run.started",
  "agent.run.updated",
  "agent.run.completed",
  "agent.phase.updated",
  "verification.started",
  "verification.result",
  "plan_updated",
  "plan_step_updated",
  "task.update",
  "subagent.start",
  "subagent.event",
  "subagent.progress",
  "subagent.done",
]);

const normalizeRuntimeSpanStatus = (status?: string): AgentProgressEvent["status"] => {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "completed" || normalized === "success" || normalized === "done") return "completed";
  if (normalized === "failed" || normalized === "error" || normalized === "blocked" || normalized === "timeout") return "failed";
  if (normalized === "running" || normalized === "pending") return "running";
  return "info";
};

const runtimeSpanBasePhase = (ev: RuntimeSpanEvent): string => {
  const explicit = String(ev.phase || "").trim().toLowerCase();
  if (explicit) return explicit;
  return String(ev.event || "").split(".")[0]?.trim().toLowerCase() || "";
};

const runtimeSpanProgressPhase = (ev: RuntimeSpanEvent): AgentProgressEvent["phase"] => {
  const phase = runtimeSpanBasePhase(ev);
  const event = String(ev.event || "").toLowerCase();
  if (phase === "context") return "planning";
  if (phase === "provider" || phase === "model") return "model";
  if (phase === "tool" || event.startsWith("tool.")) return "tool";
  if (phase === "approval" || event.includes("approval")) return "approval";
  if (phase === "verification" || phase === "verify") return "verify";
  if (phase === "final") return "final";
  if (phase === "recovery" || phase === "recover") return "recover";
  if (phase === "iteration") return "iteration";
  if (phase === "subagent") return "subagent";
  if (phase === "workflow") return "workflow";
  if (phase === "cache") return "cache";
  return "status";
};

const runtimeSpanProgressStage = (ev: RuntimeSpanEvent): AgentProgressEvent["stage"] => {
  const phase = runtimeSpanProgressPhase(ev);
  if (phase === "tool") return "tool";
  if (phase === "approval") return "approval";
  if (phase === "verify") return "verification";
  if (phase === "final") return "final";
  if (phase === "planning" || phase === "model" || phase === "orienting") return "planning";
  return "status";
};

const runtimeSpanProgressId = (ev: RuntimeSpanEvent): string => {
  const toolCallId = String(ev.tool_call_id || "").trim();
  if (toolCallId) {
    return `${runtimeSpanProgressPhase(ev) === "approval" ? "approval" : "tool"}:${toolCallId}`;
  }
  const iterationId = String(ev.iteration_id || "").trim();
  const event = String(ev.event || "").toLowerCase();
  if (event.startsWith("provider.") || runtimeSpanBasePhase(ev) === "provider") {
    return `provider-request:${iterationId || ev.span_id}`;
  }
  if (event.startsWith("context.") || runtimeSpanBasePhase(ev) === "context") {
    return `context:${iterationId || ev.turn_id || ev.run_id || ev.span_id}`;
  }
  if (event.startsWith("subagent.") || runtimeSpanBasePhase(ev) === "subagent") {
    return `subagent:${ev.agent_id || ev.span_id}`;
  }
  if (event.startsWith("workflow.") || runtimeSpanBasePhase(ev) === "workflow") {
    return `workflow:${ev.agent_id || ev.span_id}`;
  }
  if (event.startsWith("cache.") || runtimeSpanBasePhase(ev) === "cache") {
    return ev.span_id.startsWith("cache:") ? ev.span_id : `cache:${ev.span_id}`;
  }
  return `span:${ev.span_id}`;
};

const humanizeRuntimeSpanEvent = (value: string): string =>
  String(value || "runtime span")
    .replace(/[._-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();

const runtimeSpanDetail = (ev: RuntimeSpanEvent): string | undefined => {
  const parts = [
    ev.waiting_on ? `waiting_on=${ev.waiting_on}` : "",
    ev.blocking_reason ? `blocking_reason=${ev.blocking_reason}` : "",
    typeof ev.duration_ms === "number" ? `duration_ms=${Math.max(0, Math.round(ev.duration_ms))}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : undefined;
};

const inspectorTargetForRuntimeSpan = (ev: RuntimeSpanEvent): { kind: "message" | "tool_call" | "provider" | "subagent" | "cache"; id: string } => {
  const toolCallId = String(ev.tool_call_id || "").trim();
  if (toolCallId) return { kind: "tool_call", id: toolCallId };
  const phase = runtimeSpanBasePhase(ev);
  if (phase === "provider" || String(ev.event || "").toLowerCase().startsWith("provider.")) {
    return { kind: "provider", id: ev.span_id };
  }
  if (phase === "subagent" || phase === "workflow") {
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
  const messages = !targetId
    ? state.messages
    : targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  return messages.some((message) =>
    message.id === messageId &&
    message.role === "assistant" &&
    Boolean(message.isStreaming || message.isThinkingStreaming),
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
  const s = useAppStore.getState();
  const messageId = eventMessageId(e);
  if (messageId && TURN_SCOPED_RUNTIME_EVENTS.has(e.type) && !hasStreamingAssistantForMessage(conversationId, messageId)) {
    return true;
  }
  switch (e.type) {
        case "agent.progress": {
      const ev = e as AgentProgressEvent;
      const message = String(ev.message ?? "").trim();
      // Don't show debug-visibility progress in chat timeline (e.g. iteration counters)
      const isDebug = ev.visibility === "debug";
      const showInChatTimeline = ev.visibility === "timeline";
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
          displayScope: ev.display_scope,
          panelHint: ev.panel_hint,
          requiresAttention: ev.requires_attention,
          ephemeral: ev.ephemeral,
        };
        if (showInChatTimeline) {
          if (ev.ephemeral && ev.group_id) {
            s.replaceEphemeralProgress(progress, conversationId, messageId);
          } else {
            s.appendProgress(progress, conversationId, messageId);
          }
        }
        s.appendAgentProgress(progress, conversationId);
        maybeAutoRoutePanel(ev);
      }
      return true;
    }
    case "runtime.span": {
      const ev = e as RuntimeSpanEvent;
      const spanId = String(ev.span_id || "").trim();
      if (!spanId) return true;

      const stage = runtimeSpanProgressStage(ev);
      const phase = runtimeSpanProgressPhase(ev);
      const status = normalizeRuntimeSpanStatus(ev.status);
      const message = String(ev.summary || ev.label || humanizeRuntimeSpanEvent(ev.event)).trim();
      const basePhase = runtimeSpanBasePhase(ev);
      const hidden = Boolean(ev.debug_only || (basePhase === "cache" && ev.requires_attention !== true));
      const silent = ev.ui_visible === false;
      const visibility: ProgressContentBlock["visibility"] = hidden
        ? "debug"
        : stage === "tool" || silent
          ? "compact"
          : "timeline";
      const progress: Omit<ProgressContentBlock, "type" | "timestamp"> = {
        id: runtimeSpanProgressId(ev),
        stage,
        phase,
        status,
        message,
        label: ev.label || ev.tool_name || runtimeSpanBasePhase(ev) || undefined,
        summary: ev.summary,
        visibility,
        detail: runtimeSpanDetail(ev),
        toolCallId: ev.tool_call_id,
        toolName: ev.tool_name,
        groupId: ev.iteration_id,
        stepId: ev.tool_call_id || spanId,
        iterationId: ev.iteration_id,
        displayScope: ev.display_scope || (hidden || silent ? "silent" : "activity"),
        panelHint: ev.panel_hint || (stage === "approval" ? "diff" : "inspector"),
        requiresAttention: ev.requires_attention,
      };
      if (message) {
        if (visibility === "timeline") s.appendProgress(progress, conversationId, messageId);
        s.appendAgentProgress(progress, conversationId);
        maybeAutoRoutePanel({
          ...ev,
          display_scope: progress.displayScope,
          panel_hint: progress.panelHint,
          requires_attention: progress.requiresAttention,
        });
      }
      const target = inspectorTargetForRuntimeSpan(ev);
      addInspectorPayload(target.kind, target.id, {
        ...ev,
        span_event: ev.event,
        event: "runtime.span",
      });
      return true;
    }
    case "agent.run.started":
    case "agent.run.updated":
    case "agent.run.completed": {
      const ev = e as AgentRunStartedEvent | AgentRunUpdatedEvent | AgentRunCompletedEvent;
      const runId = String(ev.run_id || "").trim();
      if (!runId) return true;
      const status = ev.type === "agent.run.completed"
        ? (ev.status === "failed" || ev.status === "cancelled" ? "failed" : "completed")
        : "running";
      s.appendAgentProgress({
        id: `agent-run:${runId}`,
        stage: status === "completed" ? "final" : "planning",
        phase: ev.phase === "verify" ? "verify" : ev.phase === "final" ? "final" : "planning",
        status,
        message: ev.summary || (status === "running" ? "代理已启动" : "代理已完成"),
        label: ev.role || "代理",
        summary: ev.error || ev.summary,
        visibility: "timeline",
        displayScope: ev.display_scope,
        panelHint: ev.panel_hint,
        requiresAttention: ev.requires_attention,
      }, ev.conversation_id || conversationId);
      maybeAutoRoutePanel(ev);
      return true;
    }
    case "agent.phase.updated": {
      const ev = e as AgentPhaseUpdatedEvent;
      const runId = String(ev.run_id || "").trim();
      if (!runId) return true;
      const phase = ev.phase === "verify" ? "verify"
        : ev.phase === "final" ? "final"
          : ev.phase === "recover" ? "recover"
            : ev.phase === "execute" ? "tool"
              : "planning";
      s.appendAgentProgress({
        id: `agent-phase:${runId}:${ev.phase || "plan"}`,
        stage: phase === "verify" ? "verification" : phase === "final" ? "final" : "planning",
        phase,
        status: (ev.status === "completed" || ev.status === "failed" ? ev.status : "running"),
        message: ev.summary || `代理阶段：${ev.phase || "未知"}`,
        label: ev.role || "agent",
        summary: ev.summary,
        visibility: "timeline",
        displayScope: ev.display_scope,
        panelHint: ev.panel_hint,
        requiresAttention: ev.requires_attention,
      }, ev.conversation_id || conversationId);
      maybeAutoRoutePanel(ev);
      return true;
    }
    case "verification.started": {
      const ev = e as VerificationStartedEvent;
      const runId = String(ev.run_id || "").trim();
      if (!runId) return true;
      s.appendAgentProgress({
        id: `verification:${runId}`,
        stage: "verification",
        phase: "verify",
        status: "running",
        message: "正在验证",
        label: "verify",
        summary: ev.command,
        visibility: "timeline",
        displayScope: ev.display_scope,
        panelHint: ev.panel_hint,
        requiresAttention: ev.requires_attention,
      }, ev.conversation_id || conversationId);
      addInspectorPayload("message", `verification:${runId}`, {
        event: "verification.started",
        command: ev.command,
        run_id: runId,
      });
      maybeAutoRoutePanel(ev, "plan");
      return true;
    }
    case "verification.result": {
      const ev = e as VerificationResultEvent;
      const runId = String(ev.run_id || "").trim();
      if (!runId) return true;
      s.appendAgentProgress({
        id: `verification:${runId}`,
        stage: "verification",
        phase: "verify",
        status: ev.passed ? "completed" : "failed",
        message: ev.passed ? "验证通过" : "验证失败",
        label: "verify",
        summary: ev.command,
        detail: ev.output,
        visibility: "timeline",
        displayScope: ev.display_scope,
        panelHint: ev.panel_hint,
        requiresAttention: ev.requires_attention ?? !ev.passed,
      }, ev.conversation_id || conversationId);
      addInspectorPayload("message", `verification:${runId}`, {
        event: "verification.result",
        run_id: runId,
        passed: Boolean(ev.passed),
        command: ev.command,
        output: ev.output,
      });
      maybeAutoRoutePanel({ ...ev, requires_attention: ev.requires_attention ?? !ev.passed }, "plan");
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
      const metadata = subagentMetadataPatch(ev as unknown as Record<string, unknown>, record);
      const existingNode = findWorkflowNodeSubagent(
        visibleSubagentsForConversation(conversationId),
        metadata.workflowId ?? "",
        metadata.nodeId,
        metadata.taskId,
      );
      const stableMetadata = {
        ...metadata,
        order: metadata.order ?? existingNode?.order,
        workflowName: metadata.workflowName ?? existingNode?.workflowName,
        workflowMode: metadata.workflowMode ?? existingNode?.workflowMode,
        objective: metadata.objective ?? existingNode?.objective,
      };
      if (existingNode && existingNode.id !== ev.subagent_id && existingNode.role !== "workflow") {
        s.removeSubagent(existingNode.id, conversationId);
      }
      s.addSubagent({
        id: ev.subagent_id,
        role: ev.role || "subagent",
        status: "running",
        summary: ev.prompt,
        parentRunId: ev.parent_run_id,
        ...stableMetadata,
      }, conversationId);
      addInspectorPayload("subagent", ev.subagent_id, {
        event: "subagent.start",
        role: ev.role,
        prompt: ev.prompt,
        parent_run_id: ev.parent_run_id,
        workflow_id: metadata.workflowId,
        node_id: metadata.nodeId,
        task_id: metadata.taskId,
        record: ev.record,
      });
      if (isActiveConversationEvent(conversationId)) maybeAutoRoutePanel(ev, "subagents");
      return true;
    }
    case "subagent.event": {
      const ev = e as SubagentEventEvent;
      const eventPayload = maybeObject(ev.event);
      const eventType = String(eventPayload?.type || "event");
      const message = maybeObject(eventPayload?.message);
      const task = maybeObject(eventPayload?.task);
      const output = maybeObject(eventPayload?.output);
      const team = maybeObject(eventPayload?.team);

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
          s.updateSubagent(targetId, {
            currentActivity: activity,
            detail: existing.detail || content,
            lastEventAt: Date.now(),
          }, conversationId);
        }
      } else if (task) {
        const taskId = maybeString(task.task_id) ?? ev.subagent_id;
        const assignee = maybeString(task.assignee);
        const title = maybeString(task.title) ?? taskId;
        const status = maybeString(task.status) ?? "pending";
        const latestOutput = maybeString(output?.content);
        const workflowId = maybeString(task.workflow_id) ?? (maybeString(task.team_name)?.startsWith("workflow-") ? maybeString(task.team_name) : undefined);
        const taskMetadata = {
          ...task,
          task_id: taskId,
          workflow_id: workflowId,
          current_activity: eventType.replace("_", " "),
        };
        const metadata = subagentMetadataPatch(taskMetadata, null);
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const existingNode = findWorkflowNodeSubagent(
          visibleSubagents,
          metadata.workflowId ?? "",
          metadata.nodeId,
          metadata.taskId ?? taskId,
        );
        const targetId = existingNode?.id || assignee || ev.subagent_id || taskId;
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
        } else {
          s.addSubagent({
            id: targetId,
            role: maybeString(task.role) || assignee || "swarm task",
            status: subagentStatus,
            summary,
            detail: latestOutput || maybeString(task.description),
            resultAvailable: Boolean(latestOutput),
            ...metadata,
          }, conversationId);
        }
      } else if ((eventType === "team_created" || eventType === "team_deleted") && team) {
        const teamName = maybeString(team.team_name) ?? "team";
        const teamId = `team:${teamName}`;
        const members = Array.isArray(team.members) ? team.members : [];
        const deleted = eventType === "team_deleted";
        const summary = deleted
          ? `Team ${teamName} deleted`
          : `Team ${teamName}: ${members.length} member${members.length === 1 ? "" : "s"}`;
        const detail = members
          .map((member) => {
            const item = maybeObject(member);
            if (!item) return "";
            const id = maybeString(item.id) ?? "";
            const role = maybeString(item.role);
            const agentType = maybeString(item.agent_type);
            return [id, role, agentType ? `[${agentType}]` : ""].filter(Boolean).join(" ");
          })
          .filter(Boolean)
          .join("\n");
        const currentState = useAppStore.getState();
        const visibleSubagents = conversationId && conversationId !== currentState.conversationId
          ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
          : currentState.subagents;
        const existing = visibleSubagents.find((subagent) => subagent.id === teamId);
        if (existing) {
          s.updateSubagent(teamId, {
            status: deleted ? "done" : "running",
            summary,
            detail,
          }, conversationId);
        } else {
          s.addSubagent({
            id: teamId,
            role: "team",
            status: deleted ? "done" : "running",
            summary,
            detail,
          }, conversationId);
        }
      } else if (eventType === "workflow_started" && eventPayload) {
        const workflowId = maybeString(eventPayload.workflow_id) ?? (ev.subagent_id || "workflow");
        const name = maybeString(eventPayload.name) ?? "Workflow";
        const mode = maybeString(eventPayload.mode) ?? "workflow";
        const steps = Array.isArray(eventPayload.steps) ? eventPayload.steps : [];
        const ready = steps.filter((step) => Boolean(maybeObject(step)?.ready)).length;
        const blocked = Math.max(0, steps.length - ready);
        const summary = `${name} started (${mode}): ${ready}/${steps.length} ready, ${blocked} blocked`;
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const existing = visibleSubagents.find((subagent) => subagent.id === workflowId);
        const workflowPatch = {
          workflowId,
          workflowName: name,
          workflowMode: mode,
          objective: name,
          currentActivity: `Workflow mode: ${mode}`,
          lastEventAt: Date.now(),
        };
        if (existing) {
          s.updateSubagent(workflowId, {
            status: "running",
            summary,
            detail: `Workflow mode: ${mode}`,
            ...workflowPatch,
          }, conversationId);
        } else {
          s.addSubagent({
            id: workflowId,
            role: "workflow",
            status: "running",
            summary,
            detail: `Workflow mode: ${mode}`,
            ...workflowPatch,
          }, conversationId);
        }
        steps.forEach((step, index) => {
          const stepPayload = maybeObject(step);
          if (!stepPayload) return;
          const title = maybeString(stepPayload.title) ?? `Step ${index + 1}`;
          const taskId = maybeString(stepPayload.task_id);
          const nodeId = maybeString(stepPayload.node_id) ?? maybeString(stepPayload.step_id) ?? taskId ?? `step-${index + 1}`;
          const isReady = Boolean(stepPayload.ready);
          const metadata = subagentMetadataPatch({
            workflow_id: workflowId,
            workflow_name: name,
            workflow_mode: mode,
            node_id: nodeId,
            task_id: taskId,
            order: index,
            objective: maybeString(stepPayload.objective) ?? title,
            depends_on: maybeStringList(stepPayload.depends_on_nodes) ?? maybeStringList(stepPayload.depends_on),
            blocked_by: maybeStringList(stepPayload.blocked_by) ?? maybeStringList(stepPayload.depends_on),
            required_for_final: maybeBoolean(stepPayload.required_for_final),
            read_only: maybeBoolean(stepPayload.read_only),
            write_scope: maybeStringList(stepPayload.write_scope),
            current_activity: isReady ? "Ready / launched" : "Waiting on dependencies",
          }, null);
          const latestSubagents = visibleSubagentsForConversation(conversationId);
          const existingNode = findWorkflowNodeSubagent(latestSubagents, workflowId, nodeId, taskId);
          const nodeEntryId = existingNode?.id ?? taskId ?? `${workflowId}:${nodeId}`;
          const nodeStatus: SubagentState["status"] = existingNode?.status === "running" && isReady
            ? "running"
            : isReady ? "running" : "blocked";
          const nodeSummary = `${isReady ? "ready" : "blocked"}: ${title}`;
          if (existingNode) {
            s.updateSubagent(existingNode.id, {
              status: nodeStatus,
              summary: existingNode.summary || nodeSummary,
              detail: existingNode.detail || metadata.currentActivity,
              ...metadata,
            }, conversationId);
          } else {
            s.addSubagent({
              id: nodeEntryId,
              role: maybeString(stepPayload.role) ?? maybeString(stepPayload.agent_type) ?? "workflow step",
              status: nodeStatus,
              summary: nodeSummary,
              detail: metadata.currentActivity,
              ...metadata,
            }, conversationId);
          }
        });
      } else if (eventType === "workflow_completed" && eventPayload) {
        const workflowId = maybeString(eventPayload.workflow_id) ?? (ev.subagent_id || "workflow");
        const name = maybeString(eventPayload.workflow_name) ?? maybeString(eventPayload.name) ?? "Workflow";
        const mode = maybeString(eventPayload.workflow_mode) ?? maybeString(eventPayload.mode) ?? "workflow";
        const summary = maybeString(eventPayload.summary) ?? `${name} completed`;
        const resultContent = maybeString(eventPayload.result_content);
        const requiredCompleted = maybeNumber(eventPayload.required_completed);
        const requiredTotal = maybeNumber(eventPayload.required_total);
        const completedTotal = maybeNumber(eventPayload.completed_total);
        const total = maybeNumber(eventPayload.total);
        const counts = typeof requiredCompleted === "number" && typeof requiredTotal === "number"
          ? `${requiredCompleted}/${requiredTotal} required`
          : typeof completedTotal === "number" && typeof total === "number"
            ? `${completedTotal}/${total} completed`
            : "";
        const detail = counts ? `Workflow complete · ${counts}` : "Workflow complete";
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const existing = visibleSubagents.find((subagent) => subagent.id === workflowId);
        const patch = {
          status: "done" as const,
          summary,
          detail,
          resultContent,
          resultAvailable: Boolean(resultContent),
          workflowId,
          workflowName: name,
          workflowMode: mode,
          objective: name,
          currentActivity: detail,
          lastEventAt: Date.now(),
        };
        if (existing) {
          s.updateSubagent(workflowId, patch, conversationId);
        } else {
          s.addSubagent({
            id: workflowId,
            role: "workflow",
            ...patch,
          }, conversationId);
        }
      } else if ((eventType === "workflow_nodes_resumed" || eventType === "workflow_nodes_unblocked") && eventPayload) {
        const workflowId = maybeString(eventPayload.workflow_id) ?? (ev.subagent_id || "workflow");
        const tasks = Array.isArray(eventPayload.tasks) ? eventPayload.tasks : [];
        const launched = Boolean(eventPayload.launched);
        const launchError = maybeString(eventPayload.launch_error);
        const launchSummary = maybeString(eventPayload.launch_summary);
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const existing = visibleSubagents.find((subagent) => subagent.id === workflowId);
        const name = existing?.workflowName ?? workflowId;
        const mode = existing?.workflowMode ?? "workflow";
        const action = eventType === "workflow_nodes_resumed" ? "resumed" : "launched";
        const checkLabel = eventType === "workflow_nodes_resumed" ? "resume" : "unblock";
        const summary = launchError
          ? `${name} ${checkLabel} failed`
          : launched
            ? `${name} ${action} ${tasks.length} node${tasks.length === 1 ? "" : "s"}`
            : `${name} ${checkLabel} checked`;
        const detail = launchError || launchSummary || `${tasks.length} workflow node${tasks.length === 1 ? "" : "s"} ${action}`;
        const nextStatus: SubagentState["status"] = launchError
          ? "error"
          : launched
            ? "running"
            : existing?.status ?? "running";
        const patch = {
          status: nextStatus,
          summary,
          detail,
          workflowId,
          workflowName: name,
          workflowMode: mode,
          objective: existing?.objective ?? name,
          currentActivity: detail,
          lastEventAt: Date.now(),
        };
        if (existing) {
          s.updateSubagent(workflowId, patch, conversationId);
        } else {
          s.addSubagent({
            id: workflowId,
            role: "workflow",
            ...patch,
          }, conversationId);
        }
      } else if (eventType === "workflow_cancelled" && eventPayload) {
        const workflowId = maybeString(eventPayload.workflow_id) ?? (ev.subagent_id || "workflow");
        const name = maybeString(eventPayload.workflow_name) ?? maybeString(eventPayload.name) ?? "Workflow";
        const mode = maybeString(eventPayload.workflow_mode) ?? maybeString(eventPayload.mode) ?? "workflow";
        const summary = maybeString(eventPayload.summary) ?? `${name} cancelled`;
        const resultContent = maybeString(eventPayload.result_content);
        const cancelledTotal = maybeNumber(eventPayload.cancelled_total);
        const total = maybeNumber(eventPayload.total);
        const detail = typeof cancelledTotal === "number" && typeof total === "number"
          ? `Workflow cancelled · ${cancelledTotal}/${total} cancelled`
          : "Workflow cancelled";
        const visibleSubagents = visibleSubagentsForConversation(conversationId);
        const existing = visibleSubagents.find((subagent) => subagent.id === workflowId);
        const patch = {
          status: "cancelled" as const,
          summary,
          detail,
          resultContent,
          resultAvailable: Boolean(resultContent),
          terminationReason: "workflow_cancelled",
          terminationInitiator: maybeString(eventPayload.initiator) ?? "parent",
          workflowId,
          workflowName: name,
          workflowMode: mode,
          objective: name,
          currentActivity: detail,
          lastEventAt: Date.now(),
        };
        if (existing) {
          s.updateSubagent(workflowId, patch, conversationId);
        } else {
          s.addSubagent({
            id: workflowId,
            role: "workflow",
            ...patch,
          }, conversationId);
        }
      }

      addInspectorPayload("subagent", ev.subagent_id || "swarm", {
        event: "subagent.event",
        payload: ev.event,
      });
      if (isActiveConversationEvent(conversationId)) {
        maybeAutoRoutePanel(ev as unknown as { display_scope?: string; panel_hint?: string; requires_attention?: boolean }, "subagents");
      }
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
      const now = Date.now();
      const blocksFinalReply = maybeBoolean((ev as unknown as Record<string, unknown>).blocks_final_reply);
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
      const currentActivity = userVisibleSubagentProgress(ev.current_activity)
        || userVisibleSubagentProgress(ev.detail)
        || undefined;
      const detail = userVisibleSubagentProgress(ev.detail) || undefined;
      const waitingOn = userVisibleSubagentProgress(
        maybeString((ev as unknown as Record<string, unknown>).waiting_on),
      ) || (ev.tool_name ? "tool" : undefined);
      const patch = {
        status: "running" as const,
        summary: subagentProgressSummary(ev),
        iteration: ev.iteration,
        maxIterations: ev.max_iterations,
        currentTool: ev.tool_name,
        currentToolCallId: maybeString((ev as unknown as Record<string, unknown>).tool_call_id),
        progressSource: maybeString((ev as unknown as Record<string, unknown>).source_event_type),
        detail,
        currentActivity,
        waitingOn,
        blocksFinalReply,
        lastEventAt: now,
        lastProgressAt: lastProgressAt ?? now,
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
      });
      if (isActiveConversationEvent(conversationId)) maybeAutoRoutePanel(ev, "subagents");
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
      const failed = Boolean(e.error || resultError || eventStatus === "failed");
      const uiStatus = eventStatus === "partial"
        ? "partial"
        : eventStatus === "cancelled"
          ? "cancelled"
          : failed
            ? "error"
            : "done";
      const summary = e.error || e.summary || (e.timed_out ? "Timed out" : undefined);
      const record = maybeObject((e as unknown as { record?: unknown }).record);
      const metadata = subagentMetadataPatch(e as unknown as Record<string, unknown>, record);
      const currentState = useAppStore.getState();
      const visibleSubagents = conversationId && conversationId !== currentState.conversationId
        ? currentState.conversationAgentStates?.[conversationId]?.subagents ?? []
        : currentState.subagents;
      const existing = visibleSubagents.find((subagent) => subagent.id === e.subagent_id);
      if (existing) {
        s.updateSubagent(e.subagent_id, {
          status: uiStatus,
          summary: summary || existing.summary,
          resultContent,
          resultError,
          durationMs,
          toolCallCount,
          resultAvailable: Boolean(resultContent || resultError),
          terminationReason,
          terminationInitiator,
          checkpointId,
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
          terminationReason,
          terminationInitiator,
          checkpointId,
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
      if (isActiveConversationEvent(conversationId)) {
        maybeAutoRoutePanel(e as unknown as { display_scope?: string; panel_hint?: string; requires_attention?: boolean }, "subagents");
      }
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
      if (e.percent >= 0.9) {
        pushToast(`Token budget at ${Math.round(e.percent * 100)}%`, "warning");
      }
      return true;
    }
    case "permission.mode.updated": {
      const ev = e as unknown as { mode?: string };
      if (ev.mode) {
        useAppStore.setState({ permissionMode: fromBackendPermissionMode(ev.mode) });
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
      if (e.type === "session.state_changed") {
        const ev = e as unknown as SessionStateEvent;
        const isWorking = ev.state === "working";
        const setState = useAppStore.setState as (partial: Partial<typeof useAppStore.getState> | ((s: typeof useAppStore.getState) => Partial<typeof useAppStore.getState>)) => void;
        setState((s) => ({ ...s, _sessionState: ev.state }) as Partial<typeof useAppStore.getState>);
        if (!isWorking) {
          // On idle, ensure streaming state is cleaned up. If done/error
          // events were missed (network hiccup, backend crash), the UI
          // would be stuck in streaming forever without this fallback.
          const targetConvId = ev.conversation_id || conversationId;
          const state = useAppStore.getState();
          const hasStreaming = targetConvId
            ? state.conversationStreaming[targetConvId]
            : state.isStreaming;
          if (hasStreaming) {
            s.finishAgentProgress(targetConvId, "completed");
            s.finishStreaming(targetConvId, undefined, "completed");
          }
          s.appendAgentProgress({
            id: `session-state:${Date.now()}`,
            stage: "final",
            phase: "final",
            status: "info",
            message: ev.reason || "idle",
            label: "session",
            summary: ev.reason || "Session idle",
            visibility: "compact",
            displayScope: "silent",
          }, targetConvId);
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
            displayScope: "activity",
            toolCallIds: ev.tool_call_ids,
          }, conversationId, messageId);
        }
        return true;
      }
      return false;
  }
};
