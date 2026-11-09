// A WebSocket stand-in that speaks the MiniCode control_v1 protocol. It only
// replaces the app-server socket at `/ws`; anything else (Vite HMR) still uses
// the native implementation.

import {
  DEMO_MODELS, DEMO_SESSION_ID, DEMO_WORKSPACE, DEMO_WORKSPACE_NAME, conversationSnapshot,
  conversationSummary, sessionSnapshot, sortedConversations, state, type Json,
} from "./state";
import { DEMO_COMMANDS, DEMO_SKILLS } from "./catalog";
import { runScenario, type TurnContext } from "./scenarios";

type Handler = ((event: Event) => void) | null;

let seq = 0;
const demoTrace: Json[] = [];
(window as unknown as { __demoTrace: Json[] }).__demoTrace = demoTrace;
let liveSocket: DemoSocket | null = null;
const activeTurns = new Map<string, TurnContext>();
const approvalWaiters = new Map<string, (decision: ApprovalDecision) => void>();

export type ApprovalDecision = { action: "approve" | "reject"; feedback?: string };

class DemoSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readyState = 0;
  url: string;
  protocol = "";
  extensions = "";
  bufferedAmount = 0;
  binaryType: BinaryType = "blob";
  onopen: Handler = null;
  onclose: Handler = null;
  onmessage: Handler = null;
  onerror: Handler = null;

  constructor(url: string) {
    super();
    this.url = url;
    liveSocket = this;
    window.setTimeout(() => {
      if (this.readyState !== 0) return;
      this.readyState = 1;
      this.fire(new Event("open"));
      this.emit({ type: "mcp_status", servers: [] });
      this.emit(modelUpdated());
    }, 40);
  }

  send(raw: string): void {
    if (this.readyState !== 1) return;
    let command: Json;
    try {
      command = JSON.parse(raw) as Json;
    } catch {
      return;
    }
    demoTrace.push({ dir: "out", type: command.type });
    // Answer asynchronously, like a real server round-trip.
    window.setTimeout(() => handleCommand(this, command), 8);
  }

  close(code = 1000): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    if (liveSocket === this) liveSocket = null;
    const event = new CloseEvent("close", { code, wasClean: true });
    this.fire(event);
  }

  emit(payload: Json): void {
    if (this.readyState !== 1) return;
    // No wire `seq`: demo events are transient and never take part in
    // replay-cursor continuity.
    seq += 1;
    const event = { ...payload, event_id: `${DEMO_SESSION_ID}:demo:${seq}`, timestamp: new Date().toISOString() };
    demoTrace.push({ dir: "in", type: payload.type });
    this.fire(new MessageEvent("message", { data: JSON.stringify(event) }));
  }

  private fire(event: Event): void {
    const handler = (this as unknown as Record<string, Handler>)[`on${event.type}`];
    try {
      handler?.call(this, event);
    } catch (error) {
      console.error(error);
    }
    this.dispatchEvent(event);
  }
}

/** Emit on whichever socket is currently connected. */
export const emit = (payload: Json): void => liveSocket?.emit(payload);

export const waitForApproval = (requestId: string): Promise<ApprovalDecision> =>
  new Promise((resolve) => approvalWaiters.set(requestId, resolve));

export const modelUpdated = (): Json => {
  const option = DEMO_MODELS.find((entry) => entry.model === state.model) ?? DEMO_MODELS[0];
  return {
    type: "llm.model.updated",
    provider: "anthropic",
    provider_id: "anthropic",
    wire_api: "messages",
    model: state.model,
    current_model: state.model,
    available_models: DEMO_MODELS.map((entry) => entry.model),
    available_model_options: DEMO_MODELS.map((entry) => ({
      model: entry.model, display_name: entry.label,
      reasoning_effort_levels: [...entry.efforts], default_reasoning_effort: entry.effort,
    })),
    models_source: "configured",
    reasoning_effort: state.effort,
    effective_reasoning_effort: state.effort || option.effort,
    working_directory: DEMO_WORKSPACE,
  };
};

