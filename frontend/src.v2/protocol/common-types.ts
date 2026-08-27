/**
 * Common / shared base types — session, control plane, skills, MCP,
 * scheduler, connectors marketplace, and miscellaneous commands.
 *
 * Domain subset of the WebSocket protocol for infrastructure-level events
 * that do not belong to a specific feature domain: session management,
 * heartbeat/control plane, skill lifecycle, MCP status, scheduler tasks,
 * connectors marketplace, and catalog listing commands.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

// ──────────────────────────────────────────────────────────────────
// Server event type strings (common / infrastructure domain)
// ──────────────────────────────────────────────────────────────────

import type { RuntimeSessionSnapshot } from "./streaming-types";
import type { AgentCapabilitiesPayload } from "./capabilities";
import type {
  ConversationRecordPayload,
  ConversationTranscriptMessage,
} from "./conversation-types";

export type CommonServerEventType =
  // MCP
  | "mcp_status"
  | "mcp.lifecycle"
  | "mcp.progress"
  // Scheduler
  | "scheduler.list"
  // Session / control plane
  | "session.restored"
  | "session.replay"
  | "session.synced"
  | "runtime.capabilities"
  | "client.command.ack"
  | "pong"
  | "control_request"
  // Pi provider OAuth callbacks projected by the session owner
  | "llm.provider.oauth.auth"
  | "llm.provider.oauth.device_code"
  | "llm.provider.oauth.info"
  | "llm.provider.oauth.progress"
  // UI catalogs / notices
  | "skills.list"
  | "skills.marketplace.list"
  | "commands.list"
  | "system_notice";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (common / infrastructure domain)
// ──────────────────────────────────────────────────────────────────

export type CommonClientCommandType =
  // Control plane
  | "control_response"
  | "control_cancel_request"
  | "llm.provider.oauth.login"
  | "llm.provider.oauth.logout"
  | "llm.provider.oauth.status"
  // Skills
  | "skills.list"
  | "skills.install"
  | "skills.marketplace.list"
  // Commands catalog
  | "commands.list"
  // MCP Connectors
  | "mcp.list"
  | "mcp.inventory.list"
  | "mcp.inventory.cancel"
  | "mcp.add"
  | "mcp.update"
  | "mcp.toggle"
  | "mcp.remove"
  | "mcp.restart"
  | "mcp.oauth.login"
  | "mcp.oauth.logout"
  | "mcp.project.approve"
  | "mcp.project.approve_all"
  | "mcp.project.reject"
  // Scheduler
  | "scheduler.list"
  | "scheduler.add"
  | "scheduler.remove"
  | "scheduler.toggle"
  | "scheduler.run_now"
  | "scheduler.retry"
  | "scheduler.cancel"
  // Session inspection
  | "session.tasks.inspect"
  | "session.status.inspect"
  | "session.usage.inspect"
  | "session.permissions.inspect"
  | "runtime.capabilities.inspect"
  // Session restore / sync
  | "session.restore"
  | "session.sync"
  // Frontend UI
  | "workspace.set";

export type McpTransport = "stdio" | "sse" | "http" | "ws";

export type McpEnvVarReference = string | { name: string; source: string };

interface McpServerMutationCommon {
  name: string;
  auto_start: boolean;
  startup_timeout_sec?: number;
  tool_timeout_sec?: number;
  required?: boolean;
  supports_parallel_tool_calls?: boolean;
  enabled_tools?: string[];
  disabled_tools?: string[];
  default_tools_approval_mode?: "auto" | "prompt" | "writes" | "approve";
  tools?: Record<string, { approval_mode?: "auto" | "prompt" | "writes" | "approve" }>;
}

export type McpServerMutationPayload = McpServerMutationCommon & (
  | {
      transport: "stdio";
      command: string;
      args?: string[];
      env?: Record<string, string>;
      env_vars?: McpEnvVarReference[];
      cwd?: string;
      url?: never;
      headers?: never;
      headers_helper?: never;
      oauth?: never;
    }
  | {
      transport: "sse" | "http";
      url: string;
      headers?: Record<string, string>;
      headers_helper?: string;
      oauth?: { client_id?: string; callback_port?: number };
      command?: never;
      args?: never;
      env?: never;
      env_vars?: never;
      cwd?: never;
    }
  | {
      transport: "ws";
      url: string;
      headers?: Record<string, string>;
      headers_helper?: string;
      oauth?: never;
      command?: never;
      args?: never;
      env?: never;
      env_vars?: never;
      cwd?: never;
    }
);

export type McpAddCommand = { type: "mcp.add" } & McpServerMutationPayload;

export type McpUpdateCommand = {
  type: "mcp.update";
  original_name: string;
} & McpServerMutationPayload;

export interface McpInventoryListCommand {
  type: "mcp.inventory.list";
  name: string;
  operation_id: string;
}

export interface McpInventoryCancelCommand {
  type: "mcp.inventory.cancel";
  name: string;
  operation_id: string;
}

export interface McpInventoryResource {
  uri: string;
  name: string;
  description?: string;
  mime_type?: string;
}

export interface McpInventoryResourceTemplate {
  uri_template: string;
  name: string;
  description?: string;
  mime_type?: string;
}

export interface McpInventoryPromptArgument {
  name: string;
  description?: string;
  required?: boolean;
}

export interface McpInventoryPrompt {
  name: string;
  description?: string;
  arguments?: McpInventoryPromptArgument[];
}

export interface McpInventoryPayload {
  server_name: string;
  capabilities: {
    resources: boolean;
    resources_subscribe: boolean;
    resources_list_changed: boolean;
    prompts: boolean;
  };
  resources: McpInventoryResource[];
  resource_templates: McpInventoryResourceTemplate[];
  prompts: McpInventoryPrompt[];
  empty: boolean;
}

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface McpStatusEvent {
  type: "mcp_status";
  servers?: {
    name: string;
    status: string;
    tools?: number;
    capabilities?: {
      tools?: boolean;
      resources?: boolean;
      resources_subscribe?: boolean;
      resources_list_changed?: boolean;
      prompts?: boolean;
      logging?: boolean;
    };
    tools_count?: number;
    transport?: McpTransport;
    command?: string;
    args?: string[];
    env?: Record<string, string>;
    headers?: Record<string, string>;
    headers_helper?: string;
    oauth?: { client_id?: string; callback_port?: number };
    env_vars?: Array<string | { name?: string; source?: string }>;
    cwd?: string;
    url?: string;
    auto_start?: boolean;
    editable?: boolean;
    enabled?: boolean;
    disabled_reason?: string;
    error?: string;
    source?: string;
    approval_status?: "approved" | "rejected" | "pending" | "not_applicable";
    config_path?: string;
    project_workspace?: string;
    priority?: number;
    auth_status?: "unsupported" | "not_logged_in" | "oauth";
    phase?: string;
    recoverable?: boolean;
    requires_user_action?: boolean;
    setup_hint?: string;
    docs_url?: string;
    required?: boolean;
    supports_parallel_tool_calls?: boolean;
    enabled_tools?: string[] | null;
    disabled_tools?: string[];
    default_tools_approval_mode?: "auto" | "prompt" | "writes" | "approve" | null;
    cleanup?: {
      pending: boolean;
      reason: string;
      requested_at?: number | null;
      completed_at?: number | null;
    };
    operation_failures?: Array<{
      operation: string;
      failure_kind: string;
      message: string;
      retryable: boolean;
    }>;
  }[];
  data?: unknown;
}

export type McpLifecyclePhase =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "auth_required"
  | "expired"
  | "failed"
  | "stopped";

export interface McpLifecycleEvent {
  type: "mcp.lifecycle";
  server_name: string;
  status?: string;
  phase: McpLifecyclePhase;
  message?: string;
  auth_status?: "unsupported" | "not_logged_in" | "oauth";
  recoverable?: boolean;
  requires_user_action?: boolean;
  setup_hint?: string;
  docs_url?: string;
}

export interface McpProgressEvent {
  type: "mcp.progress";
  server_name: string;
  operation: string;
  message?: string;
  progress?: number; // optional 0-1
  status: "running" | "completed" | "failed";
}

export interface SchedulerListEvent {
  type: "scheduler.list";
  conversation_id: string;
  workspace_root: string;
  request_id?: string;
  tasks?: { id: string; name: string; prompt: string; schedule: string; timezone?: string; isolation?: "worktree" | "workspace"; conversation_id?: string; permission_mode: string; enabled: boolean; last_run_at?: string | null; next_run_at?: string | null; created_at?: string; workspace_root?: string; last_run_id?: string | null; last_run_status?: string | null; last_error?: string | null }[];
  runs?: { id: string; task_id: string; scheduled_at: string; started_at?: string; finished_at?: string | null; status: string; conversation_id?: string; workspace_root?: string; result_summary?: string; error?: string }[];
}

export interface SkillsListEvent {
  type: "skills.list";
  skills: {
    name: string;
    description: string;
    path?: string;
    display_name?: string;
    short_description?: string;
    icon?: string;
    icon_large?: string;
    brand_color?: string;
    version?: string;
    mcp_dependencies?: string[];
    allow_implicit_invocation?: boolean;
    user_invocable?: boolean;
    default_prompt?: string;
    source_level?: string;
    active?: boolean;
  }[];
}

export interface SkillsMarketplaceListEvent {
  type: "skills.marketplace.list";
  skills: {
    name: string;
    title: string;
    description: string;
    triggers: string[];
    installed: boolean;
  }[];
}

export interface CommandAvailabilityPayload {
  kind: string;
  scope: string;
  reason?: string;
}

export interface CommandArgumentPayload {
  value: string;
  description: string;
}

export interface CommandCatalogEntryPayload {
  id?: string;
  name: string;
  command: string;
  label: string;
  description: string;
  type: "local" | "template" | "protocol";
  kind?: string;
  source: string;
  enabled: boolean;
  availability: CommandAvailabilityPayload;
  panel?: string;
  args?: CommandArgumentPayload[];
  extension_path?: string;
  source_path?: string;
  template?: string;
  search_text?: string;
  argument_hint?: string;
  argument_names?: string[];
  base_dir?: string;
  is_skill_file?: boolean;
}

export interface CommandsListEvent {
  type: "commands.list";
  /** Null is the explicit session catalog used before a conversation exists. */
  conversation_id: string | null;
  request_id?: string;
  commands: CommandCatalogEntryPayload[];
}

