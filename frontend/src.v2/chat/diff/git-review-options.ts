import { commandResultSucceeded, sendClientCommandAwaitResult } from "../../protocol/ws-outbox";

export interface GitReviewOptions extends Record<string, unknown> {
  is_git_repo: boolean;
  current_branch: string | null;
  default_base_branch: string | null;
  branches: string[];
  merge_base_sha: string | null;
  commits: { sha: string; committedAt: string; subject: string; message: string }[];
  comparison_status: "success" | "expected";
  reason: string | null;
}

export async function readGitReviewOptions(conversationId: string, workspaceRoot: string, baseBranch?: string): Promise<GitReviewOptions> {
  const result = await sendClientCommandAwaitResult({ type: "diff.git_review_options", conversation_id: conversationId,
    workspace: workspaceRoot, base_branch: baseBranch }, "diff.git_review_options", { silent: true });
  if (!commandResultSucceeded(result)) throw new Error(result.message);
  return result.data as GitReviewOptions;
}
