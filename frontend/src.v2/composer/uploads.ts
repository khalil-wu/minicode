import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import { uploadAttachment, type UploadResponse } from "../protocol/api";
import { useAppStore } from "../stores";
import { conversationWorkspacePath } from "../stores/shared-helpers";
import type { ComposerAttachment } from "../stores/types";
import { getPastedTextMetadata, PASTED_TEXT_INPUT_SOURCE } from "./pastedText";
import { prepareNativeImageFile } from "./imagePreparation";
import { desktop } from "../desktop/runtime";
import { composerDraftAttachments, composerDraftOf, updateComposerDraftAttachment } from "../stores/composer-drafts";

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
    if (ownerId && state.sideChats[ownerId]) {
      const thread = state.sideChats[ownerId];
      return { sideChats: { ...state.sideChats, [ownerId]: { ...thread, attachments: patchList(thread.attachments ?? [], false) } } };
    }
    const liveBelongsToOwner = Boolean(ownerId && state.conversationId === ownerId)
      || Boolean(seed?.draftId && state.activeConversationDraftId === seed.draftId);
    const live = liveBelongsToOwner ? updateComposerDraftAttachment(composerDraftOf(state), id, patch) : state;
    const livePatch = { attachments: live.attachments, queuedEdit: live.queuedEdit, retainedInputEdit: live.retainedInputEdit };

    if (!ownerId) {
      const key = seed!.draftId!;
      const draft = state.workspaceComposerDrafts[key];
      return { ...livePatch, ...(draft ? { workspaceComposerDrafts: { ...state.workspaceComposerDrafts,
        [key]: updateComposerDraftAttachment(draft, id, patch),
      } } : {}) };
    }
    const existingWorkbench = state.conversationWorkbenchStates[ownerId];
    if (!existingWorkbench) return livePatch;
    const ownerWorkbench = existingWorkbench;
    return {
      ...livePatch,
      conversationWorkbenchStates: {
        ...state.conversationWorkbenchStates,
        [ownerId]: updateComposerDraftAttachment(ownerWorkbench, id, patch, seed, true),
      },
    };
  });
};

export const acceptAttachmentConversationOwner = async (ownerConversationId: string): Promise<boolean> => {
  const ownerId = ownerConversationId.trim();
  const state = useAppStore.getState();
  if (state.conversationId) return state.conversationId === ownerId;
  const draftId = state.activeConversationDraftId;
  if (!draftId || state.conversationDrafts[draftId].conversationId !== ownerId) return false;
  return await state.materializeConversationDraft(draftId) === ownerId
    && useAppStore.getState().conversationId === ownerId;
};

const normalizedTransportAttachment = (
  file: File | undefined,
  result: UploadResponse,
): Record<string, unknown> => {
  const pastedText = file ? getPastedTextMetadata(file) : undefined;
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
  workspaceRoot: string,
): Promise<UploadResponse | null> => {
  if ((!attachment.localFile && !attachment.remoteSourcePath) || cancelledUploads.has(attachment.id)) return null;
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
    let result: UploadResponse;
    if (attachment.remoteSourcePath) {
      result = await desktop()!.attachments.importRemoteFile({
        path: attachment.remoteSourcePath, sessionId, conversationId: ownerConversationId, workspaceRoot,
      });
    } else {
      const localFile = attachment.localFile!;
      const uploadFile = localFile.type.startsWith("image/") ? await prepareNativeImageFile(localFile) : localFile;
      result = await uploadAttachment(
      sessionId,
      ownerConversationId,
      uploadFile,
      {
        signal: controller.signal,
        workspaceRoot,
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
    }
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
    attachment: normalizedTransportAttachment(attachment.localFile, result),
    ...(result.source ? { remoteSourcePath: result.source.path, name: result.source.name,
      type: result.source.mediaType, size: result.source.sizeBytes } : {}),
    error: uploadWarning(result),
  }, ownerConversationId, attachment);
};

