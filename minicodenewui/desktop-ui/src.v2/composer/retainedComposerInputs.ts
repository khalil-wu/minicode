import { useAppStore } from "../stores";
import type { AppStore, ChatMessage } from "../stores/types";
import { composerDraftAttachments, composerDraftOf, emptyComposerDraft, isComposerConversationDeleted, type ComposerDraft, type RetainedComposerInput } from "../stores/composer-drafts";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import { updateMessagesForConversation } from "../stores/shared-helpers";
import { cloneDiffComments } from "../lib/diff-comments";
import { getDiffCommentScope, readDiffCommentState, setDiffComments, subscribeDiffComments } from "../lib/diff-comment-storage";
import { cancelComposerUpload } from "./uploads";

type Owner = RetainedComposerInput["owner"];
type InputOutcome = { ownerId: string; inputId: string; accepted: boolean; reason?: string };
const outcomesDuringRecovery = new Map<string, InputOutcome>();

/** Only an exact authoritative input identity settles a restored submission. */
export function reconcileRetainedInputOutcome(outcome: InputOutcome): void {
  const state = useAppStore.getState();
  if (!state.composerDraftsReady) {
    outcomesDuringRecovery.set(JSON.stringify([outcome.ownerId, outcome.inputId]), outcome);
    return;
  }
  const input = state.retainedComposerInputs[outcome.inputId];
  if (!input) return;
  const draftContext = input.owner.kind === "draft" ? state.conversationDrafts[input.owner.id] : undefined;
  const ownerId = input.owner.kind === "conversation" ? input.owner.id : draftContext?.conversationId ?? draftContext?.requestedConversationId;
  if (ownerId !== outcome.ownerId) return;
  if (outcome.accepted) {
    completeRetainedInputEdit(outcome.inputId, outcome.ownerId);
  }
  else {
    useAppStore.setState(current => updateMessagesForConversation(current, outcome.ownerId, messages => messages.map(message =>
      message.id === outcome.inputId ? { ...message, deliveryStatus: "failed", failureMessage: outcome.reason } : message)));
    retainFailedComposerInput({ ...input, message: { ...input.message, failureMessage: outcome.reason } });
  }
}

export function reconcileRecoveredInputOutcomes(): void {
  const outcomes = [...outcomesDuringRecovery.values()];
  outcomesDuringRecovery.clear();
  for (const outcome of outcomes) reconcileRetainedInputOutcome(outcome);
}
const activeOwner = (state: AppStore, owner: Owner) => owner.kind === "draft"
  ? state.activeConversationDraftId === owner.id : state.conversationId === owner.id;
const ownerDraft = (state: AppStore, owner: Owner): ComposerDraft => activeOwner(state, owner) ? composerDraftOf(state)
  : owner.kind === "draft" ? state.workspaceComposerDrafts[owner.id] ?? emptyComposerDraft()
    : state.conversationWorkbenchStates[owner.id] ?? emptyComposerDraft();
const writeOwnerDraft = (state: AppStore, owner: Owner, draft: ComposerDraft): Partial<AppStore> => {
  const body = { ...draft, richDraft: draft.richDraft, queuedEdit: draft.queuedEdit, retainedInputEdit: draft.retainedInputEdit };
  return {
    ...(activeOwner(state, owner) ? body : {}),
    ...(owner.kind === "draft" ? { workspaceComposerDrafts: { ...state.workspaceComposerDrafts, [owner.id]: draft } }
      : { conversationWorkbenchStates: { ...state.conversationWorkbenchStates,
        [owner.id]: { ...emptyConversationWorkbenchState(), ...state.conversationWorkbenchStates[owner.id], ...body } } }),
  };
};

const freshPreviews = (draft: ComposerDraft): ComposerDraft => ({ ...draft,
  attachments: draft.attachments.map(attachment => ({ ...attachment,
    ...(attachment.localFile && attachment.type.startsWith("image/") ? { dataUrl: URL.createObjectURL(attachment.localFile) } : {}),
  })),
});

