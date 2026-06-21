import { useAppStore } from "../stores";
import type {
  ArtifactPreviewEvent,
  CitationAddEvent,
  InspectorUpdateEvent,
  ServerEvent,
} from "../protocol/events";
import { maybeAutoRoutePanel } from "./displayRouting";

interface ArtifactContentEvent {
  type: "artifact_content";
  artifact_id?: string;
  content?: string;
  preview?: string;
  media_type?: string;
  url?: string;
}

export const handleArtifactEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "artifact_content": {
      const ev = e as ArtifactContentEvent;
      if (ev.artifact_id) {
        s.setPreviewArtifact({
          artifactId: ev.artifact_id,
          content: ev.content ?? "",
          preview: ev.preview,
          mediaType: ev.media_type,
          url: ev.url,
          loadedAt: Date.now(),
        });
      }
      return true;
    }
    case "artifact.preview": {
      const ev = e as ArtifactPreviewEvent;
      useAppStore.setState((st) => {
        const targetId = conversationId || st.conversationId || undefined;
        const isActive = !targetId || targetId === st.conversationId;
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
      useAppStore.setState((st) => {
        const targetId = conversationId || st.conversationId || undefined;
        const isActive = !targetId || targetId === st.conversationId;
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
      maybeAutoRoutePanel(ev, "inspector");
      return true;
    }
    default:
      return false;
  }
};
