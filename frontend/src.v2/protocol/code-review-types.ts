export interface CodeReviewScope {
  mode: "uncommitted" | "base-branch";
  cwd: string;
  gitRoot: string;
  baseBranch?: string;
  mergeBaseSha?: string;
}

export interface CodeReviewOptions {
  cwd: string;
  gitRoot: string;
  currentBranch: string;
  branches: string[];
  defaultBaseBranch?: string | null;
  comparisonStatus?: "success" | "expected";
  reason?: "default_branch_unavailable" | "merge_base_unavailable" | null;
}

export interface PreparedCodeReview {
  scope: CodeReviewScope;
  prompt: string;
  requestMessage: string;
  diff: string;
}

export type CodeReviewCommand = {
  type: "review.options";
  conversation_id?: string;
  execution_cwd: string;
} | {
  type: "review.prepare";
  conversation_id?: string;
  execution_cwd: string;
  mode: CodeReviewScope["mode"];
  base_branch?: string;
  merge_base_sha?: string;
  request_message?: string;
};