export const retainedInputMessage = (message: ChatMessage, draft: ComposerDraft, unconfirmed = false): ChatMessage => ({ ...message,
  deliveryStatus: unconfirmed ? "sending" : "failed", attachmentRefs: message.attachmentRefs?.map((reference, index) => ({ ...reference,
    // Composer captures these references in the same order as its attachments.
    dataUrl: draft.attachments[index]?.dataUrl ?? reference.dataUrl,
  })),
});

/** Transfer the submitted bytes before clearing the editor, in the same draft database. */
export function retainPendingComposerInput(input: RetainedComposerInput): void {
  if (input.owner.kind === "conversation" && isComposerConversationDeleted(input.owner.id)) return;
  const draft = freshPreviews({ ...input.draft, queuedEdit: undefined, retainedInputEdit: undefined });
  const retained: RetainedComposerInput = { ...input, unconfirmed: true, recovered: undefined, draft, comments: cloneDiffComments(input.comments),
    message: retainedInputMessage(input.message, draft, true) };
  useAppStore.setState(state => ({ retainedComposerInputs: { ...state.retainedComposerInputs, [input.message.id]: retained },
    ...(input.owner.kind === "conversation" && state.preparingComposerMessages[input.owner.id]?.id === input.message.id
      ? { preparingComposerMessages: { ...state.preparingComposerMessages, [input.owner.id]: retained.message } } : {}),
    ...(input.owner.kind === "draft" && state.conversationDrafts[input.owner.id]?.pendingUserMessage?.id === input.message.id
      ? { conversationDrafts: { ...state.conversationDrafts, [input.owner.id]: { ...state.conversationDrafts[input.owner.id], pendingUserMessage: retained.message } } } : {}),
  }));
}

export function discardRetainedComposerInput(id: string): void {
  const input = useAppStore.getState().retainedComposerInputs[id];
  if (!input) return;
  useAppStore.setState(state => {
    const retainedComposerInputs = { ...state.retainedComposerInputs };
    delete retainedComposerInputs[id];
    return { retainedComposerInputs };
  });
  releaseRetainedInputPreviews(input);
}

export function retainFailedComposerInput(input: RetainedComposerInput): void {
  if (input.owner.kind === "conversation" && isComposerConversationDeleted(input.owner.id)) return;
  const previous = useAppStore.getState().retainedComposerInputs[input.message.id];
  const draft = freshPreviews({ ...input.draft, queuedEdit: undefined, retainedInputEdit: undefined });
  useAppStore.setState(state => {
    const retainedComposerInputs = { ...state.retainedComposerInputs,
      [input.message.id]: { ...input, unconfirmed: undefined, draft, comments: cloneDiffComments(input.comments), message: retainedInputMessage(input.message, draft) } };
    const next = { ...state, retainedComposerInputs };
    return { ...(input.owner.kind === "conversation" ? updateMessagesForConversation(next, input.owner.id,
      messages => mergeRetainedComposerMessages(next, input.owner.id, messages)) : {}), retainedComposerInputs };
  });
  if (previous) releaseRetainedInputPreviews(previous);
}

/** Merge at hydration/admission boundaries, not on each streamed token. */
export function mergeRetainedComposerMessages(state: AppStore, ownerId: string, messages: ChatMessage[]): ChatMessage[] {
  const edit = state.conversationId === ownerId ? state.retainedInputEdit : state.conversationWorkbenchStates[ownerId]?.retainedInputEdit;
  const inputs = Object.values(state.retainedComposerInputs).filter(input => input.owner.kind === "conversation" && input.owner.id === ownerId
    && !(edit?.inputId === input.message.id && !edit.completed));
  if (!inputs.length) return messages;
  const byId = new Map(inputs.map(input => [input.message.id, input.message]));
  const existing = new Set(messages.map(message => message.id));
  const next = messages.map(message => message.deliveryStatus === "failed" || message.deliveryStatus === "sending"
    ? byId.get(message.id) ?? message : message);
  for (const input of inputs.sort((left, right) => left.message.timestamp - right.message.timestamp)) {
    if (existing.has(input.message.id)) continue;
    const index = next.findIndex(message => message.timestamp > input.message.timestamp);
    next.splice(index < 0 ? next.length : index, 0, input.message);
  }
  return next;
}

