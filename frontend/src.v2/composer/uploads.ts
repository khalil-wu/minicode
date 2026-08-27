import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import { uploadAttachment, type UploadResponse } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { LS, writeLS } from "../stores/shared-helpers";
import type {
  AppStore,
  ComposerAttachment,
  ConversationWorkbenchState,
} from "../stores/types";
import { getPastedTextMetadata, PASTED_TEXT_INPUT_SOURCE } from "./pastedText";
import { prepareNativeImageFile } from "./imagePreparation";

const activeUploads = new Map<string, AbortController>();
const cancelledUploads = new Set<string>();

const attachmentId = (): string => {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `att-${crypto.randomUUID()}`;
  }
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
};

const uploadWarning = (
  result: { attachment?: Record<string, unknown> },
): string | undefined => {
  const parseError = String(result.attachment?.parse_error ?? "").trim();
  return parseError ? shortUploadError(parseError) : undefined;
};

const shortUploadError = (value: string): string => {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return "上传失败";
  if (/session|socket|connect/i.test(normalized)) return "连接已断开";
  if (/timed?\s*out|超时/i.test(normalized)) return "上传超时，请重试";
  if (normalized.length > 90) return `${normalized.slice(0, 87)}…`;
  return normalized;
};

const cloneWorkbench = (state: AppStore): ConversationWorkbenchState => ({
  diffReview: state.diffReview ? {
    ...state.diffReview,
    files: state.diffReview.files.map((file) => ({ ...file })),
    fileDecisions: { ...state.diffReview.fileDecisions },
    lineComments: state.diffReview.lineComments?.map((comment) => ({ ...comment })) ?? [],
  } : null,
  previewArtifact: state.previewArtifact ? { ...state.previewArtifact } : null,
  livePreviewUrl: state.livePreviewUrl,
  activeTerminalSessionId: state.activeTerminalSessionId,
  rightStackTab: state.rightStackTab,
  rightPanelOpen: state.rightPanelOpen,
  rightStackTabLocked: state.rightStackTabLocked,
  draft: state.draft,
  attachments: state.attachments.map((attachment) => ({ ...attachment })),
  quotedMessage: state.quotedMessage ? { ...state.quotedMessage } : null,
  selectedMentions: state.selectedMentions.map((mention) => ({ ...mention })),
  selectedSkills: state.selectedSkills.map((skill) => ({ ...skill })),
  allowedRemoteImageDomains: [...state.allowedRemoteImageDomains],
});

const mergeAttachments = (
  current: ComposerAttachment[],
  incoming: ComposerAttachment[],
): ComposerAttachment[] => {
  const next = current.map((attachment) => ({ ...attachment }));
  for (const attachment of incoming) {
    const index = next.findIndex((item) => item.id === attachment.id);
    if (index >= 0) next[index] = { ...next[index], ...attachment };
    else next.push({ ...attachment });
  }
  return next;
};

const patchAttachment = (
  id: string,
  patch: Partial<ComposerAttachment>,
  ownerConversationId = "",
  seed?: ComposerAttachment,
): void => {
  useAppStore.setState((state) => {
    const patchList = (items: ComposerAttachment[], insert: boolean): ComposerAttachment[] => {
      const index = items.findIndex((item) => item.id === id);
      if (index < 0) {
        return insert && seed ? [...items, { ...seed, ...patch }] : items;
      }
      const next = items.slice();
      next[index] = { ...next[index], ...patch };
      return next;
    };

    const ownerId = ownerConversationId.trim();
    const liveBelongsToOwner = !ownerId
      || !state.conversationId
      || state.conversationId === ownerId;
    const attachments = liveBelongsToOwner
      ? patchList(state.attachments, false)
      : state.attachments;

    if (!ownerId) return { attachments };
    const existingWorkbench = state.conversationWorkbenchStates[ownerId];
    const ownerWorkbench = existingWorkbench ?? {
      ...cloneWorkbench(state),
      attachments: [],
    };
    return {
      attachments,
      conversationWorkbenchStates: {
        ...state.conversationWorkbenchStates,
        [ownerId]: {
          ...ownerWorkbench,
          attachments: patchList(ownerWorkbench.attachments ?? [], true),
        },
      },
    };
  });
};

