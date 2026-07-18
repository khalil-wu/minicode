/**
 * Workspace, environment, and git event types.
 *
 * Domain subset of the WebSocket protocol for events related to workspace
 * management, file watching, git diff staging, environment variables,
 * checkpoint management, and command results.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

// ──────────────────────────────────────────────────────────────────
// Server event type strings (workspace domain)
// ──────────────────────────────────────────────────────────────────

export type WorkspaceServerEventType =
  // File watcher
  | "file.changed"
  // Commands / artifacts
  | "command.result"
  | "command_output_chunk"
  | "artifact_content"
  | "artifact.preview"
  // Workspace
  | "workspace.imported"
  | "workspace.recent.list"
  // Environment
  | "env.list"
  // Git CI
  | "git.pr_status"
  // Checkpoints
  | "checkpoint.created"
  | "checkpoint.list"
  | "checkpoint.rewound"
  | "checkpoint.run.list"
  | "checkpoint.run.resume"
  // Guidelines / permissions
  | "guidelines.updated"
  | "permission.mode.updated"
  | "permission.rules.updated"
  // Git diff
  | "diff.git_working_tree"
  | "diff.git_staged"
  | "diff.git_stage_file"
  | "diff.git_unstage_file"
  | "diff.git_stage_all"
  | "diff.git_unstage_all"
  | "diff.git_revert_file";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (workspace domain)
// ──────────────────────────────────────────────────────────────────

export type WorkspaceClientCommandType =
  | "workspace.import"
  | "workspace.switch"
  | "workspace.recent"
  | "env.list"
  | "env.set"
  | "env.delete"
  | "git.pr_status"
  | "checkpoint.list"
  | "checkpoint.rewind"
  | "checkpoint.run.list"
  // Git diff
  | "diff.git_working_tree"
  | "diff.git_staged"
  | "diff.git_stage_file"
  | "diff.git_unstage_file"
  | "diff.git_stage_all"
  | "diff.git_unstage_all"
  | "diff.git_revert_file";

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface FileChangedEvent {
  type: "file.changed";
  path: string;
  event: string;
}

export interface CommandResultEvent {
  type: "command.result";
  command: string;
  level: string;
  message: string;
  title?: string;
  data?: Record<string, unknown>;
}

export interface EnvListEvent {
  type: "env.list";
  entries?: { name: string; description: string; scope: string }[];
}

export interface GitPrStatusEvent {
  type: "git.pr_status";
  pr?: { number: number; title: string; state: string; url: string; branch: string } | null;
  checks?: { name: string; status: string; url: string }[];
  error?: string;
}

export interface GitDiffFilePayload {
  path: string;
  patch: string;
  additions: number;
  deletions: number;
  is_binary?: boolean;
}

export interface GitDiffWorkingTreeEvent {
  type: "diff.git_working_tree";
  files?: GitDiffFilePayload[];
  untracked?: string[];
  preview?: boolean;
  tool_call_id?: string;
  progress?: number;
}

export interface GitDiffStagedEvent {
  type: "diff.git_staged";
  files?: GitDiffFilePayload[];
}

export interface GitDiffActionEvent {
  type:
    | "diff.git_stage_file"
    | "diff.git_unstage_file"
    | "diff.git_stage_all"
    | "diff.git_unstage_all"
    | "diff.git_revert_file";
  ok?: boolean;
  path?: string;
  message?: string;
}

export interface RunCheckpointRecord {
  run_id?: string;
  session_id?: string;
  conversation_id?: string;
  iteration?: number;
  iterations?: number;
  stopped_reason?: string | null;
  created_at?: number;
  timestamp?: number;
}

export interface RunCheckpointListEvent {
  type: "checkpoint.run.list";
  session_id?: string;
  conversation_id?: string;
  checkpoints?: RunCheckpointRecord[];
  runs?: Record<string, unknown>[];
  subagents?: Record<string, unknown>[];
}

export interface RunCheckpointResumeEvent {
  type: "checkpoint.run.resume";
  resumed: boolean;
  session_id?: string;
  conversation_id?: string;
  run_id?: string;
  iteration?: number;
  stopped_reason?: string | null;
  message?: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads
// ──────────────────────────────────────────────────────────────────

export interface EnvListCommand {
  type: "env.list";
}

export interface EnvSetCommand {
  type: "env.set";
  name: string;
  value: string;
  description?: string;
}

export interface EnvDeleteCommand {
  type: "env.delete";
  name: string;
}

export interface RunCheckpointListCommand {
  type: "checkpoint.run.list";
  session_id?: string;
  conversation_id?: string;
}
