import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";
import { wsProtocols, wsUrl } from "../protocol/api";
import {
  SERVER_EVENT_TYPES,
  isReplayedEvent,
  type ClientCommand,
  type ServerEvent,
  type ServerEventType,
} from "../protocol/events";
import { isUnknownServerEventType, normalizeInboundServerEvent } from "../protocol/server-event-validation";
import {
  commandWithClientCommandId,
  rejectAllPendingCommandResults,
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
import { handleControlPlaneProjectionEvent } from "../chat/controlPlaneEvents";
import { createStreamBuffer } from "../lib/stream-buffer";
import { clearStreamingState } from "../chat/streamingState";
import { safeJsonParse } from "../lib/safe-parse";
import { LS, readLS } from "../stores/shared-helpers";
import { hasInterruptFence } from "../lib/interrupt-command";

// Transport policy follows MiniCode's WebSocket transport: reconnects are
// bounded by a total budget, use bounded jitter, and heartbeat liveness is
// decided by the matching pong rather than arbitrary inbound traffic.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const RECONNECT_BUDGET_MS = 600_000;
// Session transport has a user-visible finite ladder. This is deliberately
// separate from provider-request and MCP retry budgets.
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_JITTER_FACTOR = 0.25;
const PING_INTERVAL_MS = 10_000;
const PONG_TIMEOUT_MS = 60_000;
const MAX_BUFFERED_CLIENT_COMMANDS = 1_000;
const PERMANENT_CLOSE_CODES = new Set([1002, 1008, 4001, 4003]);
// Browsers reject server-only close code 1012 from WebSocket.close(). Use a
// private client code so replay/protocol recovery can still reconnect normally.
export const CLIENT_RESYNC_CLOSE_CODE = 4002;

export const closeWebSocketForResync = (socket: WebSocket, reason: string): void => {
  try {
    socket.close(CLIENT_RESYNC_CLOSE_CODE, reason);
  } catch {
    // A closing/closed socket may reject a reasoned close; a plain close is
    // still preferable to surfacing an unhandled InvalidAccessError.
    try { socket.close(); } catch { /* noop */ }
  }
};

export interface QueuedCommand {
  cmd: ClientCommand;
  queuedAt: number;
}

export const isTimeSensitiveCommand = (cmd: ClientCommand): boolean =>
  cmd.type === "control_response"
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
  if (cmd.type === "interrupt") return hasInterruptFence(cmd);
  if (isTimeSensitiveCommand(cmd)) return true;
  return (
    cmd.type === "user_message"
    || cmd.type === "conversation.list"
    || cmd.type === "conversation.switch"
    || cmd.type === "session.restore"
    || cmd.type === "commands.list"
    || cmd.type === "skills.list"
  );
};

export const shouldDeferCommandUntilSessionRestore = (cmd: ClientCommand): boolean =>
  cmd.type === "interrupt" || isQueueableWhenOffline(cmd);

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
  conversations?: Array<{ id?: string; archived?: boolean; conversationType?: "main" | "side_chat" }>;
}): string => {
  const activeId = typeof state.conversationId === "string" ? state.conversationId.trim() : "";
  if (activeId) return activeId;
  const persistedId = (readLS(LS.conversation.activeId) || "").trim();
  if (!persistedId) return "";
  const conversations = state.conversations ?? [];
  if (conversations.length === 0) return persistedId;
  return conversations.some((item) => (
    item.id === persistedId
    && item.conversationType !== "side_chat"
    && !item.archived
  )) ? persistedId : "";
};

let singleton: WebSocketHandle | null = null;
const subscribers = new Set<(data: unknown) => void>();
const BROWSER_SESSION_STORAGE_KEY = "minicode.websocket.session_id";
type SessionStorageHost = { sessionStorage?: Pick<Storage, "getItem" | "setItem"> };

