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
  status: "starting" | "running" | "ready" | "exited" | "crashed";
  stderr_tail?: string[];
  output_tail?: PreviewServerOutputLine[];
}

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface PreviewServersUpdatedEvent {
  type: "preview.servers.updated";
  servers: PreviewServerInfo[];
}

export interface PreviewServerDetectedEvent extends PreviewServerInfo {
  type: "preview.server.detected";
}

export interface PreviewServerStoppedEvent {
  type: "preview.server.stopped";
  port: number;
}

export interface PreviewNavigatedEvent {
  type: "preview.navigated";
  url: string;
}

export interface PreviewRefreshedEvent {
  type: "preview.refreshed";
  url?: string;
}

export interface PreviewLaunchConfigEvent {
  type: "preview.launch.config";
  workspace_root?: string;
  configs: PreviewLaunchConfigInfo[];
  running?: PreviewLaunchProcessInfo[];
}

export interface PreviewLaunchStartedEvent extends PreviewLaunchProcessInfo {
  type: "preview.launch.started";
}

export interface PreviewLaunchStoppedEvent extends PreviewLaunchProcessInfo {
  type: "preview.launch.stopped";
}

export interface PreviewServerReadyEvent {
  type: "preview.server.ready";
  id: string;
  url: string;
  port: number;
}

export interface PreviewServerOutputEvent extends PreviewServerOutputLine {
  type: "preview.server.output";
  id: string;
}

export interface PreviewServerCrashedEvent {
  type: "preview.server.crashed";
  id: string;
  exit_code?: number | null;
  stderr_tail?: string[];
}

export interface PreviewServerUnhealthyEvent {
  type: "preview.server.unhealthy";
  id: string;
  url?: string;
  consecutive_failures?: number;
  last_error?: string;
}

export interface PreviewVerifiedEvent {
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

export interface PreviewDetectCommand {
  type: "preview.detect";
}

export interface PreviewNavigateCommand {
  type: "preview.navigate";
  url: string;
}

export interface PreviewRefreshCommand {
  type: "preview.refresh";
  url?: string;
}

export interface PreviewLaunchConfigCommand {
  type: "preview.launch.config";
  workspace_root?: string;
}

export interface PreviewLaunchStartCommand {
  type: "preview.launch.start";
  name?: string;
  workspace_root?: string;
}

export interface PreviewLaunchStopCommand {
  type: "preview.launch.stop";
  name?: string;
}

export interface PreviewVerifyCommand {
  type: "preview.verify";
  url: string;
}
