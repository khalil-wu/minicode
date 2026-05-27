import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";

const noticeText = (e: ServerEvent): string => {
  const ev = e as unknown as { content?: unknown; message?: unknown; summary?: unknown };
  return String(ev.content ?? ev.message ?? ev.summary ?? "").trim();
};

const conversationIdFor = (e: ServerEvent, fallback?: string): string | undefined => {
  const cid = (e as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof cid === "string" && cid ? cid : fallback;
};

export const handleNoticeEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "system_notice": {
      const content = noticeText(e);
      if (content) {
        s.upsertSystemMessage(
          `system-notice-${Date.now().toString(36)}`,
          content,
          { conversationId: conversationIdFor(e, conversationId) },
        );
        pushToast(content, "info", 3000);
      }
      return true;
    }
    case "guidelines.updated": {
      const content = noticeText(e) || "Project guidelines have been updated.";
      s.upsertSystemMessage(
        "system-guidelines-updated",
        content,
        { conversationId: conversationIdFor(e, conversationId), replacePrefix: "Project guidelines" },
      );
      pushToast(content, "info", 3000);
      return true;
    }
    case "conversation.compaction.updated": {
      const ev = e as unknown as { summary?: string };
      const currentUsage = useAppStore.getState().contextUsage;
      s.setContextUsage({
        used: currentUsage?.used ?? 0,
        limit: currentUsage?.limit ?? 0,
        compactedAt: Date.now(),
        compactSummary: ev.summary,
      });
      return true;
    }
    case "conversation.summary.updated":
    case "conversation.hydration.updated":
    case "permission.rules.updated":
    case "checkpoint.list":
    case "checkpoint.rewound":
    case "workspace.recent.list":
    case "command_output_chunk":
    case "pong":
      return true;
    default:
      return false;
  }
};
