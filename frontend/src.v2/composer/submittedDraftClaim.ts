import { useAppStore } from "../stores";
import { composerWorkspaceKey, emptyComposerDraft, type ComposerDraft } from "../stores/composer-drafts";
import { getDiffCommentScope, readDiffCommentState, subscribeDiffComments } from "../lib/diff-comment-storage";
import { workspaceRootsEqual } from "../lib/workspace-path";

/** One optimistically cleared draft, until its delivery result owns the input. */
export function claimSubmittedDraft({ conversationId, workspaceRoot, userMessageId, selectedCommand }: {
  conversationId: string;
  workspaceRoot: string;
  userMessageId: string;
  selectedCommand: () => string | null;
}) {
  let owner = conversationId;
  const empty = emptyComposerDraft();
  const location = () => {
    const state = useAppStore.getState();
    if (!owner && state.conversationId && state.messages.some(message => message.id === userMessageId)) owner = state.conversationId;
    const active = String(state.conversationId || "") === owner && workspaceRootsEqual(state.workingDirectory, workspaceRoot);
    const draft: ComposerDraft = active ? state : owner
      ? state.conversationWorkbenchStates[owner] ?? empty
      : state.workspaceComposerDrafts[composerWorkspaceKey(workspaceRoot)] ?? empty;
    const scope = getDiffCommentScope(owner, workspaceRoot);
    return { owner, active, draft, scope };
  };
  const signature = () => {
    const { active, draft, scope } = location();
    return JSON.stringify([draft.draft, draft.attachments, draft.selectedMentions, draft.selectedSkills,
      draft.quotedMessage, readDiffCommentState(scope).comments, active ? selectedCommand() : null]);
  };
  const baseline = signature();
  let untouched = true;
  const observe = () => { if (signature() !== baseline) untouched = false; };
  const stopDraft = useAppStore.subscribe(observe);
  const stopComments = subscribeDiffComments(observe);
  return {
    observe,
    canRestore: () => {
      observe();
      const { active, draft, scope } = location();
      return untouched && !draft.draft && !draft.attachments.length && !draft.selectedMentions.length
        && !draft.selectedSkills.length && !draft.quotedMessage && !readDiffCommentState(scope).comments.length
        && (!active || !selectedCommand());
    },
    location,
    release: () => { untouched = false; stopDraft(); stopComments(); },
  };
}
