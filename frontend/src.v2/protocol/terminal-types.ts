/**
 * Terminal event types.
 *
 * Domain subset of the WebSocket protocol for events related to terminal
 * session management: output streaming, process lifecycle, PTY creation,
 * and background command completion.
 *
 * Keep in lockstep with backend/ws/events.py.
 */

// ──────────────────────────────────────────────────────────────────
// Server event type strings (terminal domain)
// ──────────────────────────────────────────────────────────────────

export type TerminalServerEventType =
  | "terminal.output"
  | "terminal.exit"
  | "terminal.created"
  | "terminal.killed"
  | "terminal.list"
  | "terminal.snapshot"
  | "terminal.resized"
  | "background.completed";

// ──────────────────────────────────────────────────────────────────
// Client command type strings (terminal domain)
// ──────────────────────────────────────────────────────────────────

export type TerminalClientCommandType =
  | "terminal.create"
  | "terminal.input"
  | "terminal.resize"
  | "terminal.kill"
  | "terminal.list"
  | "terminal.snapshot.request"
  | "terminal.mirror.created"
  | "terminal.mirror.output"
  | "terminal.mirror.exit"
  | "terminal.exec";

// ──────────────────────────────────────────────────────────────────
// Server event payload types
// ──────────────────────────────────────────────────────────────────

export interface TerminalOutputEvent {
  type: "terminal.output";
  session_id?: string;
  data?: string;
  command?: string;
  output?: string;
  exit_code?: number;
}

export interface TerminalExitEvent {
  type: "terminal.exit";
  session_id: string;
  exit_code: number;
}

export interface TerminalCreatedEvent {
  type: "terminal.created";
  session_id: string;
  pid?: number;
  shell?: string;
  cwd?: string;
}

export interface TerminalKilledEvent {
  type: "terminal.killed";
  session_id: string;
}

export interface TerminalListEvent {
  type: "terminal.list";
  sessions: {
    session_id?: string;
    pid?: number;
    shell?: string;
    cwd?: string;
    is_alive?: boolean;
    started_at?: number;
  }[];
}

export interface TerminalSnapshotEvent {
  type: "terminal.snapshot";
  session_id: string;
  pid?: number | null;
  shell?: string;
  cwd?: string;
  started_at?: number;
  is_alive?: boolean;
  output: string;
  output_chars?: number;
  total_output_chars?: number;
  truncated?: boolean;
  error?: string;
}

export interface TerminalResizedEvent {
  type: "terminal.resized";
  session_id: string;
  cols: number;
  rows: number;
  applied?: boolean;
}

export interface BackgroundCompletedEvent {
  type: "background.completed";
  command_id: string;
  exit_code: number;
  status: string;
}

// ──────────────────────────────────────────────────────────────────
// Client command payloads (terminal domain)
// ──────────────────────────────────────────────────────────────────

export interface TerminalCreateCommand {
  type: "terminal.create";
  cwd?: string;
}

export interface TerminalInputCommand {
  type: "terminal.input";
  session_id: string;
  data: string;
}

export interface TerminalResizeCommand {
  type: "terminal.resize";
  session_id: string;
  cols: number;
  rows: number;
}

export interface TerminalKillCommand {
  type: "terminal.kill";
  session_id: string;
}

export interface TerminalSnapshotRequestCommand {
  type: "terminal.snapshot.request";
  session_id?: string;
  max_chars?: number;
}

export interface TerminalMirrorCreatedCommand {
  type: "terminal.mirror.created";
  session_id: string;
  pid?: number;
  shell?: string;
  cwd?: string;
}

export interface TerminalMirrorOutputCommand {
  type: "terminal.mirror.output";
  session_id: string;
  data: string;
  pid?: number;
  shell?: string;
  cwd?: string;
}

export interface TerminalMirrorExitCommand {
  type: "terminal.mirror.exit";
  session_id: string;
  exit_code?: number;
}

export interface TerminalExecCommand {
  type: "terminal.exec";
  command: string;
  cwd?: string;
}
