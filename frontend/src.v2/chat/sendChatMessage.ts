import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import type { CommandResultEvent, UserMessageCommand, UserMessageQueueUpdatedEvent } from "../protocol/events";
import { toBackendPermissionMode } from "../protocol/permissions";
import { useAppStore } from "../stores";
import type { AgentMode, ChatMessage, ComposerQuote, MessageAttachmentRef, MessageContextRef } from "../stores/types";
import { hasRuntimePendingUserAction, hasRuntimePendingUserActionForConversation } from "../lib/runtime-session";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { conversationWorkspacePath, uniqueMessageId, updateMessagesForConversation } from "../stores/shared-helpers";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { parseRuntimeSlashInput } from "../lib/runtime-commands";
import { commandResultSucceeded, notifyClientCommandReceived, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { adoptGeneratedConversation } from "./conversationAdoption";
import { captureSteerTurnId } from "../lib/steer-command";

interface SendChatMessageOptions {
  asyncRequestId?: string;
  displayContent?: string;
  backendContent?: string;
  attachments?: Record<string, unknown>[];
  attachmentRefs?: MessageAttachmentRef[];
  conversationId?: string;
  primaryFile?: string;
  agentMode?: AgentMode;
  codeReview?: import("../protocol/code-review-types").CodeReviewScope;
  contextRefs?: MessageContextRef[];
  quotedMessage?: ComposerQuote | null;
  allowWhileStreaming?: boolean;
  busyBehavior?: "queue" | "steer";
  expectedTurnId?: string | null;
  skipLocalAppend?: boolean;
  assistantMessageId?: string;
  userMessageId?: string;
  retryFromMessageId?: string;
  awaitAdmission?: boolean;
  onAdmission?: (event: CommandResultEvent) => void;
  onReceived?: () => void;
  onRejected?: () => void;
}

const pendingInputs = new Map<string, Parameters<typeof appendLocalUserTurn>[0] & {
  workspaceRoot: string;
  queueUpdate?: UserMessageQueueUpdatedEvent;
}>();

export const resetPendingUserMessagesForTests = () => pendingInputs.clear();

const addSystemNotice = (content: string, conversationId?: string) => {
  useAppStore.getState().upsertSystemMessage(
    uniqueMessageId("send-error"),
    content,
    conversationId ? { conversationId } : undefined,
  );
};

const isAttachmentRef = (value: unknown): value is MessageAttachmentRef => {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<MessageAttachmentRef>;
  return Boolean(item.id && item.name && item.mediaType);
};

export const attachmentRefFromPayload = (payload: Record<string, unknown>): MessageAttachmentRef | null => {
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

const attachmentTransportRef = (payload: Record<string, unknown>): Record<string, unknown> => {
  const mediaType = String(payload.media_type ?? payload.mediaType ?? "application/octet-stream");
  return {
    id: String(payload.id ?? ""),
    kind: String(payload.kind ?? (mediaType.startsWith("image/") ? "image" : "document")),
    file_name: String(payload.file_name ?? payload.name ?? ""),
    media_type: mediaType,
    artifact_id: String(payload.artifact_id ?? payload.artifactId ?? ""),
    doc_id: String(payload.doc_id ?? payload.docId ?? ""),
    size_bytes: Number(payload.size_bytes ?? payload.sizeBytes ?? 0),
    title: String(payload.title ?? ""),
    summary: String(payload.summary ?? ""),
    parse_error: String(payload.parse_error ?? ""),
    input_source: String(payload.input_source ?? payload.inputSource ?? ""),
    source_char_count: Number(payload.source_char_count ?? payload.sourceCharCount ?? 0),
  };
};

const appendLocalUserTurn = ({
  asyncRequestId,
  content,
  backendContent,
  codeReview,
  quotedMessage,
  conversationId,
  contextRefs,
  attachmentRefs,
  assistantMessageId,
  userMessageId,
  retryFromMessageId,
  queued,
  includeAssistant = true,
}: {
  asyncRequestId?: string;
  content: string;
  backendContent: string;
  codeReview?: import("../protocol/code-review-types").CodeReviewScope;
  quotedMessage?: ComposerQuote | null;
  conversationId?: string;
  contextRefs: MessageContextRef[];
  attachmentRefs: MessageAttachmentRef[];
  assistantMessageId: string;
  userMessageId: string;
  retryFromMessageId?: string;
  queued: boolean;
  includeAssistant?: boolean;
}) => {
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId || undefined;
    const currentMessages = targetId && state.sideChats[targetId] ? state.sideChats[targetId].messages
      : targetId && targetId !== state.conversationId ? state.conversationMessages[targetId] ?? [] : state.messages;
    // Restore may already have hydrated the durable admission before its reply
    // is replayed. Stable input identity must never append it a second time.
    const retryIndex = retryFromMessageId ? currentMessages.findIndex(message => message.id === retryFromMessageId) : -1;
    const retainedMessages = retryIndex >= 0 ? currentMessages.slice(0, retryIndex) : currentMessages;
    const existingUser = retainedMessages.find(message => message.id === userMessageId);
    const existingAssistant = retainedMessages.find(message => message.id === assistantMessageId);
    if (existingUser && (!includeAssistant || (existingAssistant && (existingUser.queueState === "queued") === queued))) return state;
    const timestamp = Date.now();
    const userMessage: ChatMessage = {
      id: userMessageId,
      role: "user",
      asyncRequestId,
      content,
      backendContent,
      codeReview,
      quotedMessage: quotedMessage ? { ...quotedMessage } : null,
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
      isStreaming: includeAssistant && !queued,
      ...(queued ? { queueState: "queued" as const, queueMessageId: assistantMessageId } : {}),
    };
    const insertInput = (): ChatMessage[] => {
      if (existingUser) {
        const updated = retainedMessages.map(message => message.id === userMessageId ? { ...message,
          queueState: queued ? "queued" as const : undefined, queueMessageId: queued ? assistantMessageId : undefined,
        } : message);
        return existingAssistant ? updated : [...updated, assistantMessage];
      }
      if (!includeAssistant) return [...retainedMessages, userMessage];
      const assistantIndex = retainedMessages.findIndex(message => message.id === assistantMessageId);
      return assistantIndex < 0 ? [...retainedMessages, userMessage, assistantMessage]
        : [...retainedMessages.slice(0, assistantIndex), userMessage, ...retainedMessages.slice(assistantIndex)];
    };

    if (targetId && state.sideChats[targetId]) {
      const thread = state.sideChats[targetId];
      const messages = insertInput();
      return {
        sideChats: {
          ...state.sideChats,
          [targetId]: {
            ...thread,
            isStreaming: queued ? thread.isStreaming : messages.some(message => message.isStreaming || message.isThinkingStreaming),
            messages,
          },
        },
      };
    }

    if (!targetId || targetId === state.conversationId) {
      const nextMessages = insertInput();
      return {
        messages: nextMessages,
        isStreaming: queued ? state.isStreaming : nextMessages.some(message => message.isStreaming || message.isThinkingStreaming),
        ...(state.conversationId
          ? {
              conversationMessages: {
                ...state.conversationMessages,
                [state.conversationId]: nextMessages,
              },
              conversationStreaming: {
                ...state.conversationStreaming,
                [state.conversationId]: queued ? state.conversationStreaming[state.conversationId] ?? state.isStreaming : nextMessages.some(message => message.isStreaming || message.isThinkingStreaming),
              },
            }
          : {}),
      };
    }

    const nextMessages = insertInput();
    return {
      conversationMessages: {
        ...state.conversationMessages,
        [targetId]: nextMessages,
      },
      conversationStreaming: {
        ...state.conversationStreaming,
        [targetId]: queued ? state.conversationStreaming[targetId] ?? false : nextMessages.some(message => message.isStreaming || message.isThinkingStreaming),
      },
    };
  });
};

