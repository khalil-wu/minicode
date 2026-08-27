import { useAppStore } from "../stores";
import type {
  ArtifactPreviewEvent,
  ArtifactContentEvent,
  CitationAddEvent,
  InspectorUpdateEvent,
  ServerEvent,
} from "../protocol/events";
import { addInspectorPayload } from "./inspectorEntries";
import { matchesPreviewRequestId } from "./previewRequestScope";

export const projectArtifactPreviewEvent = (
  ev: ArtifactPreviewEvent,
  owner: string,
): boolean => {
  let projected = false;
  useAppStore.setState((st) => {
    const targetId = owner.trim();
    if (!targetId) return st;
    const isActive = targetId === st.conversationId;
    const sourceMessages = !isActive
      ? st.conversationMessages[targetId] ?? []
      : st.messages;
    const idxById = sourceMessages.findIndex((message) => message.id === ev.message_id);
    const idx = idxById >= 0
      ? idxById
      : sourceMessages.findIndex((message) => message.isStreaming);
    if (idx < 0) return st;

    const next = sourceMessages.slice();
    const artifacts = next[idx].artifacts ?? [];
    const artifactIndex = artifacts.findIndex((artifact) => artifact.artifactId === ev.artifact_id);
    const existing = artifactIndex >= 0 ? artifacts[artifactIndex] : undefined;
    const artifact = {
      ...existing,
      artifactId: ev.artifact_id,
      kind: ev.kind,
      summary: ev.summary,
      bytes: ev.bytes ?? existing?.bytes,
      mediaType: ev.media_type ?? existing?.mediaType,
      url: ev.url ?? existing?.url,
      textOffset: typeof ev.text_offset === "number" && Number.isFinite(ev.text_offset)
        ? ev.text_offset
        : existing?.textOffset,
    };
    const nextArtifacts = artifacts.slice();
    if (artifactIndex >= 0) nextArtifacts[artifactIndex] = artifact;
    else nextArtifacts.push(artifact);
    next[idx] = { ...next[idx], artifacts: nextArtifacts };
    projected = true;

    if (!isActive) {
      return {
        conversationMessages: {
          ...st.conversationMessages,
          [targetId]: next,
        },
      };
    }
    return {
      messages: next,
      conversationMessages: {
        ...st.conversationMessages,
        [targetId]: next,
      },
    };
  });
  return projected;
};

export const handleArtifactEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  const eventOwner = (e as unknown as { conversation_id?: unknown }).conversation_id;
  const owner = typeof eventOwner === "string" && eventOwner.trim()
    ? eventOwner.trim()
    : conversationId?.trim() || undefined;
  switch (e.type) {
    case "artifact_content": {
      const ev = e as ArtifactContentEvent;
      if (ev.artifact_id && owner && matchesPreviewRequestId(owner, ev.request_id)) {
        const existing = owner
          ? (
              owner === s.conversationId
                ? s.previewArtifact
                : s.conversationWorkbenchStates?.[owner]?.previewArtifact
            )
          : null;
        const sameArtifact = existing?.artifactId === ev.artifact_id ? existing : null;
        const artifact = {
          ...(sameArtifact ?? {}),
          artifactId: ev.artifact_id,
          content: ev.content ?? "",
          preview: ev.preview,
          mediaType: ev.media_type || sameArtifact?.mediaType,
          url: ev.url || sameArtifact?.url,
          name: ev.name || sameArtifact?.name,
          source: ev.is_attachment ? "attachment" as const : "artifact" as const,
          loading: false,
          error: undefined,
          loadedAt: Date.now(),
        };
        s.setConversationPreviewArtifact(owner, artifact);
      }
      return true;
    }
    case "artifact.preview": {
      const ev = e as ArtifactPreviewEvent;
      if (!owner) {
        addInspectorPayload("artifact", ev.artifact_id, {
          event: e.type,
          unowned: true,
          message_id: ev.message_id,
          kind: ev.kind,
          summary: ev.summary,
          bytes: ev.bytes,
          media_type: ev.media_type,
          text_offset: ev.text_offset,
          url_available: Boolean(ev.url),
          url_characters: ev.url?.length ?? 0,
        });
        return true;
      }
      if (!projectArtifactPreviewEvent(ev, owner)) {
        addInspectorPayload("artifact", ev.artifact_id, {
          event: e.type,
          conversation_id: owner,
          message_id: ev.message_id,
          kind: ev.kind,
          summary: ev.summary,
          bytes: ev.bytes,
          media_type: ev.media_type,
          text_offset: ev.text_offset,
          url_available: Boolean(ev.url),
          url_characters: ev.url?.length ?? 0,
          projected: false,
        });
      }
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
      if (!owner || owner !== s.conversationId) return true;
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