export interface CheckpointOriginPayload {
  run_id: string;
  conversation_id: string;
  session_id: string;
  sequence: number;
  timestamp: number;
  stopped_reason: string;
}

export type SystemNoticeEvent = {
  type: "system_notice";
  conversation_id: string;
  data?: Record<string, unknown>;
  checkpoint_origin?: CheckpointOriginPayload;
} & (
  | {
      content: string;
      title?: string;
      message?: string;
    }
  | {
      content?: string;
      title: string;
      message: string;
    }
);

export interface PongEvent {
  type: "pong";
}

export interface ClientCommandAckEvent {
  type: "client.command.ack";
  client_command_id: string;
  command_type: string;
  duplicate?: boolean;
  accepted?: boolean;
  reason?: string;
}

export interface RuntimeCapabilitiesEvent {
  type: "runtime.capabilities";
  session_id?: string;
  source?: string;
  capabilities: AgentCapabilitiesPayload;
}

export interface ProviderOAuthInfoLink {
  url: string;
  label?: string;
}

export interface ProviderOAuthAuthEvent {
  type: "llm.provider.oauth.auth";
  conversation_id: string;
  provider: string;
  url: string;
  instructions?: string;
}

export interface ProviderOAuthDeviceCodeEvent {
  type: "llm.provider.oauth.device_code";
  conversation_id: string;
  provider: string;
  userCode: string;
  verificationUri: string;
  intervalSeconds?: number;
  expiresInSeconds?: number;
}