const conversationSwitchBlockReason = (conversationId?: string): string | null => {
  const state = useAppStore.getState();
  return state.pendingConversationSwitchId && (!conversationId || conversationId === state.conversationId)
    ? "正在切换会话，请等待加载完成后发送。"
    : null;
};

export const getChatSendBlockReason = (conversationId?: string): string | null => {
  const switchReason = conversationSwitchBlockReason(conversationId);
  if (switchReason) return switchReason;
  const state = useAppStore.getState();
  if (!state.composerDraftsReady) return "正在恢复输入草稿，请等待完成后发送。";
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
    )
  ) {
    return "请先处理待确认的授权或问题。";
  }
  const hasRuntimePending = targetConversationId
    ? hasRuntimePendingUserActionForConversation(state.runtimeSession, targetConversationId)
    : hasRuntimePendingUserAction(state.runtimeSession);
  if (hasRuntimePending) {
    return "请先处理待确认的授权或问题。";
  }
  const targetStreaming = conversationId
    ? conversationId === state.conversationId
      ? state.isStreaming
      : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId] ?? false
    : state.isStreaming;
  if (targetStreaming) {
    return "当前对话仍在运行，请等待完成或先停止；你也可以在新对话中继续。";
  }
  return null;
};

export const sendChatMessage = (options: SendChatMessageOptions): boolean | Promise<boolean> => {
  const switchReason = conversationSwitchBlockReason(options.conversationId);
  if (switchReason) {
    pushToast(switchReason, "warning");
    return false;
  }
  const before = useAppStore.getState();
  // Preserve the user's original target through permission waits and durable
  // replay. Callers preparing async context supply the value captured earlier.
  const capturedOptions = Object.hasOwn(options, "expectedTurnId") ? options : {
    ...options,
    expectedTurnId: options.allowWhileStreaming && options.busyBehavior === "steer"
      ? captureSteerTurnId(before, options.conversationId) : undefined,
  };
  if (capturedOptions.expectedTurnId === null) {
    pushToast("当前任务尚未提供回合标识，无法引导；请稍后重试。", "warning");
    return false;
  }
  const conversationId = options.conversationId || before.conversationId || "";
  const change = before.pendingPermissionModeChanges[conversationId];
  if (!change) return sendConfirmedChatMessage(capturedOptions);
  const conversation = before.conversations.find((item) => item.id === conversationId);
  const workspaceRoot = conversationId === (before.conversationId || "")
    ? before.workingDirectory
    : before.sideChats[conversationId]?.workspaceRoot || conversationWorkspacePath(conversation);
  if (!workspaceRootsEqual(change.workspaceRoot, workspaceRoot)) return false;

  return (async () => {
    let pending = change;
    while (pending) {
      if (!await pending.confirmation) return false;
      pending = useAppStore.getState().pendingPermissionModeChanges[conversationId];
      if (pending && !workspaceRootsEqual(pending.workspaceRoot, workspaceRoot)) return false;
    }
    const current = useAppStore.getState();
    const owner = current.conversations.find((item) => item.id === conversationId);
    const sideOwner = current.sideChats[conversationId];
    if (conversationId === (current.conversationId || "")) {
      if (!workspaceRootsEqual(current.workingDirectory, workspaceRoot)) return false;
    } else if ((!owner && !sideOwner)
      || !workspaceRootsEqual(sideOwner?.workspaceRoot || conversationWorkspacePath(owner), workspaceRoot)) {
      return false;
    }
    return sendConfirmedChatMessage({ ...capturedOptions, conversationId: conversationId || undefined });
  })();
};

