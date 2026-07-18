import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { wsProtocols, wsUrl } from "../protocol/api";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { registerWebSocketSender } from "../protocol/ws-outbox";
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

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8000;
const RECONNECT_STABLE_MS = 5000;
const QUEUE_STALE_MS = 10_000;
const QUEUE_MAX_AGE_MS = 60_000;
const COMMAND_ACK_TIMEOUT_MS = 5000;
const COMMAND_COALESCE_DELAY_MS = 50;
const PING_INTERVAL_MS = 30_000;
const INBOUND_PROBE_DEADLINE_MS = 60_000;

export interface QueuedCommand {
  cmd: ClientCommand;
  queuedAt: number;
}

export const isTimeSensitiveCommand = (cmd: ClientCommand): boolean =>
  cmd.type === "approval"
  || cmd.type === "answer"
  || cmd.type === "control_response"
  || cmd.type === "control_cancel_request"
  || cmd.type === "approval.respond"
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
  now: number = Date.now(),
): boolean => {
  const age = now - queued.queuedAt;
  if (queued.cmd.type === "user_message") return true;
  if (age > QUEUE_MAX_AGE_MS) return false;
  if (isTimeSensitiveCommand(queued.cmd) && age > QUEUE_STALE_MS) return false;
  return true;
};

const createClientCommandId = (): string => {
  const randomPart = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  return `cmd_${randomPart}`;
};

export const commandWithClientCommandId = (cmd: ClientCommand): ClientCommand => {
  if (typeof cmd.client_command_id === "string" && cmd.client_command_id) {
    return cmd;
  }
  return { ...cmd, client_command_id: createClientCommandId() };
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
const PENDING_CLIENT_COMMAND_ACK_IDS_MAX = 512;
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
const pendingClientCommandAcks = new Map<string, ReturnType<typeof setTimeout> | null>();
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
  const seq = Number((event as { seq?: unknown }).seq);
  if (shouldAdvanceReplayCursor(event) && Number.isFinite(seq) && seq > lastReceivedServerSeq) {
    lastReceivedServerSeq = seq;
  }
  const eventId = event.event_id;
  if (typeof eventId !== "string" || !eventId) return true;
  if (recentInboundEventIdSet.has(eventId)) return false;
  recentInboundEventIdSet.add(eventId);
  recentInboundEventIds.push(eventId);
  while (recentInboundEventIds.length > RECENT_INBOUND_EVENT_IDS_MAX) {
    const removed = recentInboundEventIds.shift();
    if (removed) recentInboundEventIdSet.delete(removed);
  }
  return true;
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
  for (const timeoutId of pendingClientCommandAcks.values()) {
    if (timeoutId) clearTimeout(timeoutId);
  }
  pendingClientCommandAckIdQueue.length = 0;
  pendingClientCommandAcks.clear();
};

export const getPendingClientCommandAckIdsForTests = (): string[] =>
  Array.from(pendingClientCommandAcks.keys());

const shouldWarnOnMissingCommandAck = (cmd: ClientCommand): boolean =>
  isTimeSensitiveCommand(cmd)
  || cmd.type === "user_message"
  || cmd.type === "interrupt";

export const shouldTrackClientCommandAck = (cmd: ClientCommand): boolean =>
  cmd.type !== "ping";

const clearPendingClientCommandAck = (clientCommandId: string) => {
  const timeoutId = pendingClientCommandAcks.get(clientCommandId);
  if (timeoutId) clearTimeout(timeoutId);
  pendingClientCommandAcks.delete(clientCommandId);
};

export const trackPendingClientCommandAck = (cmd: ClientCommand) => {
  if (!shouldTrackClientCommandAck(cmd)) return;
  if (typeof cmd.client_command_id !== "string" || !cmd.client_command_id) return;
  clearPendingClientCommandAck(cmd.client_command_id);
  const timeoutId = shouldWarnOnMissingCommandAck(cmd)
    ? setTimeout(() => {
        pendingClientCommandAcks.delete(cmd.client_command_id as string);
        pushToast(
          "Command delivery was not confirmed. Check the connection before retrying.",
          "warning",
          5000,
        );
      }, COMMAND_ACK_TIMEOUT_MS)
    : null;
  pendingClientCommandAcks.set(cmd.client_command_id, timeoutId);
  pendingClientCommandAckIdQueue.push(cmd.client_command_id);
  while (pendingClientCommandAckIdQueue.length > PENDING_CLIENT_COMMAND_ACK_IDS_MAX) {
    const removed = pendingClientCommandAckIdQueue.shift();
    if (removed) clearPendingClientCommandAck(removed);
  }
};

