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
  | "workspace.recent.remove"
  | "workspace.recent.clear"
  | "env.list"
  | "env.set"
  | "env.delete"
  | "git.pr_status"
  | "git.pr_automation.set"
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

interface WorkspaceOwnedEvent {
  conversation_id: string;
  workspace_root: string;
  request_id?: string;
}

export interface FileChangedEvent extends WorkspaceOwnedEvent {
  type: "file.changed";
  path: string;
  event: string;
  timestamp?: string;
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

export interface GitPrStatusEvent extends WorkspaceOwnedEvent {
  type: "git.pr_status";
  pr?: { number: number; title: string; state: string; url: string; branch: string } | null;
  checks?: { name: string; status: string; url: string }[];
  error?: string;
  automation?: { auto_fix?: boolean; auto_merge?: boolean };
}

export interface GitDiffFilePayload {
  path: string;
  patch: string;
  additions: number;
  deletions: number;
  is_binary?: boolean;
  is_truncated?: boolean;
}

export interface GitDiffWorkingTreeEvent extends WorkspaceOwnedEvent {
  type: "diff.git_working_tree";
  files?: GitDiffFilePayload[];
  untracked?: string[];
  preview?: boolean;
  tool_call_id?: string;
  progress?: number;
}

export interface GitDiffStagedEvent extends WorkspaceOwnedEvent {
  type: "diff.git_staged";
  files?: GitDiffFilePayload[];
}

export interface GitDiffActionEvent extends WorkspaceOwnedEvent {
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

export interface WorkspaceRecentProjectPayload {
  path: string;
  name: string;
  project_type: string;
  last_opened: number;
}

export interface WorkspaceRecentListEvent {
  type: "workspace.recent.list";
  projects: WorkspaceRecentProjectPayload[];
}

export interface WorkspaceProjectPayload {
  root_path: string;
  project_type: string;
  name: string;
  description: string;
  file_count: number;
  total_size: number;
  has_project_instructions: boolean;
  index_truncated: boolean;
}

export interface WorkspaceImportedEvent extends WorkspaceOwnedEvent {
  type: "workspace.imported";
  project: WorkspaceProjectPayload;
  summary: string;
  file_count: number;
}

export interface CheckpointRecordPayload {
  id: string;
  conversation_id: string;
  session_id: string;
  tool_call_id: string;
  tool_name: string;
  workspace_root: string;
  paths: string[];
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface CheckpointCreatedEvent extends CheckpointRecordPayload {
  type: "checkpoint.created";
}

export interface CheckpointListEvent extends WorkspaceOwnedEvent {
  type: "checkpoint.list";
  checkpoints: CheckpointRecordPayload[];
}

export interface CheckpointRewoundEvent extends WorkspaceOwnedEvent {
  type: "checkpoint.rewound";
  checkpoint: CheckpointRecordPayload;
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
  session_id: string;
  conversation_id: string;
  workspace_root: string;
  checkpoints: RunCheckpointRecord[];
  runs: Record<string, unknown>[];
  subagents: Record<string, unknown>[];
}

export interface RunCheckpointResumeEvent {
  type: "checkpoint.run.resume";
  resumed: boolean;
  session_id?: string;
  conversation_id: string;
  workspace_root: string;
  run_id?: string;
  iteration?: number;
  stopped_reason?: string | null;
  message?: string;
}

export interface GuidelinesUpdatedEvent extends WorkspaceOwnedEvent {
  type: "guidelines.updated";
  message: string;
  path?: string;
  cache_cleared?: boolean;
  effective_from?: "next_turn" | string;
  source_kind?: "direct" | "import" | string;
  parent_path?: string;
}

export interface PermissionModeUpdatedEvent {
  type: "permission.mode.updated";
  session_id: string;
  mode: string;
  source: string;
}

export interface PermissionRulePayload {
  pattern?: string;
  source: string;
  level?: string;
  tool?: string;
  rule_content?: string;
  behavior?: string;
  destination?: string;
}

export interface PermissionRulesPayload {
  mode: string;
  context_source: string;
  system_deny: PermissionRulePayload[];
  session_deny: PermissionRulePayload[];
  session_overrides: PermissionRulePayload[];
  session_prompt_rules: PermissionRulePayload[];
}

export interface PermissionRulesUpdatedEvent {
  type: "permission.rules.updated";
  session_id: string;
  conversation_id: string;
  source: string;
  rules: PermissionRulesPayload;
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

export interface ArtifactContentEvent extends WorkspaceOwnedEvent {
  type: "artifact_content";
  artifact_id: string;
  request_id: string;
  content?: string;
  preview?: string;
  media_type?: string;
  url?: string;
  purpose?: string;
  name?: string;
  is_attachment?: boolean;
}

export interface GitPrAutomationSetCommand {
  type: "git.pr_automation.set";
  auto_fix?: boolean;
  auto_merge?: boolean;
}

interface CheckpointOwnedCommand {
  conversation_id: string;
  workspace_root: string;
}

export interface CheckpointListCommand extends CheckpointOwnedCommand {
  type: "checkpoint.list";
  limit?: number;
}

export interface CheckpointRewindCommand extends CheckpointOwnedCommand {
  type: "checkpoint.rewind";
  checkpoint_id: string;
}

export interface RunCheckpointListCommand extends CheckpointOwnedCommand {
  type: "checkpoint.run.list";
}