const sendConfirmedChatMessage = ({
  asyncRequestId,
  displayContent,
  backendContent,
  attachments = [],
  attachmentRefs = [],
  conversationId,
  primaryFile,
  agentMode,
  codeReview,
  contextRefs = [],
  quotedMessage = null,
  allowWhileStreaming = false,
  busyBehavior = "queue",
  expectedTurnId,
  skipLocalAppend = false,
  assistantMessageId: requestedAssistantMessageId,
  userMessageId: requestedUserMessageId,
  retryFromMessageId,
  awaitAdmission = false,
  onAdmission,
  onReceived,
  onRejected,
}: SendChatMessageOptions): boolean | Promise<boolean> => {
  // Permission confirmation can outlive a later navigation request.
  const switchReason = conversationSwitchBlockReason(conversationId);
  if (switchReason) {
    pushToast(switchReason, "warning");
    return false;
  }
  const submittedContent = backendContent ?? displayContent ?? "";
  const contentForBackend = contextRefs.some(ref => ref.kind === "diff_comment") ? submittedContent : submittedContent.trim();
  const contentForDisplay = (displayContent ?? backendContent ?? "").trim();
  if (!contentForBackend && attachments.length === 0) return false;

  const state = useAppStore.getState();
  if (!state.composerDraftsReady) {
    pushToast("正在恢复输入草稿，请等待完成后发送。", "warning");
    return false;
  }
  const isRetry = Boolean(retryFromMessageId?.trim());
  const targetStreaming = conversationId
    ? conversationId === state.conversationId
      ? state.isStreaming
      : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId] ?? false
    : state.isStreaming;
  const steering = busyBehavior === "steer" && expectedTurnId != null;
  const queued = !isRetry && allowWhileStreaming && (targetStreaming || steering);
  const displayAttachmentRefs = (attachmentRefs.length > 0
    ? attachmentRefs
    : attachments
        .map(attachmentRefFromPayload)
        .filter((item): item is MessageAttachmentRef => item != null && isAttachmentRef(item)))
    .map(durableAttachmentRef);
  const transportAttachments = attachments.map(attachmentTransportRef);
  if (!allowWhileStreaming && !isRetry) {
    const reason = getChatSendBlockReason(conversationId);
    if (reason) {
      pushToast(reason, "warning");
      return false;
    }
  }

  const ws = getWebSocket();
  if (!ws) {
    const reason = "尚未连接 MiniCode，请稍后再试。";
    pushToast(reason, "warning");
    return false;
  }

  const targetConversationId = conversationId || state.conversationId || "";
  const targetConversation = targetConversationId
    ? state.conversations.find((item) => item.id === targetConversationId)
    : undefined;
  const isMainConversation = !targetConversationId || targetConversationId === state.conversationId;
  const targetWorkspaceRoot = state.sideChats[targetConversationId]?.workspaceRoot || conversationWorkspacePath(targetConversation);
  const targetPrimaryFile = primaryFile || (isMainConversation && targetWorkspaceRoot ? state.activeTabPath : undefined);
  const targetPermissionMode = !targetConversationId
    ? state.permissionMode
    : undefined;
  const targetAgentMode = agentMode ?? (isMainConversation ? state.agentMode
    : state.conversationWorkbenchStates[targetConversationId]?.agentMode ?? targetConversation?.agentMode ?? "build");

  const assistantMessageId = requestedAssistantMessageId
    || (skipLocalAppend && !awaitAdmission ? "" : uniqueMessageId("a"));
  const userMessageId = requestedUserMessageId
    || (skipLocalAppend && !awaitAdmission ? "" : uniqueMessageId("u"));

  // A manual right-panel tab selection locks auto-routing for the rest of the
  // current interaction. A new user turn should re-enable auto-routing (so the
  // next diff/preview can open) instead of staying locked forever.
  if (!targetConversationId || targetConversationId === state.conversationId) {
    useAppStore.getState().setRightStackTabLocked(false);
  }

  const command: UserMessageCommand & { client_command_id: string } = {
    type: "user_message",
    client_command_id: uniqueMessageId("cmd"),
    content: contentForBackend,
    display_content: contentForDisplay,
    context_refs: contextRefs.map((ref) => ({ ...ref })),
    quoted_message: quotedMessage ? { ...quotedMessage } : null,
    ...(asyncRequestId ? { async_request_id: asyncRequestId } : {}),
    ...(targetWorkspaceRoot ? { workspace_root: targetWorkspaceRoot } : {}),
    ...(targetPrimaryFile ? { primary_file: targetPrimaryFile } : {}),
    ...(isMainConversation && targetWorkspaceRoot && state.activeTabPath ? { active_tab_path: state.activeTabPath } : {}),
    ...(targetPermissionMode ? { permission_mode: toBackendPermissionMode(targetPermissionMode) } : {}),
    ...(codeReview ? { code_review: codeReview } : { agent_mode: targetAgentMode }),
    ...(targetConversationId ? { conversation_id: targetConversationId } : {}),
    ...(transportAttachments.length > 0 ? { attachments: transportAttachments } : {}),
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
    ...(retryFromMessageId ? { retry_from_message_id: retryFromMessageId } : {}),
    ...(!isRetry && allowWhileStreaming ? { queue_if_busy: true } : {}),
    ...(queued ? { streaming_behavior: steering ? "steer" : "follow_up" } : {}),
    ...(steering ? { expected_turn_id: expectedTurnId } : {}),
  };

  if (!skipLocalAppend && !isRetry && !awaitAdmission) {
    appendLocalUserTurn({
      asyncRequestId,
      content: contentForDisplay,
      backendContent: contentForBackend,
      quotedMessage,
      conversationId,
      contextRefs,
      attachmentRefs: displayAttachmentRefs,
      codeReview,
      assistantMessageId,
      userMessageId,
      retryFromMessageId,
      queued,
    });
    if (!queued && targetConversationId && assistantMessageId) {
      useAppStore.getState().flushPendingProviderProgress(targetConversationId, assistantMessageId);
    }
  }

  try {
    if (awaitAdmission || (isRetry && !skipLocalAppend)) pendingInputs.set(command.client_command_id!, {
      content: contentForDisplay, backendContent: contentForBackend, quotedMessage, codeReview, asyncRequestId,
      conversationId: targetConversationId, contextRefs, attachmentRefs: displayAttachmentRefs,
      assistantMessageId, userMessageId, retryFromMessageId, queued: isRetry ? false : queued,
      workspaceRoot: state.workingDirectory,
    });
    if (awaitAdmission) {
      let received = false;
      const admission = sendClientCommandAwaitResult(command, "user_message", { silent: true, onReceived: () => {
        received = true;
        const current = useAppStore.getState();
        const visibleEmptyOwner = !targetConversationId && !current.conversationId && workspaceRootsEqual(current.workingDirectory, state.workingDirectory);
        if (!skipLocalAppend && !isRetry && (targetConversationId || visibleEmptyOwner)) appendLocalUserTurn({
          content: contentForDisplay, backendContent: contentForBackend, quotedMessage, asyncRequestId, conversationId: targetConversationId || undefined,
          contextRefs, attachmentRefs: displayAttachmentRefs, assistantMessageId, userMessageId,
          queued, includeAssistant: false,
        });
        onReceived?.();
      } });
      return admission.then(event => {
        handleUserMessageAdmission(event);
        if (commandResultSucceeded(event) && (event.data?.admitted === true || event.data?.queued === true
          || event.data?.native_completion === "completed")) {
          onAdmission?.(event);
          return true;
        }
        const cancelled = event.data?.cancelled === true;
        if (event.data?.native_completion === "unknown"
          || (!cancelled && commandResultSucceeded(event) && event.data?.admitted !== false)) {
          pushToast(received ? "消息已保存，准入结果尚未确认；等待会话恢复原记录。"
            : "消息接收尚未确认，输入已保留。请先核对会话再重试。", "warning", 6000);
          return false;
        }
        onRejected?.();
        const current = useAppStore.getState();
        const owner = !targetConversationId && current.messages.some(message => message.id === assistantMessageId)
          ? current.conversationId || undefined
          : typeof event.data?.conversation_id === "string" ? event.data.conversation_id : targetConversationId;
        const reason = event.message || (cancelled ? "用户已取消本次发送，输入已保留。" : "消息未被后端接收，输入已保留。");
        if (current.getVisibleMessages(owner || undefined).some(message => message.id === assistantMessageId)) {
          current.finishStreaming(owner || undefined, undefined, cancelled ? "interrupted" : "failed", assistantMessageId,
            reason, true, undefined, cancelled ? String(event.data?.reason || "user_interrupted") : "admission_rejected");
        } else if (!cancelled && received && !skipLocalAppend && !isRetry) {
          addSystemNotice(reason, owner || undefined);
        }
        if (!cancelled && !(skipLocalAppend && parseRuntimeSlashInput(contentForBackend)?.command === "/compact")) {
          pushToast(reason, event.level === "warning" ? "warning" : "error");
        }
        return false;
      }, error => {
        if (!received) {
          onRejected?.();
          pendingInputs.delete(command.client_command_id);
        }
        pushToast(received ? "消息已保存，处理结果尚未确认；重连后恢复原记录。"
          : `消息接收尚未确认，输入已保留。重连后请先核对会话再重试。${error instanceof Error ? ` ${error.message}` : ""}`, "warning", 6000);
        return false;
      });
    }
    if (!ws.send(command)) {
      throw new Error("Backend connection is not ready yet.");
    }
    return true;
  } catch (err) {
    pendingInputs.delete(command.client_command_id!);
    if (!isRetry) useAppStore.getState().removeEmptyStreamingAssistant(conversationId, assistantMessageId);
    if (!queued && !isRetry) {
      useAppStore.getState().finishStreaming(conversationId, undefined, "failed");
    }
    const message = normalizeAgentErrorMessage(err instanceof Error ? err.message : "消息发送失败。");
    addSystemNotice(`错误：${message}`, targetConversationId);
    pushToast(message, "error");
    return false;
  }
};

