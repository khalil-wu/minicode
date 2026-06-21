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
  // Skills
  | "skill_activated"
  | "skill_deactivated"
  // MCP
  | "mcp_status"
  | "mcp.lifecycle"
  | "mcp.progress"
  // Scheduler
  | "scheduler.list"
  // Connectors marketplace
  | "connectors.marketplace.list"
  // Session / control plane
  | "session.restored"
  | "session.synced"
  | "runtime.capabilities"
  | "client.command.ack"
  | "pong"
  | "control_request"
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
  // Skills
  | "load_skill"
  | "unload_skill"
  | "skills.list"
  | "skills.install"
  | "skills.marketplace.list"
  // Commands catalog
  | "commands.list"
  // MCP Connectors
  | "mcp.list"
  | "mcp.add"
  | "mcp.remove"
  | "mcp.restart"
  // Scheduler
  | "scheduler.list"
  | "scheduler.add"
  | "scheduler.remove"
  | "scheduler.toggle"
  // Connectors marketplace
  | "connectors.marketplace.list"
  | "connectors.marketplace.install"
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
  | "workspace.set"
  // Task management
  | "task.stop";

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface SkillActivatedEvent {
  type: "skill_activated";
  skill_name: string;
}

export interface SkillDeactivatedEvent {
  type: "skill_deactivated";
  skill_name: string;
}

export interface McpStatusEvent {
  type: "mcp_status";
  servers?: {
    name: string;
    status: string;
    tools?: number;
    tools_count?: number;
    transport?: string;
    error?: string;
    source?: string;
    priority?: number;
    phase?: string;
    recoverable?: boolean;
    requires_user_action?: boolean;
    setup_hint?: string;
    docs_url?: string;
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
  tasks?: { id: string; name: string; prompt: string; schedule: string; permission_mode: string; enabled: boolean; last_run_at?: string | null; created_at?: string }[];
}

export interface ConnectorsMarketplaceListEvent {
  type: "connectors.marketplace.list";
  connectors?: {
    name: string;
    title: string;
    description: string;
    transport: string;
    command?: string;
    args?: string[];
    url?: string;
    tags?: string[];
    installed: boolean;
    auth?: string;
    requiresUserAction?: boolean;
    setupHint?: string;
    docsUrl?: string;
    autoStart?: boolean;
    maxRetries?: number;
  }[];
}

export interface SkillsListEvent {
  type: "skills.list";
  skills: {
    name: string;
    description: string;
    version?: string;
    triggers?: string[];
    tools_required?: string[];
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

export interface ClientCommandAckEvent {
  type: "client.command.ack";
  client_command_id: string;
  command_type: string;
}

export interface RuntimeCapabilitiesEvent {
  type: "runtime.capabilities";
  session_id?: string;
  source?: string;
  capabilities: AgentCapabilitiesPayload;
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
  available_models?: string[];
  messages?: ConversationTranscriptMessage[];
  error?: string | null;
  session?: SessionSnapshotPayload | null;
}

export interface SessionSyncedEvent {
  type: "session.synced";
  session_id?: string;
  synced?: boolean;
  incremental?: boolean;
  changes?: unknown[];
  active_conversation_id?: string | null;
  active_conversation?: ConversationRecordPayload | null;
  working_directory?: string | null;
  workspace_root?: string | null;
  workspace?: SessionWorkspacePayload | null;
  model?: string | null;
  current_model?: string | null;
  provider?: string | null;
  available_models?: string[];
  session?: SessionSnapshotPayload | null;
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

export interface SchedulerListCommand {
  type: "scheduler.list";
}

export interface SchedulerAddCommand {
  type: "scheduler.add";
  name: string;
  prompt: string;
  schedule: string;
}

export interface SchedulerRemoveCommand {
  type: "scheduler.remove";
  task_id: string;
}

export interface SchedulerToggleCommand {
  type: "scheduler.toggle";
  task_id: string;
  enabled: boolean;
}

export interface ConnectorsMarketplaceListCommand {
  type: "connectors.marketplace.list";
}

export interface ConnectorsMarketplaceInstallCommand {
  type: "connectors.marketplace.install";
  name: string;
}

export interface RuntimeCapabilitiesInspectCommand {
  type: "runtime.capabilities.inspect";
  source?: string;
}
