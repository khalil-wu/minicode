import { useAppStore } from "../stores";
import type {
  ConversationCompactionUpdatedEvent,
  ConversationSummaryUpdatedEvent,
  ServerEvent,
  SystemNoticeEvent,
} from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { isControlPlaneNotice } from "./controlPlaneNotices";
import { addInspectorPayload } from "./inspectorEntries";

const noticeText = (event: SystemNoticeEvent): string => {
  const title = String(event.title || "").trim();
  const body = String(event.content || event.message || "").trim();
  if (title && body && title !== body) return `${title} — ${body}`;
  return body || title;
};

const conversationIdFor = (e: ServerEvent, fallback?: string): string | undefined => {
  const cid = (e as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof cid === "string" && cid ? cid : fallback;
};

const eventTimestampMs = (event: ServerEvent): number => {
  const parsed = Date.parse(String(event.timestamp || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const isReplayedEvent = (event: ServerEvent): boolean =>
  (event as ServerEvent & { __replayed?: boolean }).__replayed === true;

const stableTextHash = (value: string): string => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
};

const systemNoticeId = (
  event: ServerEvent,
  conversationId: string,
  content: string,
): string => {
  if (event.event_id) return `system-notice-${event.event_id}`;
  if (Number.isSafeInteger(event.seq)) return `system-notice-seq-${event.seq}`;
  if (event.timestamp) return `system-notice-time-${stableTextHash(`${event.timestamp}:${content}`)}`;
  if (isReplayedEvent(event)) {
    return `system-notice-replay-${stableTextHash(`${conversationId}:${content}`)}`;
  }
  return `system-notice-${Date.now().toString(36)}-${stableTextHash(content)}`;
};

export const handleNoticeEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "system_notice": {
      const ev = e as SystemNoticeEvent;
      const owner = conversationIdFor(e, conversationId);
      const content = noticeText(ev);
      if (content) {
        if (isControlPlaneNotice(content)) {
          if (
            owner
            && owner === useAppStore.getState().conversationId
            && !isReplayedEvent(e)
          ) {
            pushToast(content, "info", 3000);
          }
          return true;
        }
        if (!owner) return true;
        const messageId = systemNoticeId(e, owner, content);
        s.upsertSystemMessage(
          messageId,
          content,
          { conversationId: owner },
        );
        if (
          owner === useAppStore.getState().conversationId
          && (ev.data || ev.checkpoint_origin)
        ) {
          addInspectorPayload("message", messageId, {
            event: ev.type,
            conversation_id: owner,
            title: ev.title,
            message: ev.message,
            data: ev.data,
            checkpoint_origin: ev.checkpoint_origin,
            replayed: isReplayedEvent(e),
          });
        }
      }
      return true;
    }
    case "conversation.compaction.updated": {
      const ev = e as ConversationCompactionUpdatedEvent;
      const targetId = ev.conversation_id;
      useAppStore.setState((state) => ({
        conversations: state.conversations.map((conversation) =>
          conversation.id === targetId
            ? {
                ...conversation,
                compactionState: ev.state,
                compactionSummary: ev.summary,
              }
            : conversation,
        ),
      }));
      if (targetId !== useAppStore.getState().conversationId) return true;
      const currentUsage = useAppStore.getState().contextUsage;
      s.setContextUsage({
        used: currentUsage?.used ?? 0,
        limit: currentUsage?.limit ?? 0,
        compactedAt: eventTimestampMs(e),
        compactSummary: ev.summary,
      });
      return true;
    }
    case "conversation.summary.updated": {
      const ev = e as ConversationSummaryUpdatedEvent;
      useAppStore.setState((state) => ({
        conversations: state.conversations.map((conversation) =>
          conversation.id === ev.conversation_id
            ? {
                ...conversation,
                summary: ev.summary,
                title: ev.title,
                updatedAt: ev.updated_at,
                memoryMode: ev.memory_mode,
                memoryPolluted: ev.memory_polluted,
                memoryPollutionSources: [...ev.memory_pollution_sources],
              }
            : conversation,
        ),
      }));
      return true;
    }
    default:
      return false;
  }
};