const appendAdmittedInput = (input: Parameters<typeof appendLocalUserTurn>[0] & { workspaceRoot: string }, owner: string, queued: boolean, started = false) => {
  const state = useAppStore.getState();
  const firstForeground = !input.conversationId && !state.conversationId
    && workspaceRootsEqual(state.workingDirectory, input.workspaceRoot);
  if (firstForeground) {
    const owned = state.conversationMessages[owner];
    if (owned) useAppStore.setState({ messages: owned });
  }
  const appendOwner = firstForeground ? undefined : owner || input.conversationId;
  appendLocalUserTurn({ ...input, conversationId: appendOwner, queued, includeAssistant: queued || started });
  if (firstForeground) adoptGeneratedConversation(owner, input.assistantMessageId, input.userMessageId);
  if (owner && !queued) useAppStore.getState().flushPendingProviderProgress(owner, input.assistantMessageId);
};

export const capturePendingUserMessageStart = (conversationId: string, assistantMessageId: string): void => {
  for (const [commandId, input] of pendingInputs) {
    if (input.assistantMessageId !== assistantMessageId) continue;
    notifyClientCommandReceived(commandId);
    appendAdmittedInput(input, conversationId, false, true);
    return;
  }
};

// Queue updates are already durable input facts. Publish the captured input
// before the normal queue reducer so an immediately following dequeue/item
// frame has its exact UID and assistant to update, even before command.result.
export const capturePendingUserMessageQueueUpdate = (event: UserMessageQueueUpdatedEvent): void => {
  for (const input of pendingInputs.values()) {
    if (input.retryFromMessageId || (input.conversationId && input.conversationId !== event.conversation_id)) continue;
    if (input.assistantMessageId !== event.message_id && input.userMessageId !== event.user_message_id) continue;
    input.queueUpdate = event;
    if (event.status !== "cancelled") appendAdmittedInput(input, event.conversation_id, true);
    return;
  }
};