const conversationList = (extra: Json = {}): Json => {
  const active = state.activeConversationId ? state.conversations.get(state.activeConversationId) : undefined;
  return {
    type: "conversation.list",
    conversation_id: state.activeConversationId,
    active_conversation_id: state.activeConversationId,
    conversations: sortedConversations().map(conversationSummary),
    active_conversation: active ? conversationSummary(active) : null,
    session: sessionSnapshot(),
    inventory_instance_id: "demo-inventory",
    inventory_revision: seq,
    ...extra,
  };
};

const reply = (command: Json): Json => ({
  client_command_id: command.client_command_id,
  client_command_type: command.type,
});

const commandResult = (command: Json, data: Json = {}, message = "", level = "success"): Json => ({
  type: "command.result",
  command: command.type,
  level,
  message,
  data: { ...data, client_command_id: command.client_command_id },
  ...reply(command),
});

const switchTo = (socket: DemoSocket, command: Json, conversationId: string | null): void => {
  state.activeConversationId = conversationId;
  const conversation = conversationId ? state.conversations.get(conversationId) : undefined;
  if (!conversation) {
    socket.emit({ type: "conversation.switched", conversation_id: null, conversation: null, session: sessionSnapshot(), ...reply(command) });
    return;
  }
  socket.emit({
    type: "conversation.switched",
    conversation_id: conversation.id,
    conversation: conversationSnapshot(conversation),
    is_hydrating: false,
    session: sessionSnapshot(),
    snapshot_at: new Date().toISOString(),
    ...reply(command),
  });
};

