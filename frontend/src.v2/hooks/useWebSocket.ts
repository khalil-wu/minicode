import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { wsProtocols, wsUrl } from "../protocol/api";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import {
  commandWithClientCommandId,
  rejectClientCommandResult,
  registerWebSocketSender,
} from "../protocol/ws-outbox";
export { commandWithClientCommandId } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { handleChatStreamEvent } from "../chat/chatStreamEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleControlEvent } from "../chat/controlEvents";
import { handleSessionEvent } from "../chat/sessionEvents";
import { handleArtifactEvent } from "../chat/artifactEvents";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { handlePreviewEvent } from "../chat/previewEvents";
import { handleDiffEvent } from "../chat/diffEvents";
import { handleNoticeEvent } from "../chat/noticeEvents";
import { createStreamBuffer } from "../lib/stream-buffer";
import { LS, readLS } from "../stores/shared-helpers";

// Transport policy follows Claude Code's WebSocket transport: reconnects are
// bounded by a total budget, use bounded jitter, and heartbeat liveness is
// decided by the matching pong rather than arbitrary inbound traffic.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const RECONNECT_BUDGET_MS = 600_000;
const RECONNECT_JITTER_FACTOR = 0.25;
const PING_INTERVAL_MS = 10_000;
const PONG_TIMEOUT_MS = 60_000;
const MAX_BUFFERED_CLIENT_COMMANDS = 1_000;
const PERMANENT_CLOSE_CODES = new Set([1002, 4001, 4003]);

export interface QueuedCommand {
  cmd: ClientCommand;
  queuedAt: number;
}

export const isTimeSensitiveCommand = (cmd: ClientCommand): boolean =>
  cmd.type === "approval"
  || cmd.type === "answer"
  || cmd.type === "control_response"
  || cmd.type === "control_cancel_request"
  || cmd.type === "interrupt";

export const coalescingKeyForClientCommand = (cmd: ClientCommand): string => {
  const typed = cmd as ClientCommand & {
    session_id?: unknown;
    source?: unknown;
    url?: unknown;
  };
  switch (cmd.type) {
    case "commands.list":
    case "connectors.marketplace.list":
    case "conversation.list":
    case "diff.git_staged":
    case "diff.git_working_tree":
    case "env.list":
    case "mcp.list":
    case "preview.detect":
    case "scheduler.list":
    case "session.usage.inspect":
    case "skills.list":
    case "skills.marketplace.list":
      return cmd.type;
    case "runtime.capabilities.inspect":
      return `${cmd.type}:${String(typed.source || "")}`;
    case "conversation.switch":
      return cmd.type;
    case "preview.navigate":
    case "preview.verify":
      return `${cmd.type}:${String(typed.url || "")}`;
    case "terminal.resize":
      return `${cmd.type}:${String(typed.session_id || "")}`;
    default:
      return "";
  }
};

export const isQueueableWhenOffline = (cmd: ClientCommand): boolean => {
  if (isTimeSensitiveCommand(cmd)) return true;
  return (
    cmd.type === "user_message"
    || cmd.type === "conversation.list"
    || cmd.type === "session.restore"
    || cmd.type === "commands.list"
    || cmd.type === "skills.list"
    || cmd.type === "interrupt"
  );
};

export const shouldReplayQueuedCommand = (
  queued: QueuedCommand,
  _now: number = Date.now(),
): boolean => {
  // A client command is durable once it has an id.  The server owns
  // de-duplication and ordering, so the browser must never discard a command
  // merely because a reconnect took longer than an invented local TTL.
  void queued;
  return true;
};

export interface WebSocketHandle {
  send: (cmd: ClientCommand) => boolean;
  close: () => void;
  sessionId: string;
  subscribe: (handler: (data: unknown) => void) => () => void;
}

export const workspaceRootForConversationRestore = (state: {
  conversationId?: string | null;
  conversations?: Array<{ id?: string; workspaceRoot?: string; worktreePath?: string }>;
}): string => {
  const conversationId = typeof state.conversationId === "string" ? state.conversationId : "";
  if (!conversationId) return "";
  const conversation = state.conversations?.find((item) => item.id === conversationId);
  return conversation?.worktreePath || conversation?.workspaceRoot || "";
};

export const conversationIdForSessionRestore = (state: {
  conversationId?: string | null;
  conversations?: Array<{ id?: string; archived?: boolean }>;
}): string => {
  const activeId = typeof state.conversationId === "string" ? state.conversationId.trim() : "";
  if (activeId) return activeId;
  const persistedId = (readLS(LS.conversation.activeId) || "").trim();
  if (!persistedId) return "";
  const conversations = state.conversations ?? [];
  if (conversations.length === 0) return persistedId;
  return conversations.some((item) => item.id === persistedId && !item.archived) ? persistedId : "";
};

