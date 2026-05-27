import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { wsUrl } from "../protocol/api";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { registerWebSocketSender } from "../protocol/ws-outbox";
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
const QUEUE_STALE_MS = 10_000;
const QUEUE_MAX_AGE_MS = 60_000;

interface QueuedCommand {
  cmd: ClientCommand;
  queuedAt: number;
}

const isTimeSensitiveCommand = (cmd: ClientCommand): boolean =>
  cmd.type === "approval" || cmd.type === "answer";

export interface WebSocketHandle {
  send: (cmd: ClientCommand) => boolean;
  close: () => void;
  sessionId: string;
  subscribe: (handler: (data: unknown) => void) => () => void;
}

let singleton: WebSocketHandle | null = null;
const subscribers = new Set<(data: unknown) => void>();
const browserSessionId = `session_${
  typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 18)}`
}`;

export const getWebSocket = (): WebSocketHandle | null => singleton;

export const useWebSocketConnection = () => {
  const ref = useRef<WebSocket | null>(null);
  const queue = useRef<QueuedCommand[]>([]);
  const reconnectAttempt = useRef(0);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;

    let hasConnectedOnce = false;

    const connect = () => {
      if (!alive) return;
      timer = null;
      const url = wsUrl("/ws", { session_id: browserSessionId });
      const ws = new WebSocket(url);
      ref.current = ws;

      ws.addEventListener("open", () => {
        if (!alive) return;
        reconnectAttempt.current = 0;
        useAppStore.getState().setConnected(true);
        const now = Date.now();
        for (const queued of queue.current) {
          if (isTimeSensitiveCommand(queued.cmd) && now - queued.queuedAt > QUEUE_STALE_MS) {
            continue;
          }
          if (now - queued.queuedAt > QUEUE_MAX_AGE_MS) {
            continue;
          }
          ws.send(JSON.stringify(queued.cmd));
        }
        queue.current = [];

        if (hasConnectedOnce) {
          const state = useAppStore.getState();
          clearStreamingState({ textStreamBuffer, thinkingStreamBuffer });
          ws.send(JSON.stringify({
            type: "session.restore",
            last_conversation_id: state.conversationId,
            last_workspace_root: state.workingDirectory,
          }));
        } else {
          ws.send(JSON.stringify({ type: "conversation.list" }));
        }
        ws.send(JSON.stringify({ type: "commands.list" }));
        hasConnectedOnce = true;

        const pingInterval = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
          }
        }, 30000);
        ws.addEventListener("close", () => clearInterval(pingInterval), { once: true });
      });

      ws.addEventListener("close", () => {
        useAppStore.getState().setConnected(false);
        if (!alive) return;
        const delay = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
        );
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
        let parsed: ServerEvent;
        try {
          parsed = JSON.parse(ev.data) as ServerEvent;
        } catch {
          return;
        }
        handleServerEvent(parsed);
        for (const sub of subscribers) sub(parsed);
      });
    };

    timer = window.setTimeout(connect, 0);

    singleton = {
      send: (cmd) => {
        const ws = ref.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(cmd));
          return true;
        }
        if (cmd.type === "user_message" || cmd.type === "conversation.list" || cmd.type === "session.restore" || cmd.type === "commands.list" || cmd.type === "interrupt") {
          queue.current.push({ cmd, queuedAt: Date.now() });
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