export interface ProviderOAuthInfoEvent {
  type: "llm.provider.oauth.info";
  conversation_id: string;
  provider: string;
  message: string;
  links?: ProviderOAuthInfoLink[];
}

export interface ProviderOAuthProgressEvent {
  type: "llm.provider.oauth.progress";
  conversation_id: string;
  provider: string;
  message: string;
}

export interface ControlCanUseToolRequest {
  subtype: "can_use_tool";
  tool_name: string;
  input: Record<string, unknown>;
  tool_use_id: string;
  diff?: string | Record<string, unknown>;
  source_agent?: string;
  source_thread?: string;
  source_tool?: string;
}

export interface ControlElicitationRequest {
  subtype: "elicitation";
  tool_use_id: string;
  prompt: string;
  question: string;
  schema?: Record<string, unknown>;
  options?: unknown[];
  choices?: unknown[];
  allowed_values?: unknown[];
}

export interface ControlProviderAuthPromptRequest {
  subtype: "provider_auth_prompt";
  prompt: string;
  provider: string;
  prompt_type: "text" | "secret" | "select" | "manual_code";
  placeholder?: string;
  allow_empty: boolean;
  allow_custom: boolean;
  options?: Array<{
    id: string;
    label: string;
    description?: string;
  }>;
}

export type ControlRequestPayload =
  | ControlCanUseToolRequest
  | ControlElicitationRequest
  | ControlProviderAuthPromptRequest;

