import { useAppStore } from "../stores";
import type { ChatMessage, DiffReviewState } from "../stores/types";
import type { PreparedCodeReview } from "../protocol/code-review-types";
import { extractFilePathFromDiff, parseUnifiedDiffLines } from "../lib/unified-diff";
import { parseCodeComments, type CodeReviewComment } from "../lib/code-comment";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";
import { emptyConversationWorkbenchState, liveConversationWorkbenchState } from "../stores/ui-slice";

export function codeReviewComments(messages: ChatMessage[], workspaceRoot: string): CodeReviewComment[] {
  const comments = new Map<string, CodeReviewComment>();
  for (const message of messages) {
    if (message.role !== "assistant" || message.isStreaming) continue;
    for (const comment of parseCodeComments(message.content, workspaceRoot)) {
      comments.set(JSON.stringify([workspaceFilePathComparisonKey(comment.file, workspaceRoot), comment.start, comment.end, comment.title, comment.body]), comment);
    }
  }
  return [...comments.values()];
}

export function codeReviewState(prepared: PreparedCodeReview, conversationId: string, userMessageId: string): DiffReviewState {
  const files = prepared.diff.split(/(?=^diff --git )/m).filter(patch => patch.trim()).map(patch => ({
    path: extractFilePathFromDiff(parseUnifiedDiffLines(patch)), patch,
  }));
  return { requestId: `code-review:${userMessageId}`, conversationId, workspaceRoot: prepared.scope.gitRoot,
    toolName: "Code review", mode: "view", status: "viewing", diff: prepared.diff, files,
    fileDecisions: {}, codeReview: prepared.scope };
}

export function publishCodeReview(prepared: PreparedCodeReview, conversationId: string, userMessageId: string): void {
  const review = codeReviewState(prepared, conversationId, userMessageId);
  useAppStore.setState(state => ({
    ...(state.conversationId === conversationId ? { diffReview: review, gitReviewRequest: null } : {}),
    conversationWorkbenchStates: { ...state.conversationWorkbenchStates,
      [conversationId]: { ...(state.conversationId === conversationId ? liveConversationWorkbenchState(state)
        : state.conversationWorkbenchStates[conversationId] ?? emptyConversationWorkbenchState()), diffReview: review, gitReviewRequest: null } },
  }));
  if (useAppStore.getState().conversationId === conversationId) useAppStore.getState().setRightStackTab("diff");
}
