import { useMemo } from "react";
import { summarizeTurnDiff } from "../lib/turn-diff";
import { useAppStore } from "../stores";
import { initialDiffReviewPatch } from "./diffReviewState";

export function useTurnChanges() {
  const turnDiff = useAppStore((state) => state.conversationId ? state.turnDiffs[state.conversationId] : undefined);
  const belongsToConversation = useAppStore((state) => Boolean(turnDiff
    && turnDiff.threadId === state.conversationId
    && state.messages.some((message) => message.role === "assistant"
      && message.turnId === turnDiff.turnId
      && (!turnDiff.messageId || message.id === turnDiff.messageId))));
  const summary = useMemo(() => belongsToConversation ? summarizeTurnDiff(turnDiff) : null, [belongsToConversation, turnDiff]);

  const openReview = () => {
    if (!summary || !turnDiff) return;
    const selectedPath = summary.files[0].path;
    const store = useAppStore.getState();
    store.setDiffReviewState({
      requestId: `turn-summary-${turnDiff.turnId}`,
      conversationId: turnDiff.threadId,
      turnId: turnDiff.turnId,
      messageId: turnDiff.messageId,
      toolName: "本轮修改",
      diff: initialDiffReviewPatch(summary.files, selectedPath),
      files: summary.files,
      selectedPath,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  return { summary, openReview };
}
