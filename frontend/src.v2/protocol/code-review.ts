import { commandResultSucceeded, sendClientCommandAwaitResult } from "./ws-outbox";
import type { CodeReviewCommand, CodeReviewOptions, CreatedCodeReview, PreparedCodeReview } from "./code-review-types";

async function request<T>(command: CodeReviewCommand): Promise<T> {
  const result = await sendClientCommandAwaitResult(command, command.type, { silent: true });
  if (!commandResultSucceeded(result)) throw new Error(result.message);
  return result.data as T;
}

export function fetchCodeReviewOptions(conversationId: string | undefined, cwd: string): Promise<CodeReviewOptions> {
  return request({ type: "review.options", conversation_id: conversationId, execution_cwd: cwd });
}

export function prepareCodeReview(conversationId: string | undefined, cwd: string, mode: "uncommitted" | "base-branch", baseBranch?: string, mergeBaseSha?: string, requestMessage?: string): Promise<PreparedCodeReview> {
  return request({ type: "review.prepare", conversation_id: conversationId, execution_cwd: cwd,
    mode, base_branch: baseBranch, merge_base_sha: mergeBaseSha, request_message: requestMessage });
}

export function createCodeReview(conversationId: string | undefined, cwd: string, mode: "uncommitted" | "base-branch", baseBranch?: string, mergeBaseSha?: string, requestMessage?: string): Promise<CreatedCodeReview> {
  return request({ type: "review.create", conversation_id: conversationId, execution_cwd: cwd,
    mode, base_branch: baseBranch, merge_base_sha: mergeBaseSha, request_message: requestMessage });
}