type ComposerUploadInput = { file: File } | { remotePath: string; name: string };

const uploadComposerInputs = (inputs: ComposerUploadInput[], sideConversationId?: string) => {
  const sessionId = getWebSocket()?.sessionId?.trim() || "";
  let initialState = useAppStore.getState();
  if (!initialState.composerDraftsReady) {
    pushToast("正在恢复输入草稿，请等待完成后添加附件。", "warning");
    return () => {};
  }
  if (!sideConversationId && !initialState.conversationId && !initialState.activeConversationDraftId) {
    initialState.startConversationDraft({ bindWorkspace: Boolean(initialState.workingDirectory), appMode: initialState.appMode });
    initialState = useAppStore.getState();
  }
  const initialConversationId = sideConversationId ?? String(initialState.conversationId || "").trim();
  const initialDraftId = sideConversationId ? null : initialState.activeConversationDraftId;
  const initialWorkspaceRoot = sideConversationId ? initialState.sideChats[sideConversationId].workspaceRoot ?? "" : initialState.workingDirectory;
  const created: ComposerAttachment[] = inputs.map((input) => {
    const file = "file" in input ? input.file : undefined;
    const isImage = Boolean(file?.type.startsWith("image/"));
    const pastedText = file ? getPastedTextMetadata(file) : undefined;
    return {
      id: attachmentId(),
      name: "file" in input ? input.file.name || (isImage ? "粘贴的图片.png" : "附件") : input.name,
      type: file?.type || "application/octet-stream",
      size: file?.size ?? 0,
      status: sessionId ? "uploading" : "error",
      progress: sessionId ? 0 : undefined,
      uploadPhase: sessionId ? "uploading" : undefined,
      conversationId: initialConversationId || undefined,
      draftId: initialDraftId || undefined,
      workspaceRoot: initialWorkspaceRoot,
      dataUrl: isImage ? URL.createObjectURL(file!) : undefined,
      inputSource: pastedText?.inputSource ?? "upload",
      sourceCharCount: pastedText?.charCount,
      localFile: file,
      ...("remotePath" in input ? { remoteSourcePath: input.remotePath } : {}),
      ...(!sessionId ? { error: "连接已断开，请在重连后上传。" } : {}),
    };
  });

  for (const attachment of created) {
    cancelledUploads.delete(attachment.id);
    if (sideConversationId) {
      useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [sideConversationId]: {
        ...state.sideChats[sideConversationId], attachments: [...(state.sideChats[sideConversationId].attachments ?? []), attachment],
      } } }));
    } else useAppStore.getState().addAttachment(attachment);
    if (attachment.inputSource === "pasted_text" && attachment.sourceCharCount) {
      pushToast(
        `长文本（${attachment.sourceCharCount.toLocaleString()} 个字符）已作为 ${attachment.name} 附加，将作为消息内容处理。`,
        "info",
        4200,
      );
    }
  }

  if (sessionId && created.length > 0) {
    void (async () => {
      const ownerConversationId = initialConversationId || (initialDraftId
        ? await useAppStore.getState().materializeConversationDraft(initialDraftId) : null);
      if (!ownerConversationId) {
        for (const attachment of created) patchAttachment(attachment.id, { status: "error", progress: undefined,
          uploadPhase: undefined, error: "会话尚未创建，附件已保留，可重试。" }, "", attachment);
        return;
      }
      const workspace = conversationWorkspacePath(useAppStore.getState().conversations.find(item => item.id === ownerConversationId)) || initialWorkspaceRoot;
      await Promise.all(created.map(async (attachment) => {
        const result = await performUpload(attachment, sessionId, ownerConversationId, workspace);
        if (result) completeUpload(attachment, result, ownerConversationId);
      }));
    })().finally(() => {
      for (const attachment of created) cancelledUploads.delete(attachment.id);
    });
  }

  return () => {
    for (const attachment of created) cancelComposerUpload(attachment.id);
  };
};