let singleton: WebSocketHandle | null = null;
const subscribers = new Set<(data: unknown) => void>();
const BROWSER_SESSION_STORAGE_KEY = "minicode.websocket.session_id";
const browserSessionId = (() => {
  try {
    const existing = typeof window !== "undefined"
      ? window.sessionStorage.getItem(BROWSER_SESSION_STORAGE_KEY)?.trim()
      : "";
    if (existing && /^session_[A-Za-z0-9_-]+$/.test(existing)) return existing;
  } catch {
    // Fall through to an in-memory id when storage is unavailable.
  }
  const created = `session_${
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "")
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 18)}`
  }`;
  try {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(BROWSER_SESSION_STORAGE_KEY, created);
    }
  } catch {
    // The current page can still use the generated id for this lifetime.
  }
  return created;
})();
const RECENT_INBOUND_EVENT_IDS_MAX = 1024;
const NON_REPLAYABLE_CURSOR_EVENT_TYPES = new Set<string>([
  "conversation.list",
  "conversation.switched",
  "llm.model.updated",
  "mcp_status",
  "pong",
  "runtime.capabilities",
  "session.restored",
  "session.synced",
  "stream_resume",
]);

const recentInboundEventIds: string[] = [];
const recentInboundEventIdSet = new Set<string>();
const pendingClientCommandAckIdQueue: string[] = [];
const pendingClientCommands = new Map<string, ClientCommand>();
let lastReceivedServerSeq = 0;

export const getWebSocket = (): WebSocketHandle | null => singleton;

export const resetRecentInboundEventIdsForTests = () => {
  recentInboundEventIds.length = 0;
  recentInboundEventIdSet.clear();
  lastReceivedServerSeq = 0;
};

export const getLastReceivedServerSeqForTests = (): number => lastReceivedServerSeq;

export const shouldAdvanceReplayCursor = (event: ServerEvent): boolean => {
  const conversationId = (event as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof conversationId === "string" && conversationId.trim().length > 0
    && !NON_REPLAYABLE_CURSOR_EVENT_TYPES.has(event.type)
    && !event.type.startsWith("session.");
};

export const shouldProcessInboundEvent = (event: ServerEvent): boolean => {
  const eventId = event.event_id;
  if (typeof eventId !== "string" || !eventId) return true;
  return !recentInboundEventIdSet.has(eventId);
};

export const commitProcessedInboundEvent = (event: ServerEvent): void => {
  const seq = Number((event as { seq?: unknown }).seq);
  if (shouldAdvanceReplayCursor(event) && Number.isFinite(seq) && seq > lastReceivedServerSeq) {
    lastReceivedServerSeq = seq;
  }
  const eventId = event.event_id;
  if (typeof eventId !== "string" || !eventId || recentInboundEventIdSet.has(eventId)) return;
  recentInboundEventIdSet.add(eventId);
  recentInboundEventIds.push(eventId);
  while (recentInboundEventIds.length > RECENT_INBOUND_EVENT_IDS_MAX) {
    const removed = recentInboundEventIds.shift();
    if (removed) recentInboundEventIdSet.delete(removed);
  }
};

export const eventsFromSessionReplay = (event: ServerEvent): ServerEvent[] => {
  if (event.type !== "session.replay") return [];
  const events = Array.isArray((event as { events?: unknown }).events)
    ? (event as { events: unknown[] }).events
    : [];
  return events.flatMap((rawEvent) => {
    const replayed = normalizeInboundServerEvent(rawEvent);
    if (!replayed || replayed.type === "session.replay") return [];
    return [{ ...replayed, __replayed: true } as ServerEvent];
  });
};

export const resetPendingClientCommandAcksForTests = () => {
  pendingClientCommandAckIdQueue.length = 0;
  pendingClientCommands.clear();
};

export const getPendingClientCommandAckIdsForTests = (): string[] =>
  pendingClientCommandAckIdQueue.filter((id) => pendingClientCommands.has(id));

const pendingClientCommandPayloads = (): ClientCommand[] =>
  pendingClientCommandAckIdQueue
    .map((id) => pendingClientCommands.get(id))
    .filter((cmd): cmd is ClientCommand => Boolean(cmd));

const hasPendingClientCommand = (clientCommandId: string): boolean =>
  pendingClientCommands.has(clientCommandId);

export const getPendingClientCommandCountForTests = (): number => pendingClientCommands.size;

export const shouldTrackClientCommandAck = (cmd: ClientCommand): boolean =>
  cmd.type !== "ping";

const clearPendingClientCommandAck = (clientCommandId: string) => {
  pendingClientCommands.delete(clientCommandId);
  const index = pendingClientCommandAckIdQueue.indexOf(clientCommandId);
  if (index >= 0) pendingClientCommandAckIdQueue.splice(index, 1);
};

export const trackPendingClientCommandAck = (cmd: ClientCommand) => {
  if (!shouldTrackClientCommandAck(cmd)) return;
  if (typeof cmd.client_command_id !== "string" || !cmd.client_command_id) return;
  const existing = pendingClientCommands.get(cmd.client_command_id);
  if (existing) {
    pendingClientCommands.set(cmd.client_command_id, cmd);
    return;
  }
  if (pendingClientCommands.size >= MAX_BUFFERED_CLIENT_COMMANDS) return;
  pendingClientCommands.set(cmd.client_command_id, cmd);
  pendingClientCommandAckIdQueue.push(cmd.client_command_id);
};

export const acknowledgeClientCommand = (event: ServerEvent): boolean => {
  if (event.type !== "client.command.ack") return false;
  const ack = event as unknown as {
    client_command_id?: unknown;
    accepted?: unknown;
    reason?: unknown;
  };
  const clientCommandId = ack.client_command_id;
  if (typeof clientCommandId !== "string" || !clientCommandId) return true;
  clearPendingClientCommandAck(clientCommandId);
  if (ack.accepted === false) {
    rejectClientCommandResult(clientCommandId, String(ack.reason || "Command was rejected by the server"));
  }
  return true;
};

export const useWebSocketConnection = () => {
  const ref = useRef<WebSocket | null>(null);
  const queue = useRef<QueuedCommand[]>([]);
  const reconnectAttempt = useRef(0);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;
    let heartbeatCleanup = () => {};
    const coalescedCommands = new Map<string, ClientCommand>();
    const coalescedMicrotasks = new Set<string>();
    let awaitingSessionRestore = false;
    let reconnectStartedAt: number | null = null;
    let reconnectExhausted = false;

    let hasConnectedOnce = false;

    const bufferedCommandCount = (): number =>
      pendingClientCommands.size + queue.current.length + coalescedCommands.size;

    const enqueueOfflineCommand = (cmd: ClientCommand): boolean => {
      const command = commandWithClientCommandId(cmd);
      const clientCommandId = String(command.client_command_id || "");
      if (
        (clientCommandId && hasPendingClientCommand(clientCommandId))
        || queue.current.some((queued) => queued.cmd.client_command_id === clientCommandId)
        || Array.from(coalescedCommands.values()).some(
          (queued) => queued.client_command_id === clientCommandId,
        )
      ) {
        return true;
      }
      if (bufferedCommandCount() >= MAX_BUFFERED_CLIENT_COMMANDS) {
        // Returning false is the explicit backpressure signal. Silently
        // dropping a user command would make delivery unverifiable.
        return false;
      }
      queue.current.push({ cmd: command, queuedAt: Date.now() });
      return true;
    };

    const sendCommandNow = (cmd: ClientCommand): boolean => {
      const active = ref.current;
      if (!active || active.readyState !== WebSocket.OPEN) return false;
      const command = commandWithClientCommandId(cmd);
      const clientCommandId = String(command.client_command_id || "");
      if (
        shouldTrackClientCommandAck(command)
        && clientCommandId
        && !hasPendingClientCommand(clientCommandId)
        && pendingClientCommands.size >= MAX_BUFFERED_CLIENT_COMMANDS
      ) {
        return false;
      }
      try {
        active.send(JSON.stringify(command));
      } catch {
        return false;
      }
      trackPendingClientCommandAck(command);
      return true;
    };

    const flushCoalescedCommand = (key: string) => {
      const pending = coalescedCommands.get(key);
      coalescedCommands.delete(key);
      if (!pending) return;
      if (sendCommandNow(pending)) return;
      if (isQueueableWhenOffline(pending)) {
        enqueueOfflineCommand(pending);
      }
    };

    const sendOrCoalesceCommand = (cmd: ClientCommand): boolean => {
      if (isTimeSensitiveCommand(cmd)) {
        return sendCommandNow(cmd);
      }
      const key = coalescingKeyForClientCommand(cmd);
      if (!key) {
        return sendCommandNow(cmd);
      }
      const command = commandWithClientCommandId(cmd);
      if (!coalescedCommands.has(key) && bufferedCommandCount() >= MAX_BUFFERED_CLIENT_COMMANDS) {
        return false;
      }
      coalescedCommands.set(key, command);
      if (!coalescedMicrotasks.has(key)) {
        coalescedMicrotasks.add(key);
        const flush = () => {
          coalescedMicrotasks.delete(key);
          if (alive) flushCoalescedCommand(key);
        };
        if (typeof queueMicrotask === "function") queueMicrotask(flush);
        else void Promise.resolve().then(flush);
      }
      return true;
    };

    const flushOfflineQueue = () => {
      const now = Date.now();
      const pending = queue.current;
      queue.current = [];
      for (const queued of pending) {
        if (!shouldReplayQueuedCommand(queued, now)) continue;
        sendOrCoalesceCommand(queued.cmd);
      }
    };

    const replayPendingClientCommands = (): void => {
      for (const command of pendingClientCommandPayloads()) {
        if (!sendCommandNow(command)) break;
      }
    };

    const connect = () => {
      if (!alive || reconnectExhausted) return;
      timer = null;
      const url = wsUrl("/ws", { session_id: browserSessionId });
      const protocols = wsProtocols();
      const ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
      ref.current = ws;
      let pingInterval: number | null = null;
      let pongTimeout: number | null = null;
      let awaitingPong = false;

      const clearHeartbeatTimers = () => {
        if (pingInterval !== null) {
          window.clearInterval(pingInterval);
          pingInterval = null;
        }
        if (pongTimeout !== null) {
          window.clearTimeout(pongTimeout);
          pongTimeout = null;
        }
        awaitingPong = false;
      };
      heartbeatCleanup = clearHeartbeatTimers;

      ws.addEventListener("open", () => {
        if (!alive || ref.current !== ws) return;
        const wasReconnect = hasConnectedOnce || reconnectAttempt.current > 0;
        reconnectAttempt.current = 0;
        reconnectStartedAt = null;
        useAppStore.getState().setConnected(true);

        // 🆕 显示重连成功提示（如果不是首次连接）
        if (wasReconnect) {
          pushToast("已重新连接", "success", 2000);
        }

        const state = useAppStore.getState();
        const restoreConversationId = conversationIdForSessionRestore(state);
        const restoreWorkspaceRoot = restoreConversationId
          ? workspaceRootForConversationRestore({ ...state, conversationId: restoreConversationId })
          : "";
        awaitingSessionRestore = Boolean(hasConnectedOnce || restoreConversationId);
        if (awaitingSessionRestore) {
          textStreamBuffer.destroy();
          thinkingStreamBuffer.destroy();
          sendOrCoalesceCommand({
            type: "session.restore",
            ...(lastReceivedServerSeq > 0 ? { last_seq: lastReceivedServerSeq } : {}),
            ...(restoreConversationId ? { last_conversation_id: restoreConversationId } : {}),
            ...(restoreWorkspaceRoot ? { last_workspace_root: restoreWorkspaceRoot } : {}),
          });
        }
        sendOrCoalesceCommand({ type: "conversation.list" });

        if (!awaitingSessionRestore) {
          replayPendingClientCommands();
          flushOfflineQueue();
        }

        sendOrCoalesceCommand({ type: "commands.list" });
        hasConnectedOnce = true;

        pingInterval = window.setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          if (!sendCommandNow({ type: "ping" })) return;
          if (awaitingPong) return;
          awaitingPong = true;
          pongTimeout = window.setTimeout(() => {
            pongTimeout = null;
            if (alive && ref.current === ws && awaitingPong) ws.close();
          }, PONG_TIMEOUT_MS);
        }, PING_INTERVAL_MS);
      });

      ws.addEventListener("close", (event) => {
        if (!alive || ref.current !== ws) return;
        clearHeartbeatTimers();
        heartbeatCleanup = () => {};
        ref.current = null;
        useAppStore.getState().setConnected(false);
        if (hasConnectedOnce) {
          pushToast("Connection lost. Reconnecting...", "warning", Infinity);
        }

        const closeCode = Number((event as CloseEvent).code || 0);
        if (PERMANENT_CLOSE_CODES.has(closeCode)) {
          reconnectExhausted = true;
          return;
        }
        const now = Date.now();
        if (reconnectStartedAt === null) reconnectStartedAt = now;
        const remainingBudget = RECONNECT_BUDGET_MS - (now - reconnectStartedAt);
        if (remainingBudget <= 0) {
          reconnectExhausted = true;
          return;
        }
        const base = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
        );
        const jitter = base * ((Math.random() * 2 - 1) * RECONNECT_JITTER_FACTOR);
        const delay = Math.min(remainingBudget, Math.max(0, base + jitter));
        reconnectAttempt.current += 1;
        timer = window.setTimeout(connect, delay);
      });

      ws.addEventListener("error", () => {
        if (!alive || ref.current !== ws) return;
        try {
          if (ws.readyState === WebSocket.OPEN) ws.close();
        } catch {
          /* noop */
        }
      });

      ws.addEventListener("message", (ev) => {
        if (!alive || ref.current !== ws) return;
        let parsed: ServerEvent | null;
        try {
          parsed = normalizeInboundServerEvent(JSON.parse(ev.data));
        } catch {
          return;
        }
        if (!parsed) return;
        if (parsed.type === "pong") {
          awaitingPong = false;
          if (pongTimeout !== null) {
            window.clearTimeout(pongTimeout);
            pongTimeout = null;
          }
        }
        if (!shouldProcessInboundEvent(parsed)) return;
        acknowledgeClientCommand(parsed);
        const handled = processInboundEvent(parsed);
        if (handled && (parsed.type === "session.restored" || parsed.type === "session.synced")) {
          awaitingSessionRestore = false;
          replayPendingClientCommands();
          flushOfflineQueue();
        }
        if (handled) {
          for (const sub of subscribers) sub(parsed);
        }
      });
    };

    timer = window.setTimeout(connect, 0);

    singleton = {
      send: (cmd) => {
        const ws = ref.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          if (awaitingSessionRestore && cmd.type === "user_message") {
            return enqueueOfflineCommand(cmd);
          }
          return sendOrCoalesceCommand(cmd);
        }
        if (isQueueableWhenOffline(cmd)) {
          return enqueueOfflineCommand(cmd);
        }
        return false;
      },
      close: () => {
        ref.current?.close();
      },
      sessionId: browserSessionId,
      subscribe: (handler) => {
        subscribers.add(handler);
        return () => { subscribers.delete(handler); };
      },
    };
    registerWebSocketSender(singleton.send);

    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
      heartbeatCleanup();
      coalescedMicrotasks.clear();
      coalescedCommands.clear();
      const ownedSocket = ref.current;
      ref.current = null;
      ownedSocket?.close();
      singleton = null;
      registerWebSocketSender(null);
    };
  }, []);
};

const textStreamBuffer = createStreamBuffer((buffered, conversationId, itemId, _metadata, messageId) => {
  useAppStore.getState().appendAgentMessageDelta(
    itemId || "agent-message",
    buffered,
    conversationId,
    messageId,
  );
});

const thinkingStreamBuffer = createStreamBuffer((buffered, conversationId, _source, metadata, messageId) => {
  useAppStore.getState().appendThinkingChunk(buffered, conversationId, metadata, messageId);
});

const conversationIdFor = (e: ServerEvent): string | undefined => {
  const cid = (e as unknown as { conversation_id?: string }).conversation_id;
  if (typeof cid !== "string" || !cid) return undefined;
  return cid;
};

const handleServerEvent = (e: ServerEvent): boolean => {
  if (e.type === "session.replay") {
    for (const replayed of eventsFromSessionReplay(e)) {
      if (!shouldProcessInboundEvent(replayed)) continue;
      if (processInboundEvent(replayed)) {
        for (const sub of subscribers) sub(replayed);
      }
    }
    return true;
  }

  const cid = conversationIdFor(e);
  if (handleChatStreamEvent(e, cid, { textStreamBuffer, thinkingStreamBuffer })) return true;
  if (handleRuntimeEvent(e, cid)) return true;
  if (handleControlEvent(e)) return true;
  if (handleSessionEvent(e, { textStreamBuffer, thinkingStreamBuffer })) return true;
  if (handleArtifactEvent(e, cid)) return true;
  if (handleCommandCatalogEvent(e)) return true;
  if (handlePeripheralEvent(e)) return true;
  if (handleCommandResultEvent(e)) return true;
  if (handlePreviewEvent(e)) return true;
  if (handleNoticeEvent(e, cid)) return true;
  return handleDiffEvent(e);
};

// Advance the replay cursor only after the event has been applied.  This is
// the same durable-event rule used by the backend: a handler failure must be
// replayable after reconnect instead of being acknowledged by the cursor.
const processInboundEvent = (event: ServerEvent): boolean => {
  try {
    const handled = handleServerEvent(event);
    if (!handled) {
      console.warn("[ws] Known server event was not routed; leaving it replayable", event);
      return false;
    }
    commitProcessedInboundEvent(event);
    return handled;
  } catch (error) {
    console.error("[ws] Failed to apply server event; leaving it replayable", {
      event,
      error,
    });
    return false;
  }
};
