/**
 * Preview server event types.
 *
 * Domain subset of the WebSocket protocol for events related to the built-in
 * preview server system: server detection, launch configuration, process
 * lifecycle, health monitoring, navigation, and verification.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

// ──────────────────────────────────────────────────────────────────
// Server event type strings (preview domain)
// ──────────────────────────────────────────────────────────────────

export type PreviewServerEventType =
  | "preview.servers.updated"
  | "preview.server.detected"
  | "preview.server.stopped"
  | "preview.navigated"
  | "preview.refreshed"
  | "preview.launch.config"
  | "preview.launch.started"
  | "preview.launch.stopped"
  | "preview.server.ready"
  | "preview.server.output"
  | "preview.server.crashed"
  | "preview.server.unhealthy"
  | "preview.verified";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (preview domain)
// ──────────────────────────────────────────────────────────────────

export type PreviewClientCommandType =
  | "preview.detect"
  | "preview.navigate"
  | "preview.refresh"
  | "preview.launch.config"
  | "preview.launch.start"
  | "preview.launch.stop"
  | "preview.verify";

// ──────────────────────────────────────────────────────────────────
// Shared info types
// ──────────────────────────────────────────────────────────────────

export interface PreviewServerInfo {
  port: number;
  url: string;
  name: string;
  framework?: string;
}

export interface PreviewLaunchConfigInfo {
  name: string;
  command: string;
  cwd: string;
  port: number;
  url: string;
  auto_port?: boolean;
  source?: string;
}

export interface PreviewServerOutputLine {
  stream: "stdout" | "stderr";
  line: string;
  timestamp?: number;
}

export interface PreviewLaunchProcessInfo extends PreviewLaunchConfigInfo {
  id: string;
  pid?: number;
  status: "starting" | "running" | "ready" | "exited" | "crashed" | "unhealthy";
  stderr_tail?: string[];
  output_tail?: PreviewServerOutputLine[];
}

interface PreviewOwnedEvent {
  conversation_id: string;
  workspace_root: string;
  request_id?: string;
}

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface PreviewServersUpdatedEvent extends PreviewOwnedEvent {
  type: "preview.servers.updated";
  servers: PreviewServerInfo[];
}

export interface PreviewServerDetectedEvent extends PreviewServerInfo, PreviewOwnedEvent {
  type: "preview.server.detected";
}

export interface PreviewServerStoppedEvent extends PreviewOwnedEvent {
  type: "preview.server.stopped";
  port: number;
}

export interface PreviewNavigatedEvent extends PreviewOwnedEvent {
  type: "preview.navigated";
  url: string;
}

export interface PreviewRefreshedEvent extends PreviewOwnedEvent {
  type: "preview.refreshed";
  url?: string;
  path?: string;
}

export interface PreviewLaunchConfigEvent extends PreviewOwnedEvent {
  type: "preview.launch.config";
  configs: PreviewLaunchConfigInfo[];
  running?: PreviewLaunchProcessInfo[];
}

export interface PreviewLaunchStartedEvent extends PreviewLaunchProcessInfo, PreviewOwnedEvent {
  type: "preview.launch.started";
}

export interface PreviewLaunchStoppedEvent extends PreviewLaunchProcessInfo, PreviewOwnedEvent {
  type: "preview.launch.stopped";
}

export interface PreviewServerReadyEvent extends PreviewOwnedEvent {
  type: "preview.server.ready";
  id: string;
  url: string;
  port: number;
}

export interface PreviewServerOutputEvent extends PreviewServerOutputLine, PreviewOwnedEvent {
  type: "preview.server.output";
  id: string;
}

export interface PreviewServerCrashedEvent extends PreviewOwnedEvent {
  type: "preview.server.crashed";
  id: string;
  exit_code?: number | null;
  stderr_tail?: string[];
}

export interface PreviewServerUnhealthyEvent extends PreviewOwnedEvent {
  type: "preview.server.unhealthy";
  id: string;
  url?: string;
  consecutive_failures?: number;
  last_error?: string;
}

export interface PreviewVerifiedEvent extends PreviewOwnedEvent {
  type: "preview.verified";
  url: string;
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (preview domain)
// ──────────────────────────────────────────────────────────────────

interface PreviewOwnedCommand {
  conversation_id?: string;
  workspace_root?: string;
  request_id?: string;
}

export interface PreviewDetectCommand extends PreviewOwnedCommand {
  type: "preview.detect";
}

export interface PreviewNavigateCommand extends PreviewOwnedCommand {
  type: "preview.navigate";
  url: string;
}

export interface PreviewRefreshCommand extends PreviewOwnedCommand {
  type: "preview.refresh";
  url?: string;
}

export interface PreviewLaunchConfigCommand extends PreviewOwnedCommand {
  type: "preview.launch.config";
  workspace_root?: string;
}

export interface PreviewLaunchStartCommand extends PreviewOwnedCommand {
  type: "preview.launch.start";
  name?: string;
  workspace_root?: string;
}

export interface PreviewLaunchStopCommand extends PreviewOwnedCommand {
  type: "preview.launch.stop";
  name?: string;
}

export interface PreviewVerifyCommand extends PreviewOwnedCommand {
  type: "preview.verify";
  url: string;
}
