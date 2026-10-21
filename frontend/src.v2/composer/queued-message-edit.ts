import { useAppStore } from "../stores";
import { composerDraftOf, emptyComposerDraft, type ComposerDraft, type QueuedMessageEdit } from "../stores/composer-drafts";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import { uniqueMessageId } from "../stores/shared-helpers";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { UserMessageQueueAddCommand } from "../protocol/conversation-types";
import type { ChatMessage, ComposerAttachment } from "../stores/types";
import type { SendChatMessageOptions } from "../chat/sendChatMessage";
import { pushToast } from "../overlays/ToastContainer";
import { getDiffCommentScope, readDiffCommentState, setDiffComments, subscribeDiffComments } from "../lib/diff-comment-storage";
import { recordQueuedMessageUndo } from "./queued-message-undo";

const editUndoActions = new WeakMap<QueuedMessageEdit, ReturnType<typeof recordQueuedMessageUndo>>();

const ownerDraft = (ownerId: string): ComposerDraft => {
  const state = useAppStore.getState();
  return state.conversationId === ownerId ? composerDraftOf(state)
    : state.conversationWorkbenchStates[ownerId] ?? emptyComposerDraft();
};

const writeOwnerDraft = (ownerId: string, draft: ComposerDraft) => useAppStore.setState(state => ({
  ...(state.conversationId === ownerId ? { ...draft, richDraft: draft.richDraft, queuedEdit: draft.queuedEdit } : {}),
  conversationWorkbenchStates: { ...state.conversationWorkbenchStates,
    [ownerId]: { ...emptyConversationWorkbenchState(), ...state.conversationWorkbenchStates[ownerId],
      ...draft, richDraft: draft.richDraft, queuedEdit: draft.queuedEdit } },
}));

const commentScope = (edit: QueuedMessageEdit) => getDiffCommentScope(edit.ownerId, edit.workspaceRoot);
const restoreBackup = (edit: QueuedMessageEdit) => {
  writeOwnerDraft(edit.ownerId, edit.backup);
  setDiffComments(commentScope(edit), edit.backupDiffComments);
};

const transportAttachments = (message: ChatMessage): Record<string, unknown>[] => (message.attachmentRefs ?? []).map(ref => ({
  id: ref.id, file_name: ref.name, kind: ref.kind, media_type: ref.mediaType, artifact_id: ref.artifactId,
  doc_id: ref.docId, size_bytes: ref.sizeBytes ?? 0, input_source: ref.inputSource, source_char_count: ref.sourceCharCount ?? 0,
}));

const restoredAttachments = (message: ChatMessage, ownerId: string, workspaceRoot: string): ComposerAttachment[] =>
  (message.attachmentRefs ?? []).map((ref, index) => ({ id: `queued-edit-${message.id}-${index}`,
    name: ref.name, type: ref.mediaType, size: ref.sizeBytes ?? 0, status: "ready", conversationId: ownerId,
    workspaceRoot, artifactId: ref.artifactId, docId: ref.docId, dataUrl: ref.dataUrl,
    attachment: transportAttachments(message)[index], inputSource: ref.inputSource, sourceCharCount: ref.sourceCharCount,
  }));

const addMessage = async (ownerId: string, message: ChatMessage) => {
  const command: UserMessageQueueAddCommand = { type: "user_message.queue.add", conversation_id: ownerId,
    message_id: message.queueMessageId!, user_message_id: message.id, content: message.backendContent ?? message.content,
    display_content: message.content, attachments: transportAttachments(message), context_refs: message.contextRefs ?? [],
    quoted_message: message.quotedMessage ?? null, ...(message.codeReview ? { code_review: message.codeReview } : {}) };
  const result = await sendClientCommandAwaitResult(command, command.type, { silent: true });
  if (!commandResultSucceeded(result) || result.data?.queued !== true) throw new Error(result.message);
};

const restorePosition = async (edit: QueuedMessageEdit, messageId: string) => {
  const ids = useAppStore.getState().getVisibleMessages(edit.ownerId)
    .filter(message => message.role === "user" && message.queueState === "queued")
    .sort((left, right) => (left.queuePosition ?? 0) - (right.queuePosition ?? 0))
    .map(message => message.queueMessageId!).filter(id => id !== messageId);
  const next = edit.nextMessageId ? ids.indexOf(edit.nextMessageId) : -1;
  const previous = edit.previousMessageId ? ids.indexOf(edit.previousMessageId) : -1;
  ids.splice(next >= 0 ? next : previous >= 0 ? previous + 1 : Math.min((edit.original.queuePosition ?? 1) - 1, ids.length), 0, messageId);
  const result = await sendClientCommandAwaitResult({ type: "user_message.queue.reorder", conversation_id: edit.ownerId,
    message_ids: ids }, "user_message.queue.reorder", { silent: true });
  if (!commandResultSucceeded(result)) throw new Error(result.message);
};

