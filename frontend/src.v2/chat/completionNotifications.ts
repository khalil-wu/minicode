import { desktop } from "../desktop/runtime";
import type { CompletionNotification } from "../protocol/notification-types";
import type { ScheduledTaskRunEntry } from "../stores/types";
import { useAppStore } from "../stores";

export const notifyCompletedTurn = (
  conversationId: string, notification: CompletionNotification | null | undefined,
  identity: { turnId?: string; messageId?: string },
) => {
  if (!notification?.eligible || notification.body === null) return;
  const conversation = useAppStore.getState().conversations.find(item => item.id === conversationId);
  void desktop()?.notify({ kind: "completion", title: conversation?.title || "回合完成",
    body: notification.body, target: { kind: "conversation", conversationId }, ...identity });
};

const observedRunsByWorkspace = new Map<string, Map<string, string>>();

export const notifyScheduledCompletions = (workspaceRoot: string, runs: ScheduledTaskRunEntry[]) => {
  const previous = observedRunsByWorkspace.get(workspaceRoot);
  observedRunsByWorkspace.set(workspaceRoot, new Map(runs.map(run => [run.id, run.status])));
  if (previous === undefined) return;
  for (const run of runs) {
    if (run.conversation_id && run.completion_notification && run.status !== previous.get(run.id)
      && ["completed", "partial", "failed", "cancelled"].includes(run.status)) {
      notifyCompletedTurn(run.conversation_id, run.completion_notification, { messageId: run.message_id });
    }
  }
};