const noDraftInputs: RetainedComposerInput[] = [];
export const visibleRetainedComposerInputs = (state: AppStore): RetainedComposerInput[] => !state.activeConversationDraftId ? noDraftInputs : Object.values(state.retainedComposerInputs)
  .filter(input => input.owner.kind === "draft" && activeOwner(state, input.owner)
    && !(state.retainedInputEdit?.inputId === input.message.id && !state.retainedInputEdit.completed));

export function restoreRetainedComposerInput(id: string): boolean {
  const state = useAppStore.getState(), input = state.retainedComposerInputs[id];
  if (!input || !activeOwner(state, input.owner)) return false;
  if (input.unconfirmed && !input.recovered) return false;
  if (state.retainedInputEdit?.inputId === id && !state.retainedInputEdit.completed) return true;
  const scope = getDiffCommentScope(input.owner.id, input.owner.workspaceRoot);
  const draft = freshPreviews(input.draft);
  const restored = { ...draft, retainedInputEdit: { inputId: id, backup: composerDraftOf(state),
    backupDiffComments: cloneDiffComments(readDiffCommentState(scope).comments) } };
  // This is a local unsent input. Recalling it cannot truncate accepted turns.
  useAppStore.setState(current => {
    const messages = current.messages.filter(message => message.id !== id || (!input.unconfirmed && message.deliveryStatus !== "failed"));
    return { messages, ...(input.owner.kind === "conversation" ? { conversationMessages: {
      ...current.conversationMessages, [input.owner.id]: messages } } : {}),
      ...writeOwnerDraft(current, input.owner, restored) };
  });
  setDiffComments(scope, input.comments);
  window.dispatchEvent(new Event("composer:focus"));
  return true;
}

export function cancelRetainedInputEdit(): boolean {
  const state = useAppStore.getState(), edit = state.retainedInputEdit;
  if (!edit) return false;
  const owner: Owner = state.activeConversationDraftId ? { kind: "draft", id: state.activeConversationDraftId, workspaceRoot: state.workingDirectory }
    : { kind: "conversation", id: state.conversationId!, workspaceRoot: state.workingDirectory };
  const preservedAttachments = [...composerDraftAttachments(edit.backup),
    ...Object.values(state.retainedComposerInputs).flatMap(input => input.draft.attachments)];
  const preservedIds = new Set(preservedAttachments.map(attachment => attachment.id));
  const preservedPreviews = new Set(preservedAttachments.map(attachment => attachment.dataUrl));
  for (const attachment of state.attachments) {
    if (attachment.status === "uploading" && !preservedIds.has(attachment.id)) cancelComposerUpload(attachment.id);
    if (attachment.dataUrl?.startsWith("blob:") && !preservedPreviews.has(attachment.dataUrl)) URL.revokeObjectURL(attachment.dataUrl);
  }
  useAppStore.setState(current => {
    const draftPatch = writeOwnerDraft(current, owner, edit.backup);
    const next = { ...current, ...draftPatch };
    return { ...(owner.kind === "conversation" ? updateMessagesForConversation(next, owner.id,
      messages => mergeRetainedComposerMessages(next, owner.id, messages)) : {}), ...draftPatch };
  });
  setDiffComments(getDiffCommentScope(owner.id, owner.workspaceRoot), edit.backupDiffComments);
  return true;
}