export const rendererSessionId = (renderer: SessionStorageHost): string => {
  let existing = "";
  try {
    existing = renderer.sessionStorage?.getItem(BROWSER_SESSION_STORAGE_KEY)?.trim() ?? "";
  } catch {
    // Storage can be unavailable in hardened or test renderers.
  }
  if (existing && /^session_[A-Za-z0-9_-]+$/.test(existing)) return existing;
  const created = `session_${
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "")
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 18)}`
  }`;
  try {
    renderer.sessionStorage?.setItem(BROWSER_SESSION_STORAGE_KEY, created);
  } catch {
    // The current document can still use the generated id for its lifetime.
  }
  return created;
};

const browserSessionId = rendererSessionId(
  (typeof window !== "undefined" ? window : globalThis) as SessionStorageHost,
);
const RECENT_INBOUND_EVENT_IDS_MAX = 1024;
const MAX_RECOVERY_BUFFERED_EVENTS = 1000;
const NON_REPLAYABLE_CURSOR_EVENT_TYPES = new Set<string>([
  "artifact_content",
  "conversation.list",
  "conversation.switched",
  "llm.model.updated",
  "mcp_status",
  "pong",
  "runtime.capabilities",
  "session.restored",
  "session.synced",
  "stream_event",
  "stream_resume",
]);

const recentInboundEventIds: string[] = [];
const recentInboundEventIdSet = new Set<string>();
const processedReplaySeqs = new Set<number>();
const failedReplaySeqs = new Set<number>();
const pendingClientCommandAckIdQueue: string[] = [];
const pendingClientCommands = new Map<string, ClientCommand>();
const latestProjectionCommandIds = new Map<string, string>();
let lastReceivedServerSeq = 0;

// Commands can project more than one event, and different command types can
// compete for the same user-visible state. Track the latest command by the
// state domain it owns instead of only by command type; otherwise a delayed
// restore could overwrite a newer manual switch, or an old switch's list could
// replace metadata from a newer switch.
const COMMAND_PROJECTION_DOMAINS: Readonly<
  Record<string, Readonly<Record<string, readonly string[]>>>
> = {
  "conversation.list": {
    "conversation.list": ["conversation-list"],
  },
  "conversation.switch": {
    "conversation.switched": ["active-conversation"],
    "conversation.list": ["conversation-list"],
  },
  "session.restore": {
    "session.restored": ["session-state", "active-conversation"],
    "conversation.switched": ["active-conversation"],
    "session.replay": ["session-recovery"],
    "stream_resume": ["session-recovery"],
  },
  "session.sync": {
    "session.synced": ["session-state", "active-conversation"],
    "session.replay": ["session-recovery"],
    "stream_resume": ["session-recovery"],
  },
};

const projectionDomainsForEvent = (commandType: string, eventType: string): readonly string[] =>
  COMMAND_PROJECTION_DOMAINS[commandType]?.[eventType] ?? [];

const projectionDomainsForCommand = (commandType: string): string[] => Array.from(new Set(
  Object.values(COMMAND_PROJECTION_DOMAINS[commandType] ?? {}).flat(),
));

const rememberSentCommandProjection = (command: ClientCommand, clientCommandId: string): void => {
  for (const domain of projectionDomainsForCommand(command.type)) {
    latestProjectionCommandIds.set(domain, clientCommandId);
  }
};

const isSupersededPendingProjectionCommand = (command: ClientCommand): boolean => {
  const clientCommandId = String(command.client_command_id || "").trim();
  if (!clientCommandId) return false;
  const domains = projectionDomainsForCommand(command.type);
  return domains.length > 0 && domains.some((domain) => {
    const latestCommandId = latestProjectionCommandIds.get(domain);
    return Boolean(latestCommandId && latestCommandId !== clientCommandId);
  });
};

export const getWebSocket = (): WebSocketHandle | null => singleton;

export const resetRecentInboundEventIdsForTests = () => {
  recentInboundEventIds.length = 0;
  recentInboundEventIdSet.clear();
  processedReplaySeqs.clear();
  failedReplaySeqs.clear();
  lastReceivedServerSeq = 0;
};

export const getLastReceivedServerSeqForTests = (): number => lastReceivedServerSeq;

export const shouldAdvanceReplayCursor = (event: ServerEvent): boolean => {
  const conversationId = (event as unknown as { conversation_id?: unknown }).conversation_id;
  const seq = Number((event as { seq?: unknown }).seq);
  return typeof conversationId === "string" && conversationId.trim().length > 0
    // A missing wire sequence cannot participate in durable replay continuity.
    // Process it as a legacy/transient event without moving the cursor.
    && Number.isSafeInteger(seq) && seq >= 0
    && !NON_REPLAYABLE_CURSOR_EVENT_TYPES.has(event.type)
    && !event.type.startsWith("session.");
};

export const shouldProcessInboundEvent = (event: ServerEvent): boolean => {
  const seq = Number((event as { seq?: unknown }).seq);
  if (
    shouldAdvanceReplayCursor(event)
    && Number.isSafeInteger(seq)
    && seq <= lastReceivedServerSeq
  ) {
    return false;
  }
  if (Number.isFinite(seq) && failedReplaySeqs.size > 0 && seq > Math.min(...failedReplaySeqs)) {
    return false;
  }
  const eventId = event.event_id;
  if (typeof eventId !== "string" || !eventId) return true;
  return !recentInboundEventIdSet.has(eventId);
};

export const assertInboundReplayCursorContinuity = (event: ServerEvent): void => {
  if (!shouldAdvanceReplayCursor(event)) return;
  const seq = Number(event.seq);
  if (!Number.isSafeInteger(seq) || seq <= lastReceivedServerSeq) {
    throw new Error("Durable event sequence does not advance the active cursor.");
  }
  const replayed = isReplayedEvent(event);
  const hasDurableLink = Object.prototype.hasOwnProperty.call(event, "previous_replay_seq");
  if (!replayed && !hasDurableLink) return;
  const previousReplaySeq = Number(event.previous_replay_seq);
  if (!Number.isSafeInteger(previousReplaySeq) || previousReplaySeq !== lastReceivedServerSeq) {
    throw new Error("Durable event does not continue the active replay chain.");
  }
};

const reportedUnknownServerEventTypes = new Set<string>();

/** Drop one event this client build cannot represent, and say so.
 *
 * The durable cursor has to move past it: leaving the cursor behind makes the
 * backend replay the same undeliverable event after every reconnect, which is
 * a loop no resync can break. Returns false when an earlier unprocessed hole
 * means the cursor must not advance yet. */
export const skipUndeliverableInboundEvent = (rawEvent: unknown, seq: number): boolean => {
  const type = rawEvent && typeof rawEvent === "object"
    ? String((rawEvent as { type?: unknown }).type ?? "")
    : "";
  if (type && !reportedUnknownServerEventTypes.has(type)) {
    reportedUnknownServerEventTypes.add(type);
    console.error("[ws] Server sent an event this client cannot represent:", type);
    pushToast(`收到无法处理的服务端事件 “${type}”，已忽略。前后端协议版本可能不一致。`, "error", 8000);
  }
  if (!Number.isSafeInteger(seq) || seq <= lastReceivedServerSeq) return true;
  const firstHole = failedReplaySeqs.size > 0 ? Math.min(...failedReplaySeqs) : Infinity;
  if (seq >= firstHole) return false;
  lastReceivedServerSeq = seq;
  return true;
};

export const commitProcessedInboundEvent = (event: ServerEvent): void => {
  const seq = Number((event as { seq?: unknown }).seq);
  if (event.type === "session.restored" || event.type === "session.synced") {
    const snapshot = event as ServerEvent & {
      current_seq?: unknown;
      cursor_reset?: unknown;
      last_seq?: unknown;
      replayed_events?: unknown;
      requested_last_seq?: unknown;
    };
    const currentSeq = Number(snapshot.current_seq);
    const snapshotLastSeq = Number(snapshot.last_seq);
    const replayedEvents = Number(snapshot.replayed_events ?? 0);
    if (
      Number.isSafeInteger(currentSeq)
      && currentSeq >= 0
      && Number.isSafeInteger(snapshotLastSeq)
      && snapshotLastSeq >= 0
      && Number.isSafeInteger(replayedEvents)
      && replayedEvents === 0
    ) {
      if (snapshot.cursor_reset === true) {
        const requestedLastSeq = Number(snapshot.requested_last_seq);
        if (
          !Number.isSafeInteger(requestedLastSeq)
          || requestedLastSeq <= currentSeq
          || snapshotLastSeq !== currentSeq
        ) {
          throw new Error("Session snapshot contains an invalid replay cursor reset.");
        }
        lastReceivedServerSeq = currentSeq;
        processedReplaySeqs.clear();
        failedReplaySeqs.clear();
      } else {
        if (snapshotLastSeq !== lastReceivedServerSeq || currentSeq < snapshotLastSeq) {
          throw new Error("Session snapshot does not continue the requested replay cursor.");
        }
        lastReceivedServerSeq = currentSeq;
        for (const processed of Array.from(processedReplaySeqs)) {
          if (processed <= currentSeq) processedReplaySeqs.delete(processed);
        }
        for (const failed of Array.from(failedReplaySeqs)) {
          if (failed <= currentSeq) failedReplaySeqs.delete(failed);
        }
      }
    }
  } else if (shouldAdvanceReplayCursor(event) && Number.isSafeInteger(seq)) {
    assertInboundReplayCursorContinuity(event);
    failedReplaySeqs.delete(seq);
    if (isReplayedEvent(event)) {
      const previousReplaySeq = Number(event.previous_replay_seq);
      if (
        !Number.isSafeInteger(previousReplaySeq)
        || previousReplaySeq !== lastReceivedServerSeq
        || seq <= previousReplaySeq
      ) {
        throw new Error("Replayed event does not continue the durable cursor chain.");
      }
      lastReceivedServerSeq = seq;
      processedReplaySeqs.delete(seq);
    } else {
      // Live wire seq can jump over transient envelopes. WebSocket delivery is
      // ordered, so a successfully applied durable live event becomes the new
      // cursor unless an earlier handler failure has frozen the stream.
      const firstHole = failedReplaySeqs.size > 0 ? Math.min(...failedReplaySeqs) : Infinity;
      if (seq >= firstHole) {
        throw new Error("Live durable event overtook an unprocessed replay event.");
      }
      lastReceivedServerSeq = Math.max(lastReceivedServerSeq, seq);
    }
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

export const markInboundEventFailed = (event: ServerEvent): void => {
  const seq = Number((event as { seq?: unknown }).seq);
  if (shouldAdvanceReplayCursor(event) && Number.isFinite(seq)) {
    failedReplaySeqs.add(seq);
  }
};

export const eventsFromSessionReplay = (event: ServerEvent): ServerEvent[] => {
  if (event.type !== "session.replay") return [];
  const envelope = event as ServerEvent & {
    current_seq?: unknown;
    events?: unknown;
    last_seq?: unknown;
    replayed_events?: unknown;
  };
  const lastSeq = Number(envelope.last_seq);
  const currentSeq = Number(envelope.current_seq);
  const replayedEvents = Number(envelope.replayed_events);
  const events = Array.isArray(envelope.events) ? envelope.events : [];
  if (
    !Number.isSafeInteger(lastSeq)
    || lastSeq < 0
    || !Number.isSafeInteger(currentSeq)
    || currentSeq < lastSeq
    || !Number.isSafeInteger(replayedEvents)
    || replayedEvents !== events.length
    || lastSeq !== lastReceivedServerSeq
  ) {
    throw new Error("Session replay envelope does not match the active durable cursor.");
  }
  let expectedPreviousSeq = lastSeq;
  const replayed: ServerEvent[] = [];
  for (const rawEvent of events) {
    const candidate = (rawEvent && typeof rawEvent === "object" ? rawEvent : {}) as Record<string, unknown>;
    const replayedSeq = Number(candidate.seq);
    const previousReplaySeq = Number(candidate.previous_replay_seq);
    if (
      !Number.isSafeInteger(replayedSeq)
      || replayedSeq <= expectedPreviousSeq
      || !Number.isSafeInteger(previousReplaySeq)
      || previousReplaySeq !== expectedPreviousSeq
    ) {
      throw new Error("Session replay contains a discontinuous durable chain.");
    }
    expectedPreviousSeq = replayedSeq;
    const normalized = normalizeInboundServerEvent(rawEvent);
    if (!normalized || normalized.type === "session.replay") {
      // One event this build cannot represent must not strand the batch.
      // Rejecting the whole envelope left the durable cursor behind, so the
      // backend replayed the same undeliverable event after every reconnect —
      // the connect/disconnect loop no resync could break. Carry it through as
      // undeliverable so the caller advances the cursor past it in order.
      replayed.push({
        type: String(candidate.type ?? ""),
        seq: replayedSeq,
        replayed: true,
        __undeliverable: true,
      } as unknown as ServerEvent);
      continue;
    }
    replayed.push({ ...normalized, replayed: true } as unknown as ServerEvent);
  }
  if (
    (events.length > 0 && expectedPreviousSeq !== currentSeq)
    || (events.length === 0 && lastSeq !== currentSeq)
  ) {
    throw new Error("Session replay does not reach its advertised current cursor.");
  }
  return replayed;
};

/** True when a replayed event was carried through only to move the cursor. */
export const isUndeliverableReplayEvent = (event: ServerEvent): boolean =>
  (event as ServerEvent & { __undeliverable?: unknown }).__undeliverable === true;

export const resetPendingClientCommandAcksForTests = () => {
  pendingClientCommandAckIdQueue.length = 0;
  pendingClientCommands.clear();
  latestProjectionCommandIds.clear();
};

export const isSupersededCommandProjection = (event: ServerEvent): boolean => {
  const commandId = String(event.client_command_id || "").trim();
  const commandType = String(event.client_command_type || "").trim();
  if (!commandId || !commandType) return false;
  const domains = projectionDomainsForEvent(commandType, event.type);
  if (domains.length === 0) return false;
  return domains.some((domain) => {
    const latestCommandId = latestProjectionCommandIds.get(domain);
    return Boolean(latestCommandId && latestCommandId !== commandId);
  });
};

export const getPendingClientCommandAckIdsForTests = (): string[] =>
  pendingClientCommandAckIdQueue.filter((id) => pendingClientCommands.has(id));

const pendingClientCommandPayloads = (): ClientCommand[] =>
  pendingClientCommandAckIdQueue
    .map((id) => pendingClientCommands.get(id))
    .filter((cmd): cmd is ClientCommand => Boolean(cmd))
    // A newer projection command already expresses the user's current intent.
    // Replaying an older unacknowledged switch/restore after reconnect would
    // make stale state authoritative again even though ingress rejects its old
    // response. Keep it pending for a late ACK, but never transmit it again.
    .filter((cmd) => !isSupersededPendingProjectionCommand(cmd));

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
    useAppStore.getState().setConnectionState("connecting", {
      attempt: 0,
      maxAttempts: null,
      error: null,
    });
    let alive = true;
    let timer: number | null = null;
    let heartbeatCleanup = () => {};
    const coalescedCommands = new Map<string, ClientCommand>();
    const coalescedMicrotasks = new Set<string>();
    let awaitingSessionRestore = false;
    let recoverySnapshotPending = false;
    let recoveryReplayPending = false;
    let recoverySwitchPending = false;
    let recoverySwitchWillRefreshCatalog = false;
    let bufferedRecoveryEvents: ServerEvent[] = [];
    let reconnectStartedAt: number | null = null;
    // A client-initiated resync that keeps failing must stay inside one
    // reconnect budget. Resetting the budget on every ``open`` turned a
    // repeating resync into an unbounded 1s loop that reported itself as a
    // successful reconnect.
    let resyncCloseSeen = false;
    let reconnectExhausted = false;
    let hasConnectedOnce = false;

    const clearUndeliverableCommands = (reason: string) => {
      rejectAllPendingCommandResults(reason);
      pendingClientCommandAckIdQueue.length = 0;
      pendingClientCommands.clear();
      queue.current = [];
      coalescedMicrotasks.clear();
      coalescedCommands.clear();
      awaitingSessionRestore = false;
      recoverySnapshotPending = false;
      recoveryReplayPending = false;
      recoverySwitchPending = false;
      recoverySwitchWillRefreshCatalog = false;
      bufferedRecoveryEvents = [];
    };

    const stopReconnecting = (reason: string, message: string) => {
      reconnectExhausted = true;
      useAppStore.getState().setConnectionState("failed", {
        attempt: reconnectAttempt.current,
        maxAttempts: MAX_RECONNECT_ATTEMPTS,
        error: message,
      });
      clearUndeliverableCommands(reason);
      // No further transport means no further terminal event. `session.restore`
      // is what normally repairs streaming flags after a reconnect, and it can
      // no longer arrive, so spinners would spin forever. Seal them here — the
      // one place that knows reconnection is over. The turn genuinely failed
      // from this client's point of view, so it is recorded as such instead of
      // being left to render as a finished turn.
      clearStreamingState({ textStreamBuffer, thinkingStreamBuffer }, {
        clearAllConversations: true,
        terminalStatus: "failed",
        failureMessage: message,
        failureRecoverable: false,
      });
      pushToast(message, "error", 0);
    };

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
      if (clientCommandId) {
        rememberSentCommandProjection(command, clientCommandId);
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

    const discardBufferedCommandType = (type: ClientCommand["type"]): void => {
      for (const [clientCommandId, command] of Array.from(pendingClientCommands.entries())) {
        if (command.type === type) clearPendingClientCommandAck(clientCommandId);
      }
      queue.current = queue.current.filter((queued) => queued.cmd.type !== type);
      const key = coalescingKeyForClientCommand({ type } as ClientCommand);
      if (key) coalescedCommands.delete(key);
    };

    const connect = () => {
      if (!alive || reconnectExhausted) return;
      timer = null;
      const url = wsUrl("/ws", {
        session_id: browserSessionId,
        protocol: "control_v1",
      });
      const protocols = wsProtocols();
      const ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
      ref.current = ws;
      let pingInterval: number | null = null;
      let pongTimeout: number | null = null;
      let awaitingPong = false;
      let connectionRecoveryCompleted = false;
      let announceReconnectAfterRecovery = false;

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

      const completeConnectionRecovery = (): void => {
        if (connectionRecoveryCompleted || !alive || ref.current !== ws) return;
        connectionRecoveryCompleted = true;
        reconnectAttempt.current = 0;
        reconnectStartedAt = null;
        resyncCloseSeen = false;
        hasConnectedOnce = true;
        useAppStore.getState().setConnectionState("connected", {
          attempt: 0,
          maxAttempts: null,
          error: null,
        });
        if (announceReconnectAfterRecovery) {
          pushToast("已重新连接", "success", 2000);
        }
      };

      ws.addEventListener("open", () => {
        if (!alive || ref.current !== ws) return;
        const wasReconnect = hasConnectedOnce || reconnectAttempt.current > 0;
        announceReconnectAfterRecovery = wasReconnect && !resyncCloseSeen;

        const state = useAppStore.getState();
        const restoreConversationId = conversationIdForSessionRestore(state);
        const restoreWorkspaceRoot = restoreConversationId
          ? workspaceRootForConversationRestore({ ...state, conversationId: restoreConversationId })
          : "";
        awaitingSessionRestore = Boolean(hasConnectedOnce || restoreConversationId);
        recoverySnapshotPending = awaitingSessionRestore;
        recoveryReplayPending = false;
        recoverySwitchPending = false;
        recoverySwitchWillRefreshCatalog = false;
        bufferedRecoveryEvents = [];
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
        // A list snapshot can be generated before restore commits the canonical
        // active owner. Serialize it behind restore so a late pre-restore list
        // cannot remove restored state or activate an obsolete conversation.
        if (!awaitingSessionRestore) {
          sendOrCoalesceCommand({ type: "conversation.list" });
        }

        if (!awaitingSessionRestore) {
          replayPendingClientCommands();
          flushOfflineQueue();
        }

        sendOrCoalesceCommand({ type: "commands.list" });
        // Composer mounts before this connection effect registers its sender,
        // so its eager catalog request can legitimately be dropped. Refresh
        // both catalogs from the transport lifecycle, which is also what keeps
        // them current after a reconnect.
        sendOrCoalesceCommand({ type: "skills.list" });
        if (!awaitingSessionRestore) completeConnectionRecovery();

        pingInterval = window.setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          if (awaitingPong) return;
          if (!sendCommandNow({ type: "ping" })) return;
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
        recoverySnapshotPending = false;
        recoveryReplayPending = false;
        recoverySwitchPending = false;
        recoverySwitchWillRefreshCatalog = false;
        bufferedRecoveryEvents = [];
        const closeCode = Number((event as CloseEvent).code || 0);
        if (closeCode === CLIENT_RESYNC_CLOSE_CODE) resyncCloseSeen = true;
        if (PERMANENT_CLOSE_CODES.has(closeCode)) {
          const terminal = closeCode === 1008
            ? {
                reason: "连接被服务拒绝，操作未完成",
                message: "连接被服务拒绝，请检查登录、来源或 Provider 配置后重试。",
              }
            : closeCode === 4001
            ? {
                reason: "连接认证已失效，操作未完成",
                message: "连接认证已失效，请重新登录或刷新应用。",
              }
            : closeCode === 4003
              ? {
                  reason: "连接授权被拒绝，操作未完成",
                  message: "当前连接没有访问权限，请检查授权后重试。",
                }
              : {
                  reason: "连接协议错误，操作未完成",
                  message: "连接协议错误，无法继续。请刷新应用后重试。",
                };
          stopReconnecting(terminal.reason, terminal.message);
          return;
        }
        const now = Date.now();
        if (reconnectAttempt.current >= MAX_RECONNECT_ATTEMPTS) {
          stopReconnecting(
            "无法重新连接，操作未完成",
            `无法重新连接到 MiniCode（已重试 ${reconnectAttempt.current}/${MAX_RECONNECT_ATTEMPTS} 次）。请检查服务状态或网络后刷新重试。`,
          );
          return;
        }
        if (reconnectStartedAt === null) reconnectStartedAt = now;
        const remainingBudget = RECONNECT_BUDGET_MS - (now - reconnectStartedAt);
        if (remainingBudget <= 0) {
          stopReconnecting(
            "无法重新连接，操作未完成",
            "无法重新连接到 MiniCode。请检查服务状态或网络后刷新重试。",
          );
          return;
        }
        const base = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
        );
        const jitter = base * ((Math.random() * 2 - 1) * RECONNECT_JITTER_FACTOR);
        const delay = Math.min(remainingBudget, Math.max(0, base + jitter));
        reconnectAttempt.current += 1;
        useAppStore.getState().setConnectionState("reconnecting", {
          attempt: reconnectAttempt.current,
          maxAttempts: MAX_RECONNECT_ATTEMPTS,
          error: null,
        });
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

      const deliverInboundEvent = (parsed: ServerEvent): boolean => {
        if (!shouldProcessInboundEvent(parsed)) return true;
        acknowledgeClientCommand(parsed);
        const handled = processInboundEvent(parsed);
        if (
          !handled
          && (
            parsed.type === "session.replay"
            || shouldAdvanceReplayCursor(parsed)
            || (
              awaitingSessionRestore
              && ["conversation.switched", "session.restored", "session.synced"].includes(parsed.type)
            )
          )
        ) {
          closeWebSocketForResync(ws, "event replay required");
          return false;
        }
        if (handled) {
          for (const sub of subscribers) sub(parsed);
        }
        return handled;
      };

      const refreshAfterSessionProjection = (switchWillRefreshCatalog: boolean): void => {
        // These semantic events prove restore/sync has completed even if its
        // transport ACK was delayed. Replaying the old restore can start a
        // loop, while replaying a pre-restore command catalog request can
        // project the previous conversation's executable scope.
        discardBufferedCommandType("session.restore");
        discardBufferedCommandType("conversation.list");
        if (!switchWillRefreshCatalog) {
          discardBufferedCommandType("commands.list");
        }
        replayPendingClientCommands();
        flushOfflineQueue();
        sendOrCoalesceCommand({ type: "conversation.list" });
        if (!switchWillRefreshCatalog) {
          sendOrCoalesceCommand({ type: "commands.list" });
        }
      };

      const finishSessionRecovery = (): void => {
        if (
          !awaitingSessionRestore
          || recoverySnapshotPending
          || recoveryReplayPending
          || recoverySwitchPending
        ) return;

        const buffered = bufferedRecoveryEvents;
        bufferedRecoveryEvents = [];
        for (const bufferedEvent of buffered) {
          if (isSupersededCommandProjection(bufferedEvent)) continue;
          if (!deliverInboundEvent(bufferedEvent)) return;
        }

        awaitingSessionRestore = false;
        refreshAfterSessionProjection(recoverySwitchWillRefreshCatalog);
        recoverySwitchWillRefreshCatalog = false;
        completeConnectionRecovery();
      };

      ws.addEventListener("message", (ev) => {
        if (!alive || ref.current !== ws) return;
        let parsed: ServerEvent | null;
        try {
          const rawEvent = safeJsonParse<unknown>(ev.data, null);
          parsed = normalizeInboundServerEvent(rawEvent);
          const rawSeq = rawEvent && typeof rawEvent === "object"
            ? (rawEvent as { seq?: unknown }).seq
            : undefined;
          if (!parsed && Number.isFinite(Number(rawSeq))) {
            // A type this build does not declare is protocol drift, not stream
            // desync. Resyncing replays it from the durable log forever, so
            // drop it, move the cursor, and make the gap visible instead.
            if (isUnknownServerEventType(rawEvent)) {
              if (skipUndeliverableInboundEvent(rawEvent, Number(rawSeq))) return;
            }
            closeWebSocketForResync(ws, "protocol resync required");
            return;
          }
        } catch {
          return;
        }
        if (!parsed) return;
        if (isSupersededCommandProjection(parsed)) return;
        if (parsed.type === "pong") {
          awaitingPong = false;
          if (pongTimeout !== null) {
            window.clearTimeout(pongTimeout);
            pongTimeout = null;
          }
        }
        if (awaitingSessionRestore && shouldAdvanceReplayCursor(parsed)) {
          if (bufferedRecoveryEvents.length >= MAX_RECOVERY_BUFFERED_EVENTS) {
              closeWebSocketForResync(ws, "session recovery buffer exceeded");
            return;
          }
          bufferedRecoveryEvents.push(parsed);
          return;
        }
        const handled = deliverInboundEvent(parsed);
        if (!handled) return;
        if (parsed.type === "session.restored" || parsed.type === "session.synced") {
          // Remove pre-snapshot catalog work before a following
          // conversation.switched handler queues its owner-correct refresh.
          discardBufferedCommandType("session.restore");
          discardBufferedCommandType("conversation.list");
          discardBufferedCommandType("commands.list");
          recoverySnapshotPending = false;
          recoveryReplayPending = Number(parsed.replayed_events || 0) > 0;
          recoverySwitchPending = parsed.type === "session.restored"
            && parsed.conversation_switched_follows === true;
          recoverySwitchWillRefreshCatalog = recoverySwitchPending;
          if (recoveryReplayPending || recoverySwitchPending) awaitingSessionRestore = true;
          if (awaitingSessionRestore) {
            finishSessionRecovery();
          } else {
            refreshAfterSessionProjection(false);
          }
          return;
        }
        if (awaitingSessionRestore && parsed.type === "conversation.switched") {
          recoverySwitchPending = false;
          finishSessionRecovery();
          return;
        }
        if (awaitingSessionRestore && parsed.type === "session.replay") {
          if (!recoveryReplayPending) {
            closeWebSocketForResync(ws, "unexpected session replay");
            return;
          }
          recoveryReplayPending = false;
          finishSessionRecovery();
        }
      });
    };

    timer = window.setTimeout(connect, 0);

    singleton = {
      send: (cmd) => {
        const ws = ref.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          // The backend has not restored the active conversation, pending
          // approval futures, or stream ownership yet. Defer every durable
          // session-scoped action, not only user messages; otherwise an
          // approval/answer sent in this window can be applied to an empty or
          // stale session and leave the real tool call blocked forever.
          if (awaitingSessionRestore && shouldDeferCommandUntilSessionRestore(cmd)) {
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
      clearUndeliverableCommands("连接已关闭，操作未完成");
      const ownedSocket = ref.current;
      ref.current = null;
      ownedSocket?.close();
      singleton = null;
      registerWebSocketSender(null);
    };
  }, []);
};

const textStreamBuffer = createStreamBuffer((buffered, conversationId, itemId, metadata, messageId) => {
  useAppStore.getState().appendAgentMessageDelta(
    itemId || "agent-message",
    buffered,
    conversationId,
    messageId,
    typeof metadata?.source === "string" ? metadata.source : undefined,
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

// ── Explicit event dispatch ──────────────────────────────────────────────
// Every server event type is owned by exactly one reducer. Dispatch is a
// lookup, never a first-wins chain: adding a new event type requires
// declaring its owner here, so two reducers cannot silently compete for the
// same event. The module-load assertion below fails loudly on duplicates.
type ServerEventDispatcher = (e: ServerEvent, cid: string | undefined) => boolean;

const CHAT_STREAM_EVENT_TYPES = new Set<string>([
  "thinking_delta", "thinking", "item.started", "agent_message.delta",
  "item.completed", "image_chunk", "tool_call", "tool_output_delta",
  "command_output_chunk", "tool_result", "permission.decision", "agent.item",
  "done", "error", "stream_resume",
]);
const RUNTIME_EVENT_TYPES = new Set<string>([
  "agent.progress", "runtime.span", "agent.run.started", "agent.run.completed",
  "context_usage", "context_compacted", "context_ledger", "context_forked",
  "context_side_query_result", "turn.plan.updated", "task.update",
  "subagent.start", "subagent.event", "subagent.progress", "subagent.done",
  "budget_update", "budget.warning", "subagent.mailbox",
  "parent.notifications",
  "subagent.plan_approval_requested", "permission.mode.updated",
  "runtime.capabilities", "mcp.lifecycle", "mcp.progress",
  // Handled inside runtimeEvents' default branch:
  "stream_event", "session.state_changed", "rate_limit",
]);
const CONTROL_EVENT_TYPES = new Set<string>([
  "control_request", "approval.file_diff", "approval.cancelled",
]);
const SESSION_EVENT_TYPES = new Set<string>([
  "user_message.queue.updated", "llm.model.updated", "llm.provider.oauth.auth",
  "llm.provider.oauth.device_code", "llm.provider.oauth.info",
  "llm.provider.oauth.progress", "session.restored", "session.synced",
  "conversation.list", "goal.updated", "conversation.switched",
]);
const ARTIFACT_EVENT_TYPES = new Set<string>([
  "artifact_content", "artifact.preview", "citation.add", "inspector.update",
]);
const COMMAND_CATALOG_EVENT_TYPES = new Set<string>([
  "skills.list", "commands.list", "skills.marketplace.list",
]);
const PERIPHERAL_EVENT_TYPES = new Set<string>([
  "terminal.output", "terminal.resized", "workspace.imported", "file.changed",
  "terminal.created", "terminal.killed", "terminal.exit", "terminal.list",
  "terminal.snapshot", "mcp_status", "env.list", "git.pr_status",
  "scheduler.list", "background.started", "background.stalled",
  "background.completed",
]);
const COMMAND_RESULT_EVENT_TYPES = new Set<string>(["command.result"]);
const PREVIEW_EVENT_TYPES = new Set<string>([
  "preview.servers.updated", "preview.server.detected", "preview.server.stopped",
  "preview.navigated", "preview.refreshed", "preview.launch.config",
  "preview.launch.started", "preview.server.ready", "preview.server.output",
  "preview.server.crashed", "preview.server.unhealthy", "preview.launch.stopped",
  "preview.verified",
]);
const CONTROL_PLANE_EVENT_TYPES = new Set<string>([
  "conversation.hydration.updated", "permission.rules.updated",
  "checkpoint.created", "checkpoint.list", "checkpoint.rewound",
  "checkpoint.run.list", "checkpoint.run.resume", "workspace.recent.list",
  "guidelines.updated",
]);
const NOTICE_EVENT_TYPES = new Set<string>([
  "system_notice", "conversation.compaction.updated",
  "conversation.summary.updated",
]);
const DIFF_EVENT_TYPES = new Set<string>([
  "turn.diff.updated", "diff.git_working_tree", "diff.git_staged",
  "diff.git_stage_file", "diff.git_unstage_file", "diff.git_stage_all",
  "diff.git_unstage_all", "diff.git_revert_file",
]);

const EVENT_DISPATCHERS: ReadonlyArray<{
  types: ReadonlySet<string>;
  dispatch: ServerEventDispatcher;
}> = [
  { types: CHAT_STREAM_EVENT_TYPES, dispatch: (e, cid) => handleChatStreamEvent(e, cid, { textStreamBuffer, thinkingStreamBuffer }) },
  { types: RUNTIME_EVENT_TYPES, dispatch: (e, cid) => handleRuntimeEvent(e, cid) },
  { types: CONTROL_EVENT_TYPES, dispatch: (e) => handleControlEvent(e) },
  { types: SESSION_EVENT_TYPES, dispatch: (e, _cid) => handleSessionEvent(e, { textStreamBuffer, thinkingStreamBuffer }) },
  { types: ARTIFACT_EVENT_TYPES, dispatch: (e, cid) => handleArtifactEvent(e, cid) },
  { types: COMMAND_CATALOG_EVENT_TYPES, dispatch: (e) => handleCommandCatalogEvent(e) },
  { types: PERIPHERAL_EVENT_TYPES, dispatch: (e) => handlePeripheralEvent(e) },
  { types: COMMAND_RESULT_EVENT_TYPES, dispatch: (e) => handleCommandResultEvent(e) },
  { types: PREVIEW_EVENT_TYPES, dispatch: (e) => handlePreviewEvent(e) },
  { types: CONTROL_PLANE_EVENT_TYPES, dispatch: (e) => handleControlPlaneProjectionEvent(e) },
  { types: NOTICE_EVENT_TYPES, dispatch: (e, cid) => handleNoticeEvent(e, cid) },
  { types: DIFF_EVENT_TYPES, dispatch: (e) => handleDiffEvent(e) },
];

// Transport and replay envelopes are owned by the websocket layer itself.
const TRANSPORT_EVENT_TYPES = new Set<string>([
  "pong",
  "client.command.ack",
  "session.replay",
]);

// Assert disjoint ownership once at module load so a duplicate registration
// fails loudly instead of silently ordering one reducer ahead of another.
{
  const seen = new Set<string>();
  for (const group of EVENT_DISPATCHERS) {
    for (const type of group.types) {
      if (seen.has(type)) {
        throw new Error(`[useWebSocket] Event type "${type}" is registered by multiple reducers.`);
      }
      seen.add(type);
    }
  }
  for (const type of TRANSPORT_EVENT_TYPES) seen.add(type);
  const missing = [...SERVER_EVENT_TYPES].filter((type) => !seen.has(type));
  const unknown = [...seen].filter(
    (type) => !SERVER_EVENT_TYPES.has(type as ServerEventType),
  );
  if (missing.length || unknown.length) {
    throw new Error(
      `[useWebSocket] Event ownership mismatch. Missing: ${missing.join(", ") || "none"}; `
      + `unknown: ${unknown.join(", ") || "none"}.`,
    );
  }
}
const EVENT_OWNER = new Map<string, ServerEventDispatcher>();
for (const group of EVENT_DISPATCHERS) {
  for (const type of group.types) {
    EVENT_OWNER.set(type, group.dispatch);
  }
}

// Transport-layer events are acknowledged by the websocket layer itself
// (heartbeat pong, client.command.ack in acknowledgeClientCommand). They are
// still reported to subscribers as handled events, but never enter a business
// reducer.
const handleServerEvent = (e: ServerEvent): boolean => {
  if (e.type === "session.replay") {
    for (const replayed of eventsFromSessionReplay(e)) {
      if (isUndeliverableReplayEvent(replayed)) {
        if (!skipUndeliverableInboundEvent(replayed, Number(replayed.seq))) return false;
        continue;
      }
      if (!shouldProcessInboundEvent(replayed)) continue;
      if (!processInboundEvent(replayed)) return false;
      for (const sub of subscribers) sub(replayed);
    }
    return true;
  }

  const cid = conversationIdFor(e);
  const dispatch = EVENT_OWNER.get(e.type);
  if (dispatch) return dispatch(e, cid);
  if (TRANSPORT_EVENT_TYPES.has(e.type)) return true;
  // Unknown event types are deliberately unhandled by the renderer. Keeping
  // them outside any default makes new backend events visible to protocol
  // validation instead of being swallowed by a catch-all reducer.
  return false;
};

// Advance the replay cursor only after the event has been applied.  This is
// the same durable-event rule used by the backend: a handler failure must be
// replayable after reconnect instead of being acknowledged by the cursor.
const processInboundEvent = (event: ServerEvent): boolean => {
  try {
    assertInboundReplayCursorContinuity(event);
    const handled = handleServerEvent(event);
    if (!handled) {
      console.warn("[ws] Known server event was not routed; leaving it replayable", event);
      return false;
    }
    commitProcessedInboundEvent(event);
    return handled;
  } catch (error) {
    markInboundEventFailed(event);
    // Do not stringify the complete event here: artifact_content may contain
    // multi-megabyte base64 data and Chromium's console bridge turns nested
    // objects into the unhelpful "[object Object]". Keep the durable event
    // replayable while emitting enough structured context to identify the
    // failing projection without leaking the payload itself.
    const normalizedError = error instanceof Error
      ? {
          name: error.name,
          message: error.message,
          stack: error.stack,
        }
      : String(error);
    console.error("[ws] Failed to apply server event; leaving it replayable", {
      type: event.type,
      seq: event.seq,
      eventId: event.event_id,
      conversationId: conversationIdFor(event),
      requestId: (event as ServerEvent & { request_id?: unknown }).request_id,
      error: normalizedError,
    });
    return false;
  }
};