export const acknowledgeClientCommand = (event: ServerEvent): boolean => {
  if (event.type !== "client.command.ack") return false;
  const clientCommandId = (event as unknown as { client_command_id?: unknown }).client_command_id;
  if (typeof clientCommandId !== "string" || !clientCommandId) return true;
  clearPendingClientCommandAck(clientCommandId);
  return true;
};

export const useWebSocketConnection = () => {
  const ref = useRef<WebSocket | null>(null);
  const queue = useRef<QueuedCommand[]>([]);
  const reconnectAttempt = useRef(0);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;
    let stableTimer: number | null = null;
    let heartbeatCleanup = () => {};
    const coalescedCommands = new Map<string, ClientCommand>();
    const coalescedTimers = new Map<string, number>();

    let hasConnectedOnce = false;

    const sendCommandNow = (cmd: ClientCommand): boolean => {
      const active = ref.current;
      if (!active || active.readyState !== WebSocket.OPEN) return false;
      const command = commandWithClientCommandId(cmd);
      active.send(JSON.stringify(command));
      trackPendingClientCommandAck(command);
      return true;
    };

    const flushCoalescedCommand = (key: string) => {
      const pending = coalescedCommands.get(key);
      coalescedCommands.delete(key);
      coalescedTimers.delete(key);
      if (!pending) return;
      if (sendCommandNow(pending)) return;
      if (isQueueableWhenOffline(pending)) {
        queue.current.push({ cmd: commandWithClientCommandId(pending), queuedAt: Date.now() });
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
      coalescedCommands.set(key, commandWithClientCommandId(cmd));
      const existingTimer = coalescedTimers.get(key);
      if (existingTimer !== undefined) window.clearTimeout(existingTimer);
      coalescedTimers.set(
        key,
        window.setTimeout(() => flushCoalescedCommand(key), COMMAND_COALESCE_DELAY_MS),
      );
      return true;
    };

    const connect = () => {
      if (!alive) return;
      timer = null;
      const url = wsUrl("/ws", { session_id: browserSessionId });
      const protocols = wsProtocols();
      const ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
      ref.current = ws;
      let pingInterval: number | null = null;
      let inboundProbeTimer: number | null = null;

      const clearInboundProbe = () => {
        if (inboundProbeTimer !== null) {
          window.clearTimeout(inboundProbeTimer);
          inboundProbeTimer = null;
        }
      };

      const markInboundActivity = () => {
        clearInboundProbe();
      };

      const clearHeartbeatTimers = () => {
        if (pingInterval !== null) {
          window.clearInterval(pingInterval);
          pingInterval = null;
        }
        clearInboundProbe();
      };
      heartbeatCleanup = clearHeartbeatTimers;

      ws.addEventListener("open", () => {
        if (!alive || ref.current !== ws) return;
        // Treat the connection as "stable" — and only then reset the backoff —
        // after it has stayed open for a few seconds. Resetting immediately lets
        // a flapping server (open→close→open) restart backoff from the base
        // every cycle, producing a reconnect storm.
        stableTimer = window.setTimeout(() => {
          reconnectAttempt.current = 0;
        }, RECONNECT_STABLE_MS);
        useAppStore.getState().setConnected(true);

        // 🆕 显示重连成功提示（如果不是首次连接）
        if (hasConnectedOnce && reconnectAttempt.current > 0) {
          pushToast("Connected", "success", 2000);
        }

        const state = useAppStore.getState();
        const restoreConversationId = conversationIdForSessionRestore(state);
        const restoreWorkspaceRoot = restoreConversationId
          ? workspaceRootForConversationRestore({ ...state, conversationId: restoreConversationId })
          : "";
        if (hasConnectedOnce || restoreConversationId) {
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

        // Replay the offline queue AFTER session.restore so a queued
        // user_message / control_response reaches the backend only once its
        // conversation + workspace context has been restored.
        const now = Date.now();
        for (const queued of queue.current) {
          if (!shouldReplayQueuedCommand(queued, now)) continue;
          sendOrCoalesceCommand(queued.cmd);
        }
        queue.current = [];

        sendOrCoalesceCommand({ type: "commands.list" });
        hasConnectedOnce = true;

        pingInterval = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            sendCommandNow({ type: "ping" });
            if (inboundProbeTimer === null) {
              inboundProbeTimer = window.setTimeout(() => {
                inboundProbeTimer = null;
                if (!alive || ref.current !== ws || ws.readyState !== WebSocket.OPEN) return;
                ws.close();
              }, INBOUND_PROBE_DEADLINE_MS);
            }
          }
        }, PING_INTERVAL_MS);
      });

      ws.addEventListener("close", () => {
        if (!alive || ref.current !== ws) return;
        clearHeartbeatTimers();
        heartbeatCleanup = () => {};
        ref.current = null;
        useAppStore.getState().setConnected(false);
        if (stableTimer !== null) {
          window.clearTimeout(stableTimer);
          stableTimer = null;
        }

        // 🆕 显示离线提示（如果已经连接过，且不是首次断开）
        if (hasConnectedOnce && reconnectAttempt.current === 0) {
          // 延迟 5 秒显示提示，避免短暂断线时闪烁
          setTimeout(() => {
            if (!useAppStore.getState().isConnected) {
              pushToast(
                "Connection lost. Reconnecting...",
                "warning",
                Infinity  // 持续显示直到重连
              );
            }
          }, 5000);
        }

        // Drop any pending approval / diff / ask-user prompt: its request id
        // dies with the socket, so the modal would be un-actionable (the stale
        // response is silently ignored) and would wedge the composer. The
        // backend re-emits any still-live prompt after session.restore.
        useAppStore.setState({
          pendingApproval: null,
          approvalQueue: [],
          pendingDiffReview: null,
          diffReview: null,
          pendingAskUser: null,
        });
        // Exponential backoff with jitter so many clients don't reconnect in
        // lockstep after a shared outage.
        const base = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
        );
        const delay = base * (1 + Math.random() * 0.3);
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
        markInboundActivity();
        let parsed: ServerEvent | null;
        try {
          parsed = normalizeInboundServerEvent(JSON.parse(ev.data));
        } catch {
          return;
        }
        if (!parsed) return;
        if (!shouldProcessInboundEvent(parsed)) return;
        acknowledgeClientCommand(parsed);
        handleServerEvent(parsed);
        for (const sub of subscribers) sub(parsed);
      });
    };

    timer = window.setTimeout(connect, 0);

    singleton = {
      send: (cmd) => {
        const ws = ref.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          return sendOrCoalesceCommand(cmd);
        }
        if (isQueueableWhenOffline(cmd)) {
          queue.current.push({ cmd: commandWithClientCommandId(cmd), queuedAt: Date.now() });
          return true;
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
      if (stableTimer !== null) window.clearTimeout(stableTimer);
      heartbeatCleanup();
      for (const timerId of coalescedTimers.values()) window.clearTimeout(timerId);
      coalescedTimers.clear();
      coalescedCommands.clear();
      const ownedSocket = ref.current;
      ref.current = null;
      ownedSocket?.close();
      singleton = null;
      registerWebSocketSender(null);
    };
  }, []);
};

