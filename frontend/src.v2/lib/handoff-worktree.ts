import { pushToast } from "../overlays/ToastContainer";

const activeHandoffs = new Map<string, Promise<void>>();

export function handoffConversationWorktree(id: string, taskName: string, target: "local" | "worktree"): Promise<void> {
  const active = activeHandoffs.get(id);
  if (active) return active;
  const operation = import("./handoff-worktree-action")
    .then(({ runHandoff }) => runHandoff(id, taskName, target))
    .catch(error => {
      pushToast(error instanceof Error ? error.message : String(error), "error", 6000);
    })
    .finally(() => activeHandoffs.delete(id));
  activeHandoffs.set(id, operation);
  return operation;
}