function handleCommand(socket: DemoSocket, command: Json): void {
  const type = String(command.type ?? "");
  if (type === "ping") {
    socket.emit({ type: "pong" });
    return;
  }
  if (type === "session.sync" || type === "session.restore") {
    const restore = type === "session.restore";
    const active = state.activeConversationId ? state.conversations.get(state.activeConversationId) : undefined;
    socket.emit({
      type: restore ? "session.restored" : "session.synced",
      session_id: DEMO_SESSION_ID,
      synced: true,
      ...(restore ? { restored: true, conversation: active ? conversationSnapshot(active) : null, messages: active?.transcript ?? [] } : {}),
      protocol_version: "1.0.0",
      active_conversation_id: state.activeConversationId,
      current_model: state.model,
      model: state.model,
      working_directory: DEMO_WORKSPACE,
      current_seq: 0,
      last_seq: 0,
      replayed_events: 0,
      event_log_gap: false,
      snapshot_required: false,
      cursor_reset: false,
      requested_last_seq: 0,
      session: sessionSnapshot(),
      ...reply(command),
    });
    return;
  }

  if (command.client_command_id) {
    socket.emit({ type: "client.command.ack", client_command_id: command.client_command_id, command_type: type, accepted: true });
  }

  switch (type) {
    case "conversation.list":
      socket.emit({ ...conversationList(), ...reply(command) });
      return;
    case "commands.list":
      socket.emit({ type: "commands.list", conversation_id: state.activeConversationId, commands: DEMO_COMMANDS, ...reply(command) });
      return;
    case "skills.list":
      socket.emit({ type: "skills.list", skills: DEMO_SKILLS, ...reply(command) });
      socket.emit(commandResult(command, { count: DEMO_SKILLS.length }, "", "info"));
      return;
    case "workspace.recent.list":
      socket.emit({ type: "workspace.recent.list", projects: recentProjects(), ...reply(command) });
      return;
    case "conversation.create": {
      const id = String(command.conversation_id || `conv_demo_${Date.now().toString(36)}`);
      const now = new Date().toISOString();
      state.conversations.set(id, {
        id, title: String(command.title || "新对话"), created_at: now, updated_at: now,
        workspace_root: String(command.workspace_root || DEMO_WORKSPACE), transcript: [],
      });
      socket.emit({ type: "workspace.recent.list", projects: recentProjects(), ...reply(command) });
      switchTo(socket, command, id);
      socket.emit({ ...conversationList(), ...reply(command) });
      socket.emit(commandResult(command, { conversation_id: id, workspace_root: DEMO_WORKSPACE, conversation_type: "main" }, "Conversation created."));
      return;
    }
    case "conversation.switch":
      switchTo(socket, command, typeof command.conversation_id === "string" ? command.conversation_id : null);
      socket.emit(commandResult(command, { conversation_id: command.conversation_id }));
      return;
    case "conversation.delete": {
      state.conversations.delete(String(command.conversation_id));
      if (state.activeConversationId === command.conversation_id) state.activeConversationId = null;
      socket.emit({ ...conversationList(), ...reply(command) });
      socket.emit(commandResult(command, { conversation_id: command.conversation_id }));
      return;
    }
    case "conversation.rename": {
      const conversation = state.conversations.get(String(command.conversation_id));
      if (conversation) conversation.title = String(command.title || conversation.title);
      socket.emit({ ...conversationList(), ...reply(command) });
      socket.emit(commandResult(command, { conversation_id: command.conversation_id }));
      return;
    }
    case "user_message":
      startTurn(socket, command);
      return;
    case "interrupt": {
      const conversationId = String(command.conversation_id || state.activeConversationId || "");
      activeTurns.get(conversationId)?.cancel();
      socket.emit(commandResult(command, { conversation_id: conversationId, interrupted: true }));
      return;
    }
    case "control_response": {
      const requestId = String(command.request_id ?? "");
      const response = (command.response as Json | undefined)?.response as Json | undefined;
      const action = response?.action === "reject" || response?.behavior === "deny" ? "reject" : "approve";
      approvalWaiters.get(requestId)?.({ action, feedback: typeof response?.feedback === "string" ? response.feedback : undefined });
      approvalWaiters.delete(requestId);
      socket.emit(commandResult(command, { request_id: requestId }));
      return;
    }
    case "conversation.pin.set": {
      const conversation = state.conversations.get(String(command.conversation_id));
      if (conversation) conversation.pinned = command.pinned === true;
      socket.emit({ ...conversationList(), ...reply(command) });
      socket.emit(commandResult(command, { conversation_id: command.conversation_id, pinned: command.pinned === true }));
      return;
    }
    case "llm.model.set": {
      if (typeof command.model === "string" && command.model) state.model = command.model;
      if (typeof command.reasoning_effort === "string" && command.reasoning_effort) state.effort = command.reasoning_effort;
      socket.emit({ ...modelUpdated(), ...reply(command) });
      socket.emit(commandResult(command, { model: state.model, reasoning_effort: state.effort, conversation_id: command.conversation_id }));
      return;
    }
    case "conversation.permission_mode.set": {
      if (typeof command.mode === "string") state.permissionMode = command.mode;
      socket.emit({ type: "permission.mode.updated", session_id: DEMO_SESSION_ID, mode: state.permissionMode, source: "user", ...reply(command) });
      socket.emit(commandResult(command, { permission_mode: state.permissionMode, conversation_id: command.conversation_id }));
      return;
    }
    default:
      if (command.client_command_id) socket.emit(commandResult(command));
  }
}

const recentProjects = (): Json[] => [
  { path: DEMO_WORKSPACE, name: DEMO_WORKSPACE_NAME, project_type: "node", last_opened: Date.now() / 1000 },
  { path: "/Users/demo/Projects/lumen-api", name: "lumen-api", project_type: "python", last_opened: Date.now() / 1000 - 86_400 },
  { path: "/Users/demo/Projects/field-notes", name: "field-notes", project_type: "unknown", last_opened: Date.now() / 1000 - 4 * 86_400 },
];

