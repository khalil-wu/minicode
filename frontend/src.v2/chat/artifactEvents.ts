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
import { normalizeArtifactContentState } from "../lib/artifact-projection";

/**
 * Resolve an event to exactly one assistant message in its owner cache.
 *
 * A message id supplied by the server is an ownership assertion.  Falling
 * back to an unrelated streaming assistant when that assertion is stale is
 * how late screenshots/citations end up in the wrong turn.  The legacy
 * fallback is therefore reserved for payloads that genuinely omit the id and
 * only succeeds when there is one unambiguous live assistant.
 */
const assistantMessageIndex = (
  messages: Array<{ id: string; role: string; isStreaming?: boolean; isThinkingStreaming?: boolean }>,
  messageId: unknown,
): number => {
  const explicitId = typeof messageId === "string" ? messageId.trim() : "";
  if (explicitId) {
    return messages.findIndex((message) => message.role === "assistant" && message.id === explicitId);
  }
  const streaming = messages
    .map((message, index) => ({ message, index }))
    .filter(({ message }) =>
      message.role === "assistant" && (message.isStreaming || message.isThinkingStreaming),
    );
  return streaming.length === 1 ? streaming[0].index : -1;
};

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
    const idx = assistantMessageIndex(sourceMessages, ev.message_id);
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

/**
 * Keep an unprojected citation observable without copying source text or URL
 * values into the Inspector.  Citation payloads can contain private document
 * locations; availability/length is enough to diagnose an ownership mismatch.
 */
const addUnprojectedCitationInspector = (
  ev: CitationAddEvent,
  owner: string | undefined,
  reason: string,
): void => {
  const messageId = typeof ev.message_id === "string" ? ev.message_id.trim() : "";
  const targetId = messageId || `unresolved-citation:${owner || "session"}`;
  const source = typeof ev.source === "string" ? ev.source : "";
  const url = typeof ev.url === "string" ? ev.url : "";
  const title = typeof ev.title === "string" ? ev.title : "";
  const label = typeof ev.label === "string" ? ev.label : "";
  addInspectorPayload("message", targetId, {
    event: "citation.add",
    conversation_id: owner,
    message_id: messageId || undefined,
    source_available: Boolean(source),
    source_characters: source.length,
    url_available: Boolean(url),
    url_characters: url.length,
    title_available: Boolean(title),
    label_available: Boolean(label),
    range: Array.isArray(ev.range) ? ev.range : undefined,
    projected: false,
    projection_reason: reason,
  });
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
        const artifact = normalizeArtifactContentState({
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
        });
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
        addUnprojectedCitationInspector(ev, undefined, "missing_conversation_owner");
        return true;
      }
      const isActive = owner === s.conversationId;
      const sourceMessages = !isActive
        ? s.conversationMessages[owner] ?? []
        : s.messages;
      const targetIndex = assistantMessageIndex(sourceMessages, ev.message_id);
      if (targetIndex < 0) {
        addUnprojectedCitationInspector(ev, owner, "assistant_message_not_found");
        return true;
      }
      useAppStore.setState((st) => {
        const targetId = owner;
        const activeTarget = targetId === st.conversationId;
        const currentMessages = targetId && !activeTarget
          ? st.conversationMessages[targetId] ?? []
          : st.messages;
        const next = currentMessages.slice();
        const existing = next[targetIndex].citations ?? [];
        next[targetIndex] = {
          ...next[targetIndex],
          citations: [...existing, { source: ev.source, range: ev.range, label: ev.label, url: ev.url, title: ev.title }],
        };
        if (targetId && !activeTarget) {
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