export interface ControlRequestEvent {
  type: "control_request";
  request_id: string;
  conversation_id: string;
  request: ControlRequestPayload;
  turn_id?: string;
  message_id?: string;
  workspace_root?: string;
  permission_mode?: string;
  workspace_scope?: string;
  timeout_seconds?: number;
  expires_at?: number;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (common / infrastructure domain)
// ──────────────────────────────────────────────────────────────────

export type SessionSnapshotPayload = RuntimeSessionSnapshot & {
  workspace_root?: string | null;
  active_conversation?: ConversationRecordPayload | null;
  parent_session_id?: string | null;
  invoked_skill_names?: string[];
};

export interface SessionWorkspacePayload {
  root_path?: string | null;
  name?: string | null;
}

export interface SessionRestoredEvent {
  type: "session.restored";
  session_id?: string;
  restored?: boolean;
  active_conversation_id?: string | null;
  conversation_switched_follows?: boolean;
  conversation?: ConversationRecordPayload | null;
  active_conversation?: ConversationRecordPayload | null;
  workspace?: SessionWorkspacePayload | null;
  working_directory?: string | null;
  workspace_root?: string | null;
  model?: string | null;
  current_model?: string | null;
  provider?: string | null;
  provider_id?: string | null;
  base_url?: string | null;
  wire_api?: string | null;
  available_models?: string[];
  models_source?: string;
  messages?: ConversationTranscriptMessage[];
  error?: string | null;
  missed_events?: boolean;
  event_log_gap?: boolean;
  snapshot_required?: boolean;
  cursor_reset?: boolean;
  requested_last_seq?: number;
  last_seq?: number;
  current_seq?: number;
  replayed_events?: number;
  session?: SessionSnapshotPayload | null;
  snapshot_at?: string;
}

export interface SessionReplayEvent {
  type: "session.replay";
  last_seq: number;
  current_seq: number;
  replayed_events: number;
  events: Array<Record<string, unknown>>;
}

export interface SessionSyncedEvent {
  type: "session.synced";
  session_id?: string;
  synced?: boolean;
  protocol_version?: string;
  active_conversation_id?: string | null;
  active_conversation?: ConversationRecordPayload | null;
  working_directory?: string | null;
  workspace_root?: string | null;
  workspace?: SessionWorkspacePayload | null;
  model?: string | null;
  current_model?: string | null;
  provider?: string | null;
  provider_id?: string | null;
  base_url?: string | null;
  wire_api?: string | null;
  available_models?: string[];
  models_source?: string;
  missed_events?: boolean;
  event_log_gap?: boolean;
  snapshot_required?: boolean;
  cursor_reset?: boolean;
  requested_last_seq?: number;
  last_seq?: number;
  current_seq?: number;
  replayed_events?: number;
  session?: SessionSnapshotPayload | null;
  snapshot_at?: string;
}

export interface ControlResponseCommand {
  type: "control_response";
  request_id: string;
  conversation_id?: string;
  turn_id?: string;
  message_id?: string;
  response: {
    subtype: "success";
    response: Record<string, unknown>;
  };
}

export interface ControlCancelRequestCommand {
  type: "control_cancel_request";
  request_id: string;
  conversation_id?: string;
  turn_id?: string;
  message_id?: string;
}

export interface SkillsListCommand {
  type: "skills.list";
}

export interface SkillsMarketplaceListCommand {
  type: "skills.marketplace.list";
}

export interface SkillsInstallCommand {
  type: "skills.install";
  name: string;
}

interface WorkspaceOwnedCommand {
  owner_conversation_id?: string;
  conversation_id?: string;
  workspace_root?: string;
}

export interface McpProjectDecisionCommand {
  type: "mcp.project.approve" | "mcp.project.approve_all" | "mcp.project.reject";
  name: string;
  conversation_id: string;
  workspace_root: string;
}

export interface SchedulerListCommand extends WorkspaceOwnedCommand {
  type: "scheduler.list";
}

export interface SchedulerAddCommand extends WorkspaceOwnedCommand {
  type: "scheduler.add";
  name: string;
  prompt: string;
  schedule: string;
  timezone?: string;
  isolation?: "worktree" | "workspace";
  permission_mode?: "confirm" | "auto";
}

export interface SchedulerRemoveCommand extends WorkspaceOwnedCommand {
  type: "scheduler.remove";
  task_id: string;
}

export interface SchedulerToggleCommand extends WorkspaceOwnedCommand {
  type: "scheduler.toggle";
  task_id: string;
  enabled: boolean;
}

export interface SchedulerRunNowCommand extends WorkspaceOwnedCommand {
  type: "scheduler.run_now";
  task_id: string;
}

export interface SchedulerRetryCommand extends WorkspaceOwnedCommand {
  type: "scheduler.retry";
  run_id: string;
}

export interface SchedulerCancelCommand extends WorkspaceOwnedCommand {
  type: "scheduler.cancel";
  run_id: string;
}

export interface RuntimeCapabilitiesInspectCommand {
  type: "runtime.capabilities.inspect";
  source?: string;
}
