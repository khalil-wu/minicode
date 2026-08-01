import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import type { UserMessageCommand } from "../protocol/events";
import { toBackendPermissionMode } from "../protocol/permissions";
import { useAppStore } from "../stores";
import type { ChatMessage, MessageAttachmentRef, MessageContextRef } from "../stores/types";
import { hasRuntimePendingUserAction, hasRuntimePendingUserActionForConversation } from "../lib/runtime-session";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { uniqueMessageId } from "../stores/shared-helpers";

interface SendChatMessageOptions {
  displayContent?: string;
  backendContent?: string;
  attachments?: Record<string, unknown>[];
  attachmentRefs?: MessageAttachmentRef[];
  conversationId?: string;
  contextRefs?: MessageContextRef[];
  allowWhileStreaming?: boolean;
  skipLocalAppend?: boolean;
  assistantMessageId?: string;
  userMessageId?: string;
}

let lastSendSignature = "";
let lastSendAt = 0;
const DUPLICATE_SEND_WINDOW_MS = 250;

export const resetSendDeduplication = () => {
  lastSendSignature = "";
  lastSendAt = 0;
};

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
    dataUrl: typeof payload.dataUrl === "string" ? payload.dataUrl : undefined,
    inputSource: payload.input_source === "pasted_text" || payload.inputSource === "pasted_text"
      ? "pasted_text"
      : "upload",
    sourceCharCount: Number(payload.source_char_count ?? payload.sourceCharCount ?? 0) || undefined,
  };
};

const durableAttachmentRef = (attachment: MessageAttachmentRef): MessageAttachmentRef => {
  if (!attachment.artifactId || !attachment.dataUrl?.startsWith("blob:")) return attachment;
  const durable = { ...attachment };
  delete durable.dataUrl;
  return durable;
};

const contextRefSignature = (ref: MessageContextRef): Record<string, unknown> => {
  if (ref.kind === "plugin") {
    return { kind: ref.kind, configName: ref.configName, path: ref.path };
  }
  if (ref.kind === "skill") {
    return { kind: ref.kind, name: ref.name, path: ref.path ?? "" };
  }
  if (ref.kind === "browser_annotation") {
    return {
      kind: ref.kind,
      url: ref.url,
      note: ref.note,
      selector: ref.selector ?? "",
      targetId: ref.targetId ?? "",
      xPercent: ref.xPercent ?? null,
      yPercent: ref.yPercent ?? null,
      widthPercent: ref.widthPercent ?? null,
      heightPercent: ref.heightPercent ?? null,
    };
  }
  return { kind: ref.kind, path: ref.path };
};