export const uploadComposerFiles = (files: File[], sideConversationId?: string) =>
  uploadComposerInputs(files.map(file => ({ file })), sideConversationId);

export const uploadComposerRemoteFiles = (paths: string[]) => uploadComposerInputs(paths.map(remotePath => ({
  remotePath, name: remotePath.split(/[\\/]/).at(-1)!,
})));

export const retryComposerAttachment = (id: string): boolean => {
  const state = useAppStore.getState();
  const attachment = composerDraftAttachments(state).find((item) => item.id === id)
    ?? Object.values(state.sideChats).flatMap((thread) => thread.attachments ?? []).find((item) => item.id === id)
    ?? Object.values(state.conversationWorkbenchStates)
      .flatMap(composerDraftAttachments)
      .find((item) => item.id === id)
    ?? Object.values(state.workspaceComposerDrafts).flatMap(composerDraftAttachments).find(item => item.id === id);
  if (!attachment || (!attachment.localFile && !attachment.remoteSourcePath)) return false;
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
  const draftEntry = Object.entries(state.workspaceComposerDrafts).find(([, body]) => composerDraftAttachments(body).some(item => item.id === id));
  const draftId = attachment.draftId || (draftEntry && state.conversationDrafts[draftEntry[0]] ? draftEntry[0] : undefined);
  const savedOwner = Object.entries(state.conversationWorkbenchStates).find(([, body]) => composerDraftAttachments(body).some(item => item.id === id))?.[0]
    ?? Object.values(state.sideChats).find(thread => thread.attachments?.some(item => item.id === id))?.id;
  const initialConversationId = String(attachment.conversationId || savedOwner
    || (!draftId && state.attachments.some(item => item.id === id) ? state.conversationId : "") || "").trim();
  const initialWorkspaceRoot = attachment.workspaceRoot ?? state.workingDirectory;
  void (async () => {
    const owner = initialConversationId || (draftId ? await useAppStore.getState().materializeConversationDraft(draftId) : null);
    if (!owner) return;
    const seed = { ...attachment, draftId };
    const workspace = conversationWorkspacePath(useAppStore.getState().conversations.find(item => item.id === owner)) || initialWorkspaceRoot;
    const result = await performUpload(seed, sessionId, owner, workspace);
    if (!result) return;
    completeUpload(seed, result, owner);
  })().finally(() => cancelledUploads.delete(id));
  return true;
};

export const cancelComposerUpload = (id: string): void => {
  const state = useAppStore.getState();
  const pending = composerDraftAttachments(state).some((attachment) => attachment.id === id && attachment.status === "uploading")
    || Object.values(state.sideChats).some((thread) => thread.attachments?.some((attachment) => attachment.id === id && attachment.status === "uploading"))
    || Object.values(state.conversationWorkbenchStates).some((workbench) => (
      composerDraftAttachments(workbench).some((attachment) => attachment.id === id && attachment.status === "uploading")
    )) || Object.values(state.workspaceComposerDrafts).some(draft => composerDraftAttachments(draft).some(attachment => attachment.id === id && attachment.status === "uploading"));
  if (pending || activeUploads.has(id)) cancelledUploads.add(id);
  activeUploads.get(id)?.abort();
  activeUploads.delete(id);
};

export const cancelConversationComposerUploads = (conversationId: string): void => {
  const state = useAppStore.getState();
  const attachments = [
    ...(state.conversationId === conversationId ? composerDraftAttachments(state) : []),
    ...(state.conversationWorkbenchStates[conversationId] ? composerDraftAttachments(state.conversationWorkbenchStates[conversationId]) : []),
    ...(state.sideChats[conversationId]?.attachments ?? []),
    ...Object.values(state.workspaceComposerDrafts).flatMap(draft => composerDraftAttachments(draft).filter(attachment => attachment.conversationId === conversationId)),
  ];
  for (const attachment of attachments) cancelComposerUpload(attachment.id);
};