const removeQueuedMessage = async (ownerId: string, message: ChatMessage) => {
  const result = await sendClientCommandAwaitResult({ type: "user_message.queue.cancel", conversation_id: ownerId,
    message_id: message.queueMessageId!, user_message_id: message.id }, "user_message.queue.cancel", { silent: true });
  if (!commandResultSucceeded(result) || result.data?.cancelled !== true) throw new Error(result.message || "Queued message is no longer available");
};

const restoreQueuedMessage = async (edit: QueuedMessageEdit) => {
  await addMessage(edit.ownerId, edit.original);
  try { await restorePosition(edit, edit.original.queueMessageId!); }
  catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
};

export const deleteQueuedMessage = async (ownerId: string, message: ChatMessage) => {
  const state = useAppStore.getState();
  const messages = state.getVisibleMessages(ownerId).filter(item => item.role === "user" && item.queueState === "queued")
    .sort((left, right) => (left.queuePosition ?? 0) - (right.queuePosition ?? 0));
  const index = messages.findIndex(item => item.id === message.id);
  const draft = ownerDraft(ownerId);
  const edit: QueuedMessageEdit = { ownerId, workspaceRoot: ownerId === state.conversationId ? state.workingDirectory
    : state.conversations.find(item => item.id === ownerId)?.workspaceRoot ?? "", original: message,
    backup: draft, backupDiffComments: [], previousMessageId: messages[index - 1]?.queueMessageId,
    nextMessageId: messages[index + 1]?.queueMessageId };
  try {
    await removeQueuedMessage(ownerId, message);
    recordQueuedMessageUndo({ ownerId, expiresAfterMs: 60_000,
      undo: () => restoreQueuedMessage(edit), redo: () => removeQueuedMessage(ownerId, message) });
  } catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
};

export const beginQueuedMessageEdit = async (ownerId: string, message: ChatMessage) => {
  const state = useAppStore.getState();
  if (state.conversationId !== ownerId || !message.queueMessageId) return;
  const backup = composerDraftOf(state);
  const ordered = state.messages.filter(item => item.role === "user" && item.queueState === "queued")
    .sort((left, right) => (left.queuePosition ?? 0) - (right.queuePosition ?? 0));
  const index = ordered.findIndex(item => item.id === message.id);
  const edit: QueuedMessageEdit = { ownerId, workspaceRoot: state.workingDirectory, original: message, backup,
    previousMessageId: ordered[index - 1]?.queueMessageId, nextMessageId: ordered[index + 1]?.queueMessageId,
    backupDiffComments: readDiffCommentState(getDiffCommentScope(ownerId, state.workingDirectory)).comments };
  let changed = false;
  const unsubscribe = useAppStore.subscribe((current, previous) => {
    if (current.conversationId !== previous.conversationId || current.pendingConversationSwitchId !== previous.pendingConversationSwitchId
      || current.draft !== previous.draft || current.attachments !== previous.attachments
      || current.selectedMentions !== previous.selectedMentions || current.selectedSkills !== previous.selectedSkills
      || current.quotedMessage !== previous.quotedMessage || current.goalMode !== previous.goalMode) changed = true;
  });
  const unsubscribeComments = subscribeDiffComments(() => { changed = true; });
  try {
    await removeQueuedMessage(ownerId, message);
    if (changed) { await addMessage(ownerId, message); await restorePosition(edit, message.queueMessageId); return; }
    const refs = message.contextRefs ?? [];
    setDiffComments(commentScope(edit), refs.flatMap(ref => ref.kind === "diff_comment" ? [ref.comment] : []));
    writeOwnerDraft(ownerId, { ...emptyComposerDraft(), agentMode: backup.agentMode, queuedEdit: edit,
      draft: message.content, attachments: restoredAttachments(message, ownerId, edit.workspaceRoot),
      quotedMessage: message.quotedMessage ?? null,
      selectedMentions: refs.filter(ref => ["file", "folder", "url", "plugin", "browser_annotation"].includes(ref.kind)) as ComposerDraft["selectedMentions"],
      selectedSkills: refs.filter((ref): ref is ComposerDraft["selectedSkills"][number] => ref.kind === "skill"),
      allowedRemoteImageDomains: backup.allowedRemoteImageDomains });
    let editedDraft = ownerDraft(ownerId);
    let editedComments = readDiffCommentState(commentScope(edit)).comments;
    let phase: "editing" | "undone" = "editing";
    let undoneFingerprint = "";
    const fingerprint = () => {
      const draft = ownerDraft(ownerId);
      return JSON.stringify([draft.draft, draft.attachments.map(attachment => attachment.id), draft.quotedMessage,
        draft.selectedMentions, draft.selectedSkills.map(skill => [skill.name, skill.path]), draft.goalMode,
        readDiffCommentState(commentScope(edit)).comments]);
    };
    let unsubscribeEdit: (() => void) | undefined;
    let unsubscribeUndoComments: (() => void) | undefined;
    if (backup.queuedEdit) editUndoActions.get(backup.queuedEdit)?.invalidate();
    const action = recordQueuedMessageUndo({ ownerId, expiresAfterMs: 30 * 60_000,
      undo: async () => {
        editedDraft = ownerDraft(ownerId);
        editedComments = readDiffCommentState(commentScope(edit)).comments;
        await restoreQueuedMessage(edit);
        if (ownerDraft(ownerId).queuedEdit === edit) restoreBackup(edit);
        undoneFingerprint = fingerprint();
        phase = "undone";
      },
      redo: async () => {
        await removeQueuedMessage(ownerId, message);
        phase = "editing";
        writeOwnerDraft(ownerId, { ...editedDraft, queuedEdit: edit });
        setDiffComments(commentScope(edit), editedComments);
      },
      onFinalize: () => { unsubscribeEdit?.(); unsubscribeUndoComments?.(); },
    });
    const invalidateBranch = () => { if (phase === "undone" && fingerprint() !== undoneFingerprint) action.invalidate(); };
    unsubscribeEdit = useAppStore.subscribe(invalidateBranch);
    unsubscribeUndoComments = subscribeDiffComments(invalidateBranch);
    editUndoActions.set(edit, action);
    window.dispatchEvent(new Event("composer:focus"));
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error");
  } finally { unsubscribe(); unsubscribeComments(); }
};