const textStreamBuffer = createStreamBuffer((buffered, conversationId, source, metadata, messageId) => {
  useAppStore.getState().appendTextChunk(buffered, conversationId, source, metadata, messageId);
});

const thinkingStreamBuffer = createStreamBuffer((buffered, conversationId, _source, metadata, messageId) => {
  useAppStore.getState().appendThinkingChunk(buffered, conversationId, metadata, messageId);
});

const conversationIdFor = (e: ServerEvent): string | undefined => {
  const cid = (e as unknown as { conversation_id?: string }).conversation_id;
  if (typeof cid !== "string" || !cid) return undefined;
  return cid;
};

const handleServerEvent = (e: ServerEvent) => {
  if (e.type === "session.replay") {
    for (const replayed of eventsFromSessionReplay(e)) {
      if (!shouldProcessInboundEvent(replayed)) continue;
      handleServerEvent(replayed);
      for (const sub of subscribers) sub(replayed);
    }
    return;
  }

  const cid = conversationIdFor(e);
  if (handleChatStreamEvent(e, cid, { textStreamBuffer, thinkingStreamBuffer })) return;
  if (handleRuntimeEvent(e, cid)) return;
  if (handleControlEvent(e)) return;
  if (handleSessionEvent(e, { textStreamBuffer, thinkingStreamBuffer })) return;
  if (handleArtifactEvent(e, cid)) return;
  if (handleCommandCatalogEvent(e)) return;
  if (handlePeripheralEvent(e)) return;
  if (handleCommandResultEvent(e)) return;
  if (handlePreviewEvent(e)) return;
  if (handleNoticeEvent(e, cid)) return;
  handleDiffEvent(e);
};