function startTurn(socket: DemoSocket, command: Json): void {
  const conversationId = String(command.conversation_id || state.activeConversationId || "");
  let conversation = state.conversations.get(conversationId);
  if (!conversation) {
    const now = new Date().toISOString();
    conversation = { id: conversationId, title: "新对话", created_at: now, updated_at: now, workspace_root: DEMO_WORKSPACE, transcript: [] };
    state.conversations.set(conversationId, conversation);
  }
  state.activeConversationId = conversationId;
  const content = String(command.display_content ?? command.content ?? "");
  const userMessageId = String(command.user_message_id || `u_demo_${Date.now().toString(36)}`);
  const assistantMessageId = String(command.assistant_message_id || `a_demo_${Date.now().toString(36)}`);
  const runId = `run_demo_${Date.now().toString(36)}`;
  const origin = reply(command);

  conversation.transcript.push({ id: userMessageId, role: "user", content, timestamp: new Date().toISOString(), attachments: [] });
  conversation.updated_at = new Date().toISOString();

  socket.emit({ type: "permission.mode.updated", session_id: DEMO_SESSION_ID, mode: state.permissionMode, source: "user_message", ...origin });
  socket.emit({
    type: "command.result", command: "user_message", level: "success", message: "",
    conversation_id: conversationId,
    data: { admitted: true, conversation_id: conversationId, user_message_id: userMessageId, assistant_message_id: assistantMessageId, run_id: runId },
    ...origin,
  });

  const context: TurnContext = createTurnContext({ conversationId, assistantMessageId, runId, content, origin });
  activeTurns.set(conversationId, context);
  void runScenario(context).finally(() => {
    if (activeTurns.get(conversationId) === context) activeTurns.delete(conversationId);
    const updated = state.conversations.get(conversationId);
    if (!updated) return;
    updated.transcript.push({
      id: assistantMessageId, role: "assistant", content: context.finalText,
      timestamp: new Date().toISOString(), terminal_status: context.cancelled ? "cancelled" : "completed",
      completed_at: Date.now(), usage: context.usage, blocks: context.blocks, tool_calls: context.toolCalls,
    });
    updated.updated_at = new Date().toISOString();
    if (updated.title === "新对话" || updated.title === "New chat") updated.title = context.title;
    emit({
      type: "conversation.summary.updated", conversation_id: conversationId, summary: "", title: updated.title,
      updated_at: updated.updated_at, memory_mode: "enabled", memory_polluted: false, memory_pollution_sources: [], ...origin,
    });
    emit({ ...conversationList(), ...origin });
  });
}

function createTurnContext(input: { conversationId: string; assistantMessageId: string; runId: string; content: string; origin: Json }): TurnContext {
  const timers = new Set<number>();
  const cancelWaiters = new Set<() => void>();
  const context: TurnContext = {
    ...input,
    owner: { conversation_id: input.conversationId, message_id: input.assistantMessageId, turn_id: input.runId, ...input.origin },
    cancelled: false,
    finalText: "",
    title: input.content.trim().slice(0, 24) || "新对话",
    usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    blocks: [],
    toolCalls: [],
    emit: (payload) => {
      if (!context.cancelled) emit({ ...payload, ...context.owner });
    },
    sleep: (ms) => new Promise<void>((resolve) => {
      if (context.cancelled) return resolve();
      const timer = window.setTimeout(() => {
        timers.delete(timer);
        resolve();
      }, ms);
      timers.add(timer);
      cancelWaiters.add(resolve);
    }),
    waitForApproval,
    cancel: () => {
      if (context.cancelled) return;
      context.cancelled = true;
      for (const timer of timers) window.clearTimeout(timer);
      timers.clear();
      for (const waiter of cancelWaiters) waiter();
      emit({ type: "agent.run.completed", run_id: input.runId, conversation_id: input.conversationId, role: "main", phase: "final", status: "cancelled", ...context.owner });
      emit({ type: "done", status: "cancelled", reason: "user_interrupted", usage: context.usage, ...context.owner });
      emit({ type: "session.state_changed", state: "idle", conversation_id: input.conversationId, reason: "agent_run_cancelled", ...input.origin });
    },
  };
  return context;
}

export function installDemoSocket(): void {
  const NativeWebSocket = window.WebSocket;
  const Patched = function (this: unknown, url: string | URL, protocols?: string | string[]) {
    const href = String(url);
    if (new URL(href, location.href).pathname !== "/ws") {
      return protocols ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
    }
    return new DemoSocket(href);
  } as unknown as typeof WebSocket;
  Object.assign(Patched, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  Patched.prototype = NativeWebSocket.prototype;
  window.WebSocket = Patched;
}