const adoptUploadOwner = (
  ownerConversationId: string,
  batchAttachments: ComposerAttachment[],
  initialConversationId: string,
  initialWorkbench: ConversationWorkbenchState,
): void => {
  const ownerId = ownerConversationId.trim();
  if (!ownerId) return;
  let activated = false;

  useAppStore.setState((state) => {
    const ownedBatch = batchAttachments.map((attachment) => {
      const live = state.attachments.find((item) => item.id === attachment.id);
      const stored = Object.values(state.conversationWorkbenchStates)
        .flatMap((workbench) => workbench.attachments ?? [])
        .find((item) => item.id === attachment.id);
      return {
        ...attachment,
        ...stored,
        ...live,
        conversationId: ownerId,
      };
    });
    const existingMeta = state.conversations.find((conversation) => conversation.id === ownerId);
    const conversations = existingMeta
      ? state.conversations
      : [{
          id: ownerId,
          title: "New chat",
          updatedAt: new Date().toISOString(),
          workspaceRoot: state.workingDirectory || undefined,
          goal: null,
        }, ...state.conversations];
    const shouldActivate = !initialConversationId && !state.conversationId;
    activated = shouldActivate;
    const baseWorkbench = state.conversationWorkbenchStates[ownerId]
      ?? (shouldActivate ? cloneWorkbench(state) : initialWorkbench);
    const ownerWorkbench: ConversationWorkbenchState = {
      ...baseWorkbench,
      attachments: mergeAttachments(baseWorkbench.attachments ?? [], ownedBatch),
    };
    const conversationWorkbenchStates = {
      ...state.conversationWorkbenchStates,
      [ownerId]: ownerWorkbench,
    };
    const conversationMessages = {
      ...state.conversationMessages,
      [ownerId]: state.conversationMessages[ownerId] ?? [],
    };
    const conversationStreaming = {
      ...state.conversationStreaming,
      [ownerId]: state.conversationStreaming[ownerId] ?? false,
    };
    const conversationAgentStates = {
      ...state.conversationAgentStates,
      [ownerId]: state.conversationAgentStates[ownerId]
        ?? { plan: null, todos: [], subagents: [], agentProgress: [] },
    };

    if (!shouldActivate) {
      return {
        conversations,
        conversationMessages,
        conversationStreaming,
        conversationAgentStates,
        conversationWorkbenchStates,
      };
    }

    return {
      conversations,
      conversationId: ownerId,
      activeGoal: existingMeta?.goal ?? null,
      messages: conversationMessages[ownerId],
      isStreaming: conversationStreaming[ownerId],
      toolCallCount: 0,
      attachments: mergeAttachments(state.attachments, ownedBatch),
      conversationMessages,
      conversationStreaming,
      conversationAgentStates,
      conversationWorkbenchStates,
    };
  });

  if (activated) writeLS(LS.conversation.activeId, ownerId);
  sendClientCommand({ type: "conversation.list" });
};

export const acceptAttachmentConversationOwner = (ownerConversationId: string): boolean => {
  const ownerId = ownerConversationId.trim();
  if (!ownerId) return false;
  const state = useAppStore.getState();
  if (state.conversationId && state.conversationId !== ownerId) return false;
  adoptUploadOwner(
    ownerId,
    [],
    String(state.conversationId || ""),
    cloneWorkbench(state),
  );
  return true;
};

const normalizedTransportAttachment = (
  file: File,
  result: UploadResponse,
): Record<string, unknown> => {
  const pastedText = getPastedTextMetadata(file);
  const transportAttachment = { ...result.attachment };
  delete transportAttachment.data;
  return {
    ...transportAttachment,
    ...(pastedText ? {
      input_source: PASTED_TEXT_INPUT_SOURCE,
      source_char_count: pastedText.charCount,
    } : {}),
  };
};

const performUpload = async (
  attachment: ComposerAttachment,
  sessionId: string,
  ownerConversationId: string,
): Promise<UploadResponse | null> => {
  if (!attachment.localFile || cancelledUploads.has(attachment.id)) return null;
  const controller = new AbortController();
  activeUploads.get(attachment.id)?.abort();
  activeUploads.set(attachment.id, controller);
  patchAttachment(attachment.id, {
    status: "uploading",
    error: undefined,
    progress: 0,
    uploadPhase: "uploading",
    ...(ownerConversationId ? { conversationId: ownerConversationId } : {}),
  }, ownerConversationId, attachment);

  try {
    const uploadFile = attachment.localFile.type.startsWith("image/")
      ? await prepareNativeImageFile(attachment.localFile)
      : attachment.localFile;
    const result = await uploadAttachment(
      sessionId,
      ownerConversationId,
      uploadFile,
      {
        signal: controller.signal,
        onProgress: (progress, uploadPhase) => {
          if (activeUploads.get(attachment.id) !== controller) return;
          patchAttachment(
            attachment.id,
            { progress, uploadPhase },
            ownerConversationId,
            attachment,
          );
        },
      },
    );
    if (activeUploads.get(attachment.id) !== controller) return null;
    const actualOwner = String(result.conversation_id || "").trim();
    if (!actualOwner) {
      throw new Error("附件上传没有返回所属会话，请重试。");
    }
    if (ownerConversationId && actualOwner !== ownerConversationId) {
      throw new Error("附件所属会话已变化，请重新上传。");
    }
    activeUploads.delete(attachment.id);
    return result;
  } catch (error: unknown) {
    if (activeUploads.get(attachment.id) !== controller) return null;
    activeUploads.delete(attachment.id);
    if (controller.signal.aborted || cancelledUploads.has(attachment.id)) return null;
    const message = error && typeof error === "object" && "message" in error
      ? String((error as { message?: unknown }).message)
      : "上传失败";
    patchAttachment(attachment.id, {
      status: "error",
      progress: undefined,
      uploadPhase: undefined,
      error: shortUploadError(message),
    }, ownerConversationId, attachment);
    return null;
  }
};

