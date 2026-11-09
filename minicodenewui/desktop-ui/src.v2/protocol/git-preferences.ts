export interface GitPreferences {
  branch_prefix: string;
  always_force_push: boolean;
  create_draft_pull_requests: boolean;
  pull_request_merge_method: "merge" | "squash";
  commit_instructions: string;
  pull_request_instructions: string;
  review_delivery: "inline" | "detached";
}