export const handleUserMessageAdmission = (event: CommandResultEvent & { client_command_id?: string }): void => {
  const id = event.client_command_id || (typeof event.data?.client_command_id === "string" ? event.data.client_command_id : "");
  if (event.command !== "user_message" || !id) return;
  const input = pendingInputs.get(id);
  if (!input) return;
  if (!commandResultSucceeded(event) || (event.data?.admitted !== true && event.data?.queued !== true)) {
    if (event.data?.native_completion !== "unknown" && (event.data?.cancelled === true
      || event.data?.native_completion === "completed" || event.data?.native_completion === "refused"
      || event.data?.admitted === false || !commandResultSucceeded(event))) {
      pendingInputs.delete(id);
      if (input.queued) {
        // ACK proved receipt, not queue admission. This reducer also receives
        // late outcomes after the awaiting UI operation has timed out.
        const owner = typeof event.data?.conversation_id === "string" ? event.data.conversation_id : input.conversationId;
        useAppStore.setState(state => updateMessagesForConversation(state, owner || undefined, messages => messages.map(message =>
          (message.id === input.userMessageId || message.id === input.assistantMessageId) && message.queueState === "queued"
            ? { ...message, queueState: undefined, queueMessageId: undefined, queuePosition: undefined, queuePaused: undefined }
            : message)));
      }
    }
    if (input.retryFromMessageId && event.data?.cancelled !== true && (event.level === "error" || event.level === "failed")) {
      pushToast(event.message || "重新生成未被后端接受，原记录已保留。", "error");
    }
    return;
  }
  pendingInputs.delete(id);
  if (input.queueUpdate) return;
  const owner = typeof event.data?.conversation_id === "string" ? event.data.conversation_id : input.conversationId || "";
  const admittedReview = event.data?.input as { code_review: import("../protocol/code-review-types").CodeReviewScope; display_content: string; content: string } | undefined;
  if (admittedReview) {
    input.codeReview = admittedReview.code_review;
    input.content = admittedReview.display_content;
    input.backendContent = admittedReview.content;
  }
  appendAdmittedInput(input, owner, event.data?.queued === true);
};
