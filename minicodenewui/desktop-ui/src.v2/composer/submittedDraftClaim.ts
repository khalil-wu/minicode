import { useAppStore } from "../stores";
import { composerWorkspaceKey, emptyComposerDraft, isComposerConversationDeleted, type ComposerDraft } from "../stores/composer-drafts";
import { getDiffCommentScope, readDiffCommentState, subscribeDiffComments } from "../lib/diff-comment-storage";
import { workspaceRootsEqual } from "../lib/workspace-path";

/** One optimistically cleared draft, until its delivery result owns the input. */
export function claimSubmittedDraft({ conversationId, draftId, workspaceRoot, userMessageId, selectedCommand }: {
  conversationId: string;
  draftId?: string | null;
  workspaceRoot: string;
  userMessageId: string;
  selectedCommand: () => string | null;
}) {
  let owner = conversationId;
  const empty = emptyComposerDraft();
  const location = () => {
    const state = useAppStore.getState();
    const context = draftId ? state.conversationDrafts[draftId] : undefined;
    if (context?.conversationId) owner = context.conversationId;
    if (!owner && state.conversationId && state.messages.some(message => message.id === userMessageId)) owner = state.conversationId;
    const active = draftId ? state.activeConversationDraftId === draftId || Boolean(owner && state.conversationId === owner)
      : String(state.conversationId || "") === owner && workspaceRootsEqual(state.workingDirectory, workspaceRoot);
    const draft: ComposerDraft = active ? state : context && !context.adopted
      ? state.workspaceComposerDrafts[context.id] ?? empty : owner
      ? state.conversationWorkbenchStates[owner] ?? empty
      : state.workspaceComposerDrafts[draftId || composerWorkspaceKey(workspaceRoot)] ?? empty;
    const scope = getDiffCommentScope(context && !context.adopted ? context.id : owner || draftId, workspaceRoot);
    return { owner, active, draft, scope };
  };
  const signature = () => {
    const { active, draft, scope } = location();
    return JSON.stringify([draft.draft, draft.richDraft, draft.attachments, draft.selectedMentions, draft.selectedSkills,
      draft.quotedMessage, readDiffCommentState(scope).comments, active ? selectedCommand() : null]);
  };
  const baseline = signature();
  let untouched = true;
  // A subsequent edit permanently owns the composer. Once that happens, do
  // not serialize its rich document/attachments again for unrelated updates.
  const observe = () => { if (untouched && signature() !== baseline) untouched = false; };
  const stopDraft = useAppStore.subscribe(observe);
  const stopComments = subscribeDiffComments(observe);
  return {
    observe,
    canRestore: () => {
      observe();
      if (!untouched) return false;
      const { active, draft, scope } = location();
      if (owner && isComposerConversationDeleted(owner)) return false;
      return !draft.draft && !draft.attachments.length && !draft.selectedMentions.length
        && !draft.selectedSkills.length && !draft.quotedMessage && !readDiffCommentState(scope).comments.length
        && (!active || !selectedCommand());
    },
    location,
    release: () => { untouched = false; stopDraft(); stopComments(); },
  };
}
