import { useAppStore } from "../stores";
import type {
  ArtifactPreviewEvent,
  CitationAddEvent,
  InspectorUpdateEvent,
  ServerEvent,
} from "../protocol/events";
import { addInspectorPayload } from "./inspectorEntries";

interface ArtifactContentEvent {
  type: "artifact_content";
  artifact_id?: string;
  conversation_id?: string;
  content?: string;
  preview?: string;
  media_type?: string;
  url?: string;
  purpose?: string;
}

export const handleArtifactEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  const eventOwner = (e as unknown as { conversation_id?: unknown }).conversation_id;
  const owner = typeof eventOwner === "string" && eventOwner.trim()
    ? eventOwner.trim()
    : conversationId?.trim() || undefined;
  switch (e.type) {
    case "artifact_content": {
      const ev = e as ArtifactContentEvent;
      if (ev.artifact_id) {
        const artifact = {
          artifactId: ev.artifact_id,
          content: ev.content ?? "",
          preview: ev.preview,
          mediaType: ev.media_type,
          url: ev.url,
          loadedAt: Date.now(),
        };
        const shouldUpdatePreview = ev.purpose !== "image_preview" && ev.purpose !== "attachment";
        if (shouldUpdatePreview && owner) {
          s.setConversationPreviewArtifact(owner, artifact);
        } else if (shouldUpdatePreview && !owner) {
          addInspectorPayload("artifact", ev.artifact_id, {
            event: e.type,
            unowned: true,
            payload: ev,
          });
        }
        if (owner && ev.media_type?.startsWith("image/") && ev.url) {
          window.dispatchEvent(new CustomEvent("artifact:image-preview", {
            detail: {
              artifactId: ev.artifact_id,
              url: ev.url,
              mediaType: ev.media_type,
              purpose: ev.purpose,
            },
          }));
        }
      }
      return true;
    }
    case "artifact.preview": {
      const ev = e as ArtifactPreviewEvent;
      if (!owner) {
        addInspectorPayload("artifact", ev.artifact_id, {
          event: e.type,
          unowned: true,
          payload: ev,
        });
        return true;
      }
      useAppStore.setState((st) => {
        const targetId = owner;
        const isActive = targetId === st.conversationId;
        const sourceMessages = targetId && !isActive
          ? st.conversationMessages[targetId] ?? []
          : st.messages;
        const idx = sourceMessages.findIndex((m) => m.isStreaming);
        if (idx < 0) return st;
        const next = sourceMessages.slice();
        next[idx] = {
          ...next[idx],
          artifacts: [...(next[idx].artifacts ?? []), {
            artifactId: ev.artifact_id,
            kind: ev.kind,
            summary: ev.summary,
            bytes: ev.bytes,
            mediaType: ev.media_type,
            url: ev.url,
          }],
        };
        if (targetId && !isActive) {
          return {
            conversationMessages: {
              ...st.conversationMessages,
              [targetId]: next,
            },
          };
        }
        return {
          messages: next,
          conversationMessages: st.conversationId
            ? { ...st.conversationMessages, [st.conversationId]: next }
            : st.conversationMessages,
        };
      });
      return true;
    }
    case "citation.add": {
      const ev = e as CitationAddEvent;
      if (!owner) {
        addInspectorPayload("message", ev.message_id, {
          event: e.type,
          unowned: true,
          payload: ev,
        });
        return true;
      }
      useAppStore.setState((st) => {
        const targetId = owner;
        const isActive = targetId === st.conversationId;
        const sourceMessages = targetId && !isActive
          ? st.conversationMessages[targetId] ?? []
          : st.messages;
        const idxById = sourceMessages.findIndex((m) => m.id === ev.message_id);
        const idx = idxById >= 0 ? idxById : sourceMessages.findIndex((m) => m.isStreaming);
        if (idx < 0) return st;
        const next = sourceMessages.slice();
        const existing = next[idx].citations ?? [];
        next[idx] = {
          ...next[idx],
          citations: [...existing, { source: ev.source, range: ev.range, label: ev.label, url: ev.url, title: ev.title }],
        };
        if (targetId && !isActive) {
          return {
            conversationMessages: {
              ...st.conversationMessages,
              [targetId]: next,
            },
          };
        }
        return {
          messages: next,
          conversationMessages: st.conversationId
            ? { ...st.conversationMessages, [st.conversationId]: next }
            : st.conversationMessages,
        };
      });
      return true;
    }
    case "inspector.update": {
      const ev = e as InspectorUpdateEvent;
      s.addInspectorEntry({
        targetKind: ev.target_kind,
        targetId: ev.target_id,
        payload: ev.payload,
        timestamp: Date.now(),
      });
      return true;
    }
    default:
      return false;
  }
};