function markDelivered(draft: ComposerDraft, ids: ReadonlySet<string>): ComposerDraft {
  const edit = draft.retainedInputEdit;
  const queued = draft.queuedEdit;
  return { ...draft,
    ...(edit ? { retainedInputEdit: { ...edit, backup: markDelivered(edit.backup, ids),
      completed: edit.completed || ids.has(edit.inputId) } } : {}),
    ...(queued ? { queuedEdit: { ...queued, backup: markDelivered(queued.backup, ids) } } : {}),
  };
}

export function completeRetainedInputEdit(id: string, conversationId?: string): void {
  const inputs = useAppStore.getState().retainedComposerInputs;
  const input = inputs[id];
  if (!input) return;
  const messageOwner = conversationId ?? (input.owner.kind === "conversation" ? input.owner.id : undefined);
  const accepted = new Set([id]);
  let source = input.sourceInputId;
  while (source) { accepted.add(source); source = inputs[source]?.sourceInputId; }
  useAppStore.setState(state => {
    const retainedComposerInputs = { ...state.retainedComposerInputs };
    for (const identifier of accepted) delete retainedComposerInputs[identifier];
    const retiredLocalInput = (message: ChatMessage) => accepted.has(message.id) && (message.deliveryStatus === "failed"
      || (message.id !== id && inputs[message.id]?.unconfirmed && message.deliveryStatus === "sending"));
    const retiringPreviews = new Set(input.draft.attachments.map(attachment => attachment.dataUrl));
    const messagePatch = messageOwner ? updateMessagesForConversation(state, messageOwner,
      messages => {
        let changed = false;
        const next = messages.flatMap(message => {
          if (retiredLocalInput(message)) { changed = true; return []; }
          if (message.id !== id || (!input.unconfirmed && message.deliveryStatus !== "sending")) return [message];
          // A restored local preview retires with this record. The admitted
          // message now owns its persisted artifact, not the discarded blob URL.
          const attachmentRefs = message.attachmentRefs?.map(reference => {
            if (!reference.artifactId || !reference.dataUrl?.startsWith("blob:") || !retiringPreviews.has(reference.dataUrl)) return reference;
            const { dataUrl: _preview, ...durable } = reference;
            return durable;
          });
          changed = true;
          return [{ ...message, deliveryStatus: "sent" as const, attachmentRefs }];
        });
        return changed ? next : null;
      }) : {};
    return { ...messagePatch, ...writeOwnerDraft(state, input.owner, markDelivered(ownerDraft(state, input.owner), accepted)), retainedComposerInputs };
  });
  for (const identifier of accepted) if (inputs[identifier]) releaseRetainedInputPreviews(inputs[identifier]);
}

export function releaseRetainedInputPreviews(input: RetainedComposerInput): void {
  for (const attachment of input.draft.attachments) {
    if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL(attachment.dataUrl);
  }
}

/** Keep a newer composer draft until it is consumed, then return its backup. */
export function installRetainedInputEditRecovery() {
  const restore = () => {
    const state = useAppStore.getState(), edit = state.retainedInputEdit;
    if (edit?.completed && !state.goalMode && !state.draft && !state.attachments.length && !state.quotedMessage
      && !state.selectedMentions.length && !state.selectedSkills.length
      && !readDiffCommentState(getDiffCommentScope(state.activeConversationDraftId || state.conversationId, state.workingDirectory)).comments.length) cancelRetainedInputEdit();
  };
  restore();
  const observe = () => queueMicrotask(restore);
  const stopState = useAppStore.subscribe((state, previous) => {
    if (state.retainedInputEdit?.completed && (state.retainedInputEdit !== previous.retainedInputEdit
      || state.draft !== previous.draft || state.attachments !== previous.attachments || state.quotedMessage !== previous.quotedMessage
      || state.selectedMentions !== previous.selectedMentions || state.selectedSkills !== previous.selectedSkills
      || state.goalMode !== previous.goalMode)) observe();
  });
  const stopComments = subscribeDiffComments(() => { if (useAppStore.getState().retainedInputEdit?.completed) observe(); });
  return () => { stopState(); stopComments(); };
}