const completeUpload = (
  attachment: ComposerAttachment,
  result: UploadResponse,
  ownerConversationId: string,
): void => {
  cancelledUploads.delete(attachment.id);
  patchAttachment(attachment.id, {
    status: "ready",
    progress: 100,
    uploadPhase: undefined,
    conversationId: ownerConversationId,
    artifactId: result.artifact_id,
    docId: result.doc_id,
    attachment: normalizedTransportAttachment(attachment.localFile as File, result),
    error: uploadWarning(result),
  }, ownerConversationId, attachment);
};

export const uploadComposerFiles = (files: File[]) => {
  const sessionId = getWebSocket()?.sessionId?.trim() || "";
  const initialState = useAppStore.getState();
  const initialConversationId = String(initialState.conversationId || "").trim();
  const created: ComposerAttachment[] = files.map((file) => {
    const isImage = file.type.startsWith("image/");
    const pastedText = getPastedTextMetadata(file);
    return {
      id: attachmentId(),
      name: file.name || (isImage ? "粘贴的图片.png" : "附件"),
      type: file.type || "application/octet-stream",
      size: file.size,
      status: sessionId ? "uploading" : "error",
      progress: sessionId ? 0 : undefined,
      uploadPhase: sessionId ? "uploading" : undefined,
      conversationId: initialConversationId || undefined,
      dataUrl: isImage ? URL.createObjectURL(file) : undefined,
      inputSource: pastedText?.inputSource ?? "upload",
      sourceCharCount: pastedText?.charCount,
      localFile: file,
      ...(!sessionId ? { error: "连接已断开，请在重连后上传。" } : {}),
    };
  });

  for (const attachment of created) {
    cancelledUploads.delete(attachment.id);
    useAppStore.getState().addAttachment(attachment);
    if (attachment.inputSource === "pasted_text" && attachment.sourceCharCount) {
      pushToast(
        `长文本（${attachment.sourceCharCount.toLocaleString()} 个字符）已作为 ${attachment.name} 附加，将作为消息内容处理。`,
        "info",
        4200,
      );
    }
  }

  const initialWorkbench = cloneWorkbench(useAppStore.getState());
  if (sessionId && created.length > 0) {
    void (async () => {
      let ownerConversationId = initialConversationId;
      let remaining = created;

      if (!ownerConversationId) {
        remaining = [];
        for (let index = 0; index < created.length; index += 1) {
          const attachment = created[index];
          const result = await performUpload(attachment, sessionId, "");
          if (!result) continue;
          ownerConversationId = String(result.conversation_id || "").trim();
          adoptUploadOwner(ownerConversationId, created, initialConversationId, initialWorkbench);
          completeUpload(attachment, result, ownerConversationId);
          remaining = created.slice(index + 1);
          break;
        }
      } else {
        adoptUploadOwner(ownerConversationId, created, initialConversationId, initialWorkbench);
      }

      if (!ownerConversationId) return;
      await Promise.all(remaining.map(async (attachment) => {
        const result = await performUpload(attachment, sessionId, ownerConversationId);
        if (result) completeUpload(attachment, result, ownerConversationId);
      }));
    })();
  }

  return () => {
    for (const attachment of created) cancelComposerUpload(attachment.id);
  };
};

export const retryComposerAttachment = (id: string): boolean => {
  const state = useAppStore.getState();
  const attachment = state.attachments.find((item) => item.id === id)
    ?? Object.values(state.conversationWorkbenchStates)
      .flatMap((workbench) => workbench.attachments ?? [])
      .find((item) => item.id === id);
  if (!attachment?.localFile) return false;
  const sessionId = getWebSocket()?.sessionId?.trim() || "";
  if (!sessionId) {
    patchAttachment(id, {
      status: "error",
      progress: undefined,
      uploadPhase: undefined,
      error: "连接已断开，请在重连后上传。",
    }, attachment.conversationId, attachment);
    return false;
  }

  cancelledUploads.delete(id);
  const initialConversationId = String(attachment.conversationId || state.conversationId || "").trim();
  const initialWorkbench = cloneWorkbench(state);
  void (async () => {
    const result = await performUpload(attachment, sessionId, initialConversationId);
    if (!result) return;
    const ownerConversationId = String(result.conversation_id || initialConversationId).trim();
    if (!initialConversationId) {
      adoptUploadOwner(ownerConversationId, [attachment], "", initialWorkbench);
    }
    completeUpload(attachment, result, ownerConversationId);
  })();
  return true;
};

export const cancelComposerUpload = (id: string): void => {
  cancelledUploads.add(id);
  activeUploads.get(id)?.abort();
  activeUploads.delete(id);
};