const appendLocalUserTurn = ({
  content,
  conversationId,
  contextRefs,
  attachmentRefs,
  assistantMessageId,
  userMessageId,
  queued,
}: {
  content: string;
  conversationId?: string;
  contextRefs: MessageContextRef[];
  attachmentRefs: MessageAttachmentRef[];
  assistantMessageId: string;
  userMessageId: string;
  queued: boolean;
}) => {
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId || undefined;
    const timestamp = Date.now();
    const resetRunState = {
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
    };
    const userMessage: ChatMessage = {
      id: userMessageId,
      role: "user",
      content,
      contextRefs,
      attachmentRefs,
      artifacts: [],
      timestamp,
      ...(queued ? { queueState: "queued" as const, queueMessageId: assistantMessageId } : {}),
    };
    const assistantMessage: ChatMessage = {
      id: assistantMessageId,
      role: "assistant",
      content: "",
      blocks: [],
      artifacts: [],
      timestamp,
      isStreaming: !queued,
      ...(queued ? { queueState: "queued" as const, queueMessageId: assistantMessageId } : {}),
    };

    if (targetId && state.sideChats[targetId]) {
      const thread = state.sideChats[targetId];
      return {
        ...(queued ? {} : resetRunState),
        ...(queued ? {} : { gitChanges: { ...state.gitChanges, live: null } }),
        sideChats: {
          ...state.sideChats,
          [targetId]: {
            ...thread,
            isStreaming: queued ? thread.isStreaming : true,
            messages: [...thread.messages, userMessage, assistantMessage],
          },
        },
      };
    }

    if (!targetId || targetId === state.conversationId) {
      const nextMessages = [...state.messages, userMessage, assistantMessage];
      return {
        ...(queued ? {} : resetRunState),
        messages: nextMessages,
        isStreaming: queued ? state.isStreaming : true,
        ...(queued ? {} : { gitChanges: { ...state.gitChanges, live: null } }),
        ...(state.conversationId
          ? {
              conversationAgentStates: {
                ...(state.conversationAgentStates ?? {}),
                [state.conversationId]: queued
                  ? state.conversationAgentStates[state.conversationId] ?? resetRunState
                  : resetRunState,
              },
              conversationMessages: {
                ...state.conversationMessages,
                [state.conversationId]: nextMessages,
              },
              conversationStreaming: {
                ...state.conversationStreaming,
                [state.conversationId]: queued ? state.conversationStreaming[state.conversationId] ?? state.isStreaming : true,
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
        [targetId]: queued ? state.conversationStreaming[targetId] ?? false : true,
      },
      ...(queued ? {} : { gitChanges: { ...state.gitChanges, live: null } }),
      ...(queued ? {} : {
        conversationAgentStates: {
          ...(state.conversationAgentStates ?? {}),
          [targetId]: resetRunState,
        },
      }),
    };
  });
};

export const getChatSendBlockReason = (conversationId?: string): string | null => {
  const state = useAppStore.getState();
  const targetConversationId = conversationId ?? state.conversationId ?? state.runtimeSession?.active_conversation_id ?? undefined;
  if (
    hasLocalPendingPromptForConversation(
      [
        state.pendingApproval,
        ...state.approvalQueue,
        state.pendingDiffReview,
        ...state.diffReviewQueue,
        state.pendingAskUser,
        ...state.askUserQueue,
      ],
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
    return "This conversation is still running. Wait or stop it before sending here; a new conversation can continue separately.";
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
  assistantMessageId: requestedAssistantMessageId,
  userMessageId: requestedUserMessageId,
}: SendChatMessageOptions): boolean => {
  const contentForBackend = (backendContent ?? displayContent ?? "").trim();
  const contentForDisplay = (displayContent ?? backendContent ?? "").trim();
  if (!contentForBackend && attachments.length === 0) return false;

  const state = useAppStore.getState();
  const targetStreaming = conversationId
    ? conversationId === state.conversationId
      ? state.isStreaming
      : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId] ?? false
    : state.isStreaming;
  const queued = allowWhileStreaming && targetStreaming;
  const displayAttachmentRefs = (attachmentRefs.length > 0
    ? attachmentRefs
    : attachments
        .map(attachmentRefFromPayload)
        .filter((item): item is MessageAttachmentRef => item != null && isAttachmentRef(item)))
    .map(durableAttachmentRef);
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
        assistantMessageId: requestedAssistantMessageId,
        userMessageId: requestedUserMessageId,
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
    contextRefs: contextRefs.map(contextRefSignature),
  });
  const now = Date.now();
  if (sendSignature === lastSendSignature && now - lastSendAt < DUPLICATE_SEND_WINDOW_MS) {
    pushToast("Duplicate send ignored.", "warning");
    return false;
  }
  const assistantMessageId = requestedAssistantMessageId
    || (skipLocalAppend ? "" : uniqueMessageId("a"));
  const userMessageId = requestedUserMessageId
    || (skipLocalAppend ? "" : uniqueMessageId("u"));

  // A manual right-panel tab selection locks auto-routing for the rest of the
  // current interaction. A new user turn should re-enable auto-routing (so the
  // next diff/preview can open) instead of staying locked forever.
  useAppStore.getState().setRightStackTabLocked(false);

  const command: UserMessageCommand = {
    type: "user_message",
    content: contentForBackend,
    ...(targetWorkspaceRoot ? { workspace_root: targetWorkspaceRoot } : {}),
    ...(targetWorkspaceRoot && state.activeTabPath ? { primaryFile: state.activeTabPath, activeTabPath: state.activeTabPath } : {}),
    permission_mode: toBackendPermissionMode(state.permissionMode),
    agent_mode: state.agentMode,
    ...(targetConversationId ? { conversation_id: targetConversationId } : {}),
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(contextRefs.some((ref) => ref.kind === "skill" && ref.path)
      ? {
          skills: contextRefs
            .filter((ref): ref is Extract<MessageContextRef, { kind: "skill" }> => ref.kind === "skill")
            .filter((ref) => Boolean(ref.path))
            .map((ref) => ({ name: ref.name, path: String(ref.path) })),
        }
      : {}),
    ...(contextRefs.some((ref) => ref.kind === "plugin")
      ? {
          plugins: contextRefs
            .filter((ref): ref is Extract<MessageContextRef, { kind: "plugin" }> => ref.kind === "plugin")
            .map((ref) => ({ config_name: ref.configName, path: ref.path })),
        }
      : {}),
    ...(assistantMessageId ? { assistant_message_id: assistantMessageId } : {}),
    ...(userMessageId ? { user_message_id: userMessageId } : {}),
    ...(allowWhileStreaming ? { queue_if_busy: true } : {}),
  };

  if (!skipLocalAppend) {
    appendLocalUserTurn({
      content: contentForDisplay || contentForBackend,
      conversationId,
      contextRefs,
      attachmentRefs: displayAttachmentRefs,
      assistantMessageId,
      userMessageId,
      queued,
    });
  }

  try {
    if (!ws.send(command)) {
      throw new Error("Backend connection is not ready yet.");
    }
    lastSendSignature = sendSignature;
    lastSendAt = now;
    return true;
  } catch (err) {
    useAppStore.getState().removeEmptyStreamingAssistant(conversationId, assistantMessageId);
    if (!queued) {
      useAppStore.getState().finishStreaming(conversationId, undefined, "failed");
    }
    const message = normalizeAgentErrorMessage(err instanceof Error ? err.message : "Failed to send message.");
    addSystemNotice(`Error: ${message}`);
    pushToast(message, "error");
    return false;
  }
};
