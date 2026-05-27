import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import type { UserMessageCommand } from "../protocol/events";
import { toBackendPermissionMode } from "../protocol/permissions";
import { useAppStore } from "../stores";
import type { MessageAttachmentRef, MessageContextRef } from "../stores/types";

interface SendChatMessageOptions {
  displayContent?: string;
  backendContent?: string;
  attachments?: Record<string, unknown>[];
  attachmentRefs?: MessageAttachmentRef[];
  conversationId?: string;
  contextRefs?: MessageContextRef[];
  allowWhileStreaming?: boolean;
  skipLocalAppend?: boolean;
}

let lastSendSignature = "";
let lastSendAt = 0;

const addSystemNotice = (content: string) => {
  useAppStore.setState((state) => ({
    messages: [
      ...state.messages,
      {
        id: `m-${Date.now().toString(36)}-sys`,
        role: "system" as const,
        content,
        artifacts: [],
        timestamp: Date.now(),
      },
    ],
  }));
};

const isAttachmentRef = (value: unknown): value is MessageAttachmentRef => {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<MessageAttachmentRef>;
  return Boolean(item.id && item.name && item.mediaType);
};

const attachmentRefFromPayload = (payload: Record<string, unknown>): MessageAttachmentRef | null => {
  const name = String(payload.file_name ?? payload.name ?? "").trim();
  const artifactId = String(payload.artifact_id ?? payload.artifactId ?? "").trim();
  if (!name || !artifactId) return null;
  const mediaType = String(payload.media_type ?? payload.mediaType ?? "application/octet-stream");
  const kind = String(payload.kind ?? (mediaType.startsWith("image/") ? "image" : "document"));
  return {
    id: String(payload.id ?? artifactId),
    name,
    kind: kind === "image" ? "image" : kind === "document" ? "document" : "file",
    mediaType,
    sizeBytes: Number(payload.size_bytes ?? payload.sizeBytes ?? 0),
    artifactId,
    docId: String(payload.doc_id ?? payload.docId ?? ""),
    indexedChunks: Number(payload.indexed_chunks ?? payload.indexedChunks ?? 0),
  };
};

export const getChatSendBlockReason = (): string | null => {
  const state = useAppStore.getState();
  if (state.pendingApproval || state.pendingDiffReview || state.pendingAskUser) {
    return "Resolve the pending approval or question first.";
  }
  if (state.isStreaming) {
    return "A response is already running. Stop it before sending another message.";
  }
  if (!state.isConnected) {
    return "Backend is reconnecting. Wait for the connection before sending.";
  }
  return null;
};

const recoverStaleStreamingState = (conversationId?: string): boolean => {
  const state = useAppStore.getState();
  if (!state.isStreaming) return false;
  const activeMessages = conversationId && conversationId !== state.conversationId
    ? state.conversationMessages[conversationId] ?? []
    : state.messages;
  const hasLiveAssistant = activeMessages.some((message) => message.isStreaming || message.isThinkingStreaming);
  if (hasLiveAssistant) return false;
  state.finishStreaming(conversationId);
  return true;
};

export const sendChatMessage = ({
  displayContent,
  backendContent,
  attachments = [],
  attachmentRefs = [],
  conversationId,
  contextRefs = [],
  allowWhileStreaming = false,
  skipLocalAppend = false,
}: SendChatMessageOptions): boolean => {
  const contentForBackend = (backendContent ?? displayContent ?? "").trim();
  const contentForDisplay = (displayContent ?? backendContent ?? "").trim();
  if (!contentForBackend && attachments.length === 0) return false;

  const state = useAppStore.getState();
  const displayAttachmentRefs = attachmentRefs.length > 0
    ? attachmentRefs
    : attachments
        .map(attachmentRefFromPayload)
        .filter((item): item is MessageAttachmentRef => item != null && isAttachmentRef(item));
  if (!state.isConnected) {
    const reason = "Backend is reconnecting. Wait for the connection before sending.";
    addSystemNotice(reason);
    pushToast(reason, "warning");
    return false;
  }

  if (!allowWhileStreaming) {
    if (recoverStaleStreamingState(conversationId)) {
      return sendChatMessage({
        displayContent,
        backendContent,
        attachments,
        attachmentRefs,
        conversationId,
        contextRefs,
        allowWhileStreaming,
        skipLocalAppend,
      });
    }
    const reason = getChatSendBlockReason();
    if (reason) {
      addSystemNotice(reason);
      pushToast(reason, "warning");
      return false;
    }
  }

  const ws = getWebSocket();
  if (!ws) {
    const reason = "Backend connection is not ready yet.";
    addSystemNotice(reason);
    pushToast(reason, "warning");
    return false;
  }

  const sendSignature = JSON.stringify({
    conversationId: conversationId || state.conversationId || "",
    workspaceRoot: state.workingDirectory || "",
    content: contentForBackend,
    attachments: attachments.map((item) => String(item.artifact_id ?? item.artifactId ?? item.id ?? "")).filter(Boolean),
  });
  const now = Date.now();
  if (sendSignature === lastSendSignature && now - lastSendAt < 1000) {
    pushToast("Duplicate send ignored.", "warning");
    return false;
  }

  const command: UserMessageCommand = {
    type: "user_message",
    content: contentForBackend,
    ...(state.workingDirectory ? { workspace_root: state.workingDirectory } : {}),
    permission_mode: toBackendPermissionMode(state.permissionMode),
    ...(conversationId ? { conversation_id: conversationId } : {}),
    ...(attachments.length > 0 ? { attachments } : {}),
  };

  if (!skipLocalAppend && !conversationId) {
    useAppStore.getState().sendMessage(contentForDisplay || contentForBackend, { contextRefs, attachmentRefs: displayAttachmentRefs });
  }

  try {
    ws.send(command);
    lastSendSignature = sendSignature;
    lastSendAt = now;
    return true;
  } catch (err) {
    useAppStore.getState().removeEmptyStreamingAssistant(conversationId);
    useAppStore.getState().finishStreaming(conversationId);
    const message = err instanceof Error ? err.message : "Failed to send message.";
    addSystemNotice(`Error: ${message}`);
    pushToast(message, "error");
    return false;
  }
};
