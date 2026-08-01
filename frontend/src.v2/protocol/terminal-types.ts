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
  | "background.started"
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
  conversation_id: string;
  session_id?: string;
  data?: string;
  command?: string;
  output?: string;
  exit_code?: number;
}

export interface TerminalExitEvent {
  type: "terminal.exit";
  conversation_id: string;
  session_id: string;
  exit_code: number;
}

export interface TerminalCreatedEvent {
  type: "terminal.created";
  conversation_id: string;
  session_id: string;
  pid?: number;
  shell?: string;
  cwd?: string;
  terminal_mode?: "pty" | "pipe";
}

export interface TerminalKilledEvent {
  type: "terminal.killed";
  conversation_id: string;
  session_id: string;
}

export interface TerminalListEvent {
  type: "terminal.list";
  conversation_id: string;
  sessions: {
    session_id?: string;
    pid?: number;
    shell?: string;
    cwd?: string;
    is_alive?: boolean;
    started_at?: number;
    terminal_mode?: "pty" | "pipe";
    conversation_id: string;
  }[];
}

export interface TerminalSnapshotEvent {
  type: "terminal.snapshot";
  conversation_id: string;
  session_id: string;
  pid?: number | null;
  shell?: string;
  cwd?: string;
  started_at?: number;
  is_alive?: boolean;
  terminal_mode?: "pty" | "pipe";
  output: string;
  output_chars?: number;
  total_output_chars?: number;
  truncated?: boolean;
  error?: string;
}

export interface TerminalResizedEvent {
  type: "terminal.resized";
  conversation_id: string;
  session_id: string;
  cols: number;
  rows: number;
  applied?: boolean;
}

export interface BackgroundCompletedEvent {
  type: "background.completed";
  command_id: string;
  command?: string;
  description?: string;
  exit_code?: number;
  status: string;
  duration?: number;
  started_at?: number;
  completed_at?: number;
  conversation_id: string;
  run_id?: string;
  task_id?: string;
  parent_run_id?: string;
  incarnation?: string;
  seq?: number;
  kind?: "background_command" | string;
  phase?: string;
  updated_at?: number;
  started_at_ms?: number;
  completed_at_ms?: number | null;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
}

export interface BackgroundStartedEvent {
  type: "background.started";
  command_id: string;
  command?: string;
  description?: string;
  cwd?: string;
  status: "running";
  started_at?: number;
  conversation_id: string;
  run_id?: string;
  task_id?: string;
  parent_run_id?: string;
  incarnation?: string;
  seq?: number;
  kind?: "background_command" | string;
  phase?: string;
  updated_at?: number;
  started_at_ms?: number;
  completed_at_ms?: number | null;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
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
  conversation_id: string;
  session_id: string;
  pid?: number;
  shell?: string;
  cwd?: string;
  is_alive?: boolean;
}

export interface TerminalMirrorOutputCommand {
  type: "terminal.mirror.output";
  conversation_id: string;
  session_id: string;
  data: string;
  pid?: number;
  shell?: string;
  cwd?: string;
}

export interface TerminalMirrorExitCommand {
  type: "terminal.mirror.exit";
  conversation_id: string;
  session_id: string;
  exit_code?: number;
}

export interface TerminalExecCommand {
  type: "terminal.exec";
  command: string;
  cwd?: string;
}
