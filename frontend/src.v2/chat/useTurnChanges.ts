import { useEffect, useMemo, useRef } from "react";
import { summarizeTurnDiff } from "../lib/turn-diff";
import { useAppStore } from "../stores";
import { initialDiffReviewPatch } from "./diffReviewState";
import { loadMessageTurnDiff } from "./loadMessageTurnDiff";
import { pushToast } from "../overlays/ToastContainer";
import type { TurnDiffState } from "../stores/types";
import type { TurnDiffSummary } from "../lib/turn-diff";

export function useTurnChanges() {
  const pending = useRef<AbortController | null>(null);
  const conversationId = useAppStore((state) => state.conversationId);
  const turnDiff = useAppStore((state) => state.conversationId ? state.turnDiffs[state.conversationId] : undefined);
  const belongsToConversation = useAppStore((state) => Boolean(turnDiff
    && turnDiff.threadId === state.conversationId
    && state.messages.some((message) => message.role === "assistant"
      && message.turnId === turnDiff.turnId
      && (!turnDiff.messageId || message.id === turnDiff.messageId))));
  const summary = useMemo(() => belongsToConversation ? summarizeTurnDiff(turnDiff) : null, [belongsToConversation, turnDiff]);
  useEffect(() => () => { pending.current?.abort(); pending.current = null; }, [conversationId, turnDiff?.turnId]);

  const showReview = (ownedDiff: TurnDiffState, loadedSummary: TurnDiffSummary) => {
    const selectedPath = loadedSummary.files[0].path;
    const store = useAppStore.getState();
    if (store.conversationId !== ownedDiff.threadId || store.turnDiffs[ownedDiff.threadId] !== ownedDiff) return;
    useAppStore.setState({ gitReviewRequest: null });
    store.setDiffReviewState({
      requestId: `turn-summary-${ownedDiff.turnId}`,
      conversationId: ownedDiff.threadId,
      turnId: ownedDiff.turnId,
      messageId: ownedDiff.messageId,
      toolName: ownedDiff.source === "workspace_snapshot" ? "工作区比较" : "本轮修改",
      truncated: ownedDiff.truncated,
      diff: initialDiffReviewPatch(loadedSummary.files, selectedPath, store.workingDirectory),
      files: loadedSummary.files,
      selectedPath,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  const openReview = () => {
    if (!summary || !turnDiff) return;
    if (!turnDiff.deferred) { showReview(turnDiff, summary); return; }
    if (!turnDiff.messageId || pending.current) return;
    const request = new AbortController();
    pending.current = request;
    void loadMessageTurnDiff({ conversationId: turnDiff.threadId, messageId: turnDiff.messageId,
      turnId: turnDiff.turnId, revision: turnDiff.revision }, request.signal).then((loaded) => {
      const loadedSummary = summarizeTurnDiff(loaded);
      if (loaded && loadedSummary && !request.signal.aborted) showReview(loaded, loadedSummary);
    }).catch((error: unknown) => {
      if (!request.signal.aborted) pushToast(error instanceof Error ? error.message : "无法加载历史差异", "error");
    }).finally(() => { if (pending.current === request) pending.current = null; });
  };

  return { summary, openReview };
}