export const cancelQueuedMessageEdit = async () => {
  const state = useAppStore.getState();
  const edit = state.queuedEdit;
  if (!edit || !state.conversationId) return false;
  try {
    if (!edit.completed) {
      await restoreQueuedMessage(edit);
    }
    editUndoActions.get(edit)?.invalidate();
    if (ownerDraft(edit.ownerId).queuedEdit === edit) restoreBackup(edit);
    return true;
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error");
    return false;
  }
};

export const submitQueuedMessageEdit = async (options: SendChatMessageOptions): Promise<boolean> => {
  const edit = options.queuedEdit!;
  const messageId = uniqueMessageId("a");
  const userMessageId = options.userMessageId ?? uniqueMessageId("u");
  try {
    const result = await sendClientCommandAwaitResult({ type: "user_message.queue.add", conversation_id: edit.ownerId,
      message_id: messageId, user_message_id: userMessageId, content: options.backendContent ?? options.displayContent ?? "",
      display_content: options.displayContent, attachments: options.attachments ?? [], context_refs: options.contextRefs ?? [],
      quoted_message: options.quotedMessage ?? null,
      ...((options.codeReview ?? edit.original.codeReview) ? { code_review: options.codeReview ?? edit.original.codeReview } : {}) },
    "user_message.queue.add", { silent: true });
    if (!commandResultSucceeded(result) || result.data?.queued !== true) throw new Error(result.message);
    editUndoActions.get(edit)?.invalidate();
    options.onAdmission?.(result);
    options.onReceived?.();
    const current = ownerDraft(edit.ownerId);
    if (current.queuedEdit === edit) {
      // A newer draft typed during acceptance stays visible. Its predecessor
      // remains durable and returns when that newer draft has been consumed.
      const empty = !current.draft && !current.attachments.length && !current.quotedMessage
        && !current.selectedMentions.length && !current.selectedSkills.length && !readDiffCommentState(commentScope(edit)).comments.length;
      if (empty) restoreBackup(edit);
      else writeOwnerDraft(edit.ownerId, { ...current, queuedEdit: { ...edit, completed: true } });
    }
    try { await restorePosition(edit, messageId); }
    catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
    return true;
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error");
    return false;
  }
};

export const installQueuedEditRecovery = () => {
  const restore = () => {
    const state = useAppStore.getState();
    const edit = state.queuedEdit;
    if (edit?.completed && !state.draft && !state.attachments.length && !state.quotedMessage
      && !state.selectedMentions.length && !state.selectedSkills.length && !readDiffCommentState(commentScope(edit)).comments.length) restoreBackup(edit);
  };
  restore();
  const observe = () => queueMicrotask(restore);
  const unsubscribe = useAppStore.subscribe(observe);
  const unsubscribeComments = subscribeDiffComments(observe);
  return () => { unsubscribe(); unsubscribeComments(); };
};
