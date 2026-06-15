import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import type { UserMessageCommand } from "../protocol/events";
import { toBackendPermissionMode } from "../protocol/permissions";
import { useAppStore } from "../stores";
import type { ChatMessage, MessageAttachmentRef, MessageContextRef } from "../stores/types";
import { hasRuntimePendingUserAction, hasRuntimePendingUserActionForConversation } from "../lib/runtime-session";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { normalizeAgentErrorMessage } from "./errorMessages";

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
const DUPLICATE_SEND_WINDOW_MS = 250;

export const resetSendDeduplication = () => {
  lastSendSignature = "";
  lastSendAt = 0;
};

const localMessageId = (prefix = "m"): string =>
  `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

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

const appendLocalUserTurn = ({
  content,
  conversationId,
  contextRefs,
  attachmentRefs,
}: {
  content: string;
  conversationId?: string;
  contextRefs: MessageContextRef[];
  attachmentRefs: MessageAttachmentRef[];
}) => {
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId || undefined;
    const timestamp = Date.now();
    const userMessage: ChatMessage = {
      id: localMessageId("u"),
      role: "user",
      content,
      contextRefs,
      attachmentRefs,
      artifacts: [],
      timestamp,
    };
    const assistantMessage: ChatMessage = {
      id: localMessageId("a"),
      role: "assistant",
      content: "",
      blocks: [],
      artifacts: [],
      timestamp,
      isStreaming: true,
    };

    if (targetId && state.sideChats[targetId]) {
      const thread = state.sideChats[targetId];
      return {
        sideChats: {
          ...state.sideChats,
          [targetId]: {
            ...thread,
            isStreaming: true,
            messages: [...thread.messages, userMessage, assistantMessage],
          },
        },
      };
    }

    if (!targetId || targetId === state.conversationId) {
      const nextMessages = [...state.messages, userMessage, assistantMessage];
      return {
        messages: nextMessages,
        isStreaming: true,
        ...(state.conversationId
          ? {
              conversationMessages: {
                ...state.conversationMessages,
                [state.conversationId]: nextMessages,
              },
              conversationStreaming: {
                ...state.conversationStreaming,
                [state.conversationId]: true,
              },
            }
          : {}),
      };
    }

    const nextMessages = [
      ...(state.conversationMessages[targetId] ?? []),
      userMessage,
      assistantMessage,
    ];
    return {
      conversationMessages: {
        ...state.conversationMessages,
        [targetId]: nextMessages,
      },
      conversationStreaming: {
        ...state.conversationStreaming,
        [targetId]: true,
      },
    };
  });
};

export const getChatSendBlockReason = (conversationId?: string): string | null => {
  const state = useAppStore.getState();
  const targetConversationId = conversationId ?? state.conversationId ?? state.runtimeSession?.active_conversation_id ?? undefined;
  if (
    hasLocalPendingPromptForConversation(
      [state.pendingApproval, state.pendingDiffReview, state.pendingAskUser],
      targetConversationId,
      state.conversationId,
    )
  ) {
    return "Resolve the pending approval or question first.";
  }
  const hasRuntimePending = targetConversationId
    ? hasRuntimePendingUserActionForConversation(state.runtimeSession, targetConversationId)
    : hasRuntimePendingUserAction(state.runtimeSession);
  if (hasRuntimePending) {
    return "Resolve the pending approval or question first.";
  }
  const targetStreaming = conversationId
    ? conversationId === state.conversationId
      ? state.isStreaming
      : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId] ?? false
    : state.isStreaming;
  if (targetStreaming) {
    return "A response is already running. Stop it before sending another message.";
  }
  if (!state.isConnected) {
    return "Backend is reconnecting. Wait for the connection before sending.";
  }
  return null;
};

const recoverStaleStreamingState = (conversationId?: string): boolean => {
  const state = useAppStore.getState();
  const targetStreaming = conversationId
    ? conversationId === state.conversationId
      ? state.isStreaming
      : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId] ?? false
    : state.isStreaming;
  if (!targetStreaming) return false;
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
    const reason = getChatSendBlockReason(conversationId);
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

  const targetConversationId = conversationId || state.conversationId || "";
  const targetConversation = targetConversationId
    ? state.conversations.find((item) => item.id === targetConversationId)
    : undefined;
  const targetWorkspaceRoot = targetConversation?.worktreePath || targetConversation?.workspaceRoot || "";

  const sendSignature = JSON.stringify({
    conversationId: targetConversationId,
    workspaceRoot: targetWorkspaceRoot,
    content: contentForBackend,
    attachments: attachments.map((item) => String(item.artifact_id ?? item.artifactId ?? item.id ?? "")).filter(Boolean),
  });
  const now = Date.now();
  if (sendSignature === lastSendSignature && now - lastSendAt < DUPLICATE_SEND_WINDOW_MS) {
    pushToast("Duplicate send ignored.", "warning");
    return false;
  }

  const command: UserMessageCommand = {
    type: "user_message",
    content: contentForBackend,
    ...(targetWorkspaceRoot ? { workspace_root: targetWorkspaceRoot } : {}),
    ...(targetWorkspaceRoot && state.activeTabPath ? { primaryFile: state.activeTabPath, activeTabPath: state.activeTabPath } : {}),
    permission_mode: toBackendPermissionMode(state.permissionMode),
    ...(targetConversationId ? { conversation_id: targetConversationId } : {}),
    ...(attachments.length > 0 ? { attachments } : {}),
  };

  if (!skipLocalAppend) {
    appendLocalUserTurn({
      content: contentForDisplay || contentForBackend,
      conversationId,
      contextRefs,
      attachmentRefs: displayAttachmentRefs,
    });
  }

  try {
    ws.send(command);
    lastSendSignature = sendSignature;
    lastSendAt = now;
    return true;
  } catch (err) {
    useAppStore.getState().removeEmptyStreamingAssistant(conversationId);
    useAppStore.getState().finishStreaming(conversationId);
    const message = normalizeAgentErrorMessage(err instanceof Error ? err.message : "Failed to send message.");
    addSystemNotice(`Error: ${message}`);
    pushToast(message, "error");
    return false;
  }
};
