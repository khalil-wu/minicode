import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { wsUrl } from "../protocol/api";
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
import { clearStreamingState } from "../chat/streamingState";

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8000;
const RECONNECT_STABLE_MS = 5000;
const QUEUE_STALE_MS = 10_000;
const QUEUE_MAX_AGE_MS = 60_000;
const COMMAND_ACK_TIMEOUT_MS = 5000;

export interface QueuedCommand {
  cmd: ClientCommand;
  queuedAt: number;
}

export const isTimeSensitiveCommand = (cmd: ClientCommand): boolean =>
  cmd.type === "approval"
  || cmd.type === "answer"
  || cmd.type === "control_response"
  || cmd.type === "control_cancel_request"
  || cmd.type === "approval.respond";

export const isQueueableWhenOffline = (cmd: ClientCommand): boolean => {
  if (isTimeSensitiveCommand(cmd)) return true;
  return (
    cmd.type === "user_message"
    || cmd.type === "conversation.list"
    || cmd.type === "session.restore"
    || cmd.type === "commands.list"
    || cmd.type === "interrupt"
  );
};

export const shouldReplayQueuedCommand = (
  queued: QueuedCommand,
  now: number = Date.now(),
): boolean => {
  const age = now - queued.queuedAt;
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

let singleton: WebSocketHandle | null = null;
const subscribers = new Set<(data: unknown) => void>();
const browserSessionId = `session_${
  typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 18)}`
}`;
const RECENT_INBOUND_EVENT_IDS_MAX = 512;
const PENDING_CLIENT_COMMAND_ACK_IDS_MAX = 512;

const recentInboundEventIds: string[] = [];
const recentInboundEventIdSet = new Set<string>();
const pendingClientCommandAckIdQueue: string[] = [];
const pendingClientCommandAcks = new Map<string, ReturnType<typeof setTimeout> | null>();

export const getWebSocket = (): WebSocketHandle | null => singleton;

export const resetRecentInboundEventIdsForTests = () => {
  recentInboundEventIds.length = 0;
  recentInboundEventIdSet.clear();
};

export const shouldProcessInboundEvent = (event: ServerEvent): boolean => {
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

const clearPendingClientCommandAck = (clientCommandId: string) => {
  const timeoutId = pendingClientCommandAcks.get(clientCommandId);
  if (timeoutId) clearTimeout(timeoutId);
  pendingClientCommandAcks.delete(clientCommandId);
};

export const trackPendingClientCommandAck = (cmd: ClientCommand) => {
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

    let hasConnectedOnce = false;

    const connect = () => {
      if (!alive) return;
      timer = null;
      const url = wsUrl("/ws", { session_id: browserSessionId });
      const ws = new WebSocket(url);
      ref.current = ws;

      const sendCommand = (cmd: ClientCommand) => {
        const command = commandWithClientCommandId(cmd);
        ws.send(JSON.stringify(command));
        trackPendingClientCommandAck(command);
      };

      ws.addEventListener("open", () => {
        if (!alive) return;
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

        if (hasConnectedOnce) {
          const state = useAppStore.getState();
          const restoreWorkspaceRoot = workspaceRootForConversationRestore(state);
          clearStreamingState(
            { textStreamBuffer, thinkingStreamBuffer },
            { conversationId: state.conversationId },
          );
          sendCommand({
            type: "session.restore",
            last_conversation_id: state.conversationId,
            ...(restoreWorkspaceRoot ? { last_workspace_root: restoreWorkspaceRoot } : {}),
          });
        } else {
          sendCommand({ type: "conversation.list" });
        }

        // Replay the offline queue AFTER session.restore so a queued
        // user_message / control_response reaches the backend only once its
        // conversation + workspace context has been restored.
        const now = Date.now();
        for (const queued of queue.current) {
          if (!shouldReplayQueuedCommand(queued, now)) continue;
          sendCommand(queued.cmd);
        }
        queue.current = [];

        sendCommand({ type: "commands.list" });
        hasConnectedOnce = true;

        const pingInterval = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            sendCommand({ type: "ping" });
          }
        }, 30000);
        ws.addEventListener("close", () => clearInterval(pingInterval), { once: true });
      });

      ws.addEventListener("close", () => {
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
        if (!alive) return;
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
        try {
          if (ws.readyState === WebSocket.OPEN) ws.close();
        } catch {
          /* noop */
        }
      });

      ws.addEventListener("message", (ev) => {
        if (!alive) return;
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
          const command = commandWithClientCommandId(cmd);
          ws.send(JSON.stringify(command));
          trackPendingClientCommandAck(command);
          return true;
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
      ref.current?.close();
      singleton = null;
      registerWebSocketSender(null);
    };
  }, []);
};

const textStreamBuffer = createStreamBuffer((buffered, conversationId) => {
  useAppStore.getState().appendTextChunk(buffered, conversationId);
});

const thinkingStreamBuffer = createStreamBuffer((buffered, conversationId) => {
  useAppStore.getState().appendThinkingChunk(buffered, conversationId);
});

const conversationIdFor = (e: ServerEvent): string | undefined => {
  const cid = (e as unknown as { conversation_id?: string }).conversation_id;
  if (typeof cid !== "string" || !cid) return undefined;
  return cid;
};

const handleServerEvent = (e: ServerEvent) => {
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
