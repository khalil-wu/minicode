import { copyText } from "../../lib/clipboard";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../../protocol/ws-outbox";
import type { GitReviewSelection } from "../../stores/types";

export const gitApplyCommand = (patch: string): string => ` (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF'\n${patch}\nEOF\n)`;

export async function copyGitApplyCommand(conversationId: string, workspace: string, selection: GitReviewSelection,
  successMessage: string): Promise<boolean> {
  const result = await sendClientCommandAwaitResult({ type: "diff.git_review_patch", conversation_id: conversationId,
    workspace, source: selection.source, base_branch: selection.baseBranch, commit_sha: selection.commitSha },
    "diff.git_review_patch", { silent: true });
  if (!commandResultSucceeded(result)) throw new Error(result.message);
  const patch = (result.data as { unified_diff: string }).unified_diff;
  return copyText(gitApplyCommand(patch), "git apply", successMessage);
}
