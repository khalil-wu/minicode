import type { DiffCellState, PlanCellState } from "../chat/cells/cellTypes";

export type AgentTurnStatus = "running" | "completed" | "failed" | "stopped";

export type AgentTimelineStatus = "pending" | "running" | "completed" | "failed";

export type AgentTimelineItem =
  | ProcessItem
  | LiveNarrationItem
  | ActivityGroupItem
  | FileChangesItem
  | PlanTimelineItem
  | BrowserPreviewItem
  | SystemStatusItem;

export interface ProcessItem {
  id: string;
  type: "process";
  kind: "process_text" | "action_summary" | "observation" | "skill";
  source: "model" | "provider" | "runtime";
  loopId?: string;
  seq: number;
  content: string;
  status: "running" | "completed" | "failed" | "info";
  title?: string;
  summary?: string;
  skill?: {
    name?: string;
    triggerMode?: string;
    sourceLevel?: string;
    reason?: string;
    tokenEstimate?: number;
  };
}

export interface LiveNarrationItem {
  id: string;
  type: "live_narration";
  seq: number;
  partialMarkdown: string;
  isStreaming: boolean;
  updatedAt: number;
}

export interface ActivityGroupItem {
  id: string;
  type: "activity_group";
  activityKind:
    | "web_search"
    | "web_read"
    | "file_read"
    | "file_change"
    | "command"
    | "test"
    | "browser"
    | "mcp"
    | "unknown";
  loopId?: string;
  seq: number;
  title: string;
  summary: string;
  durationLabel?: string;
  status: "running" | "completed" | "failed";
  details: ActivityDetail[];
  defaultCollapsed: boolean;
  emphasis?: "inline" | "group";
}

export type ActivityDetail =
  | {
      kind: "shell";
      title: string;
      command: string;
      output?: string;
      exitCode?: number;
    }
  | {
      kind: "source";
      title: string;
      url?: string;
      path?: string;
      query?: string;
      excerpt?: string;
      lineInfo?: string;
      additions?: number;
      deletions?: number;
      changeType?: "created" | "updated" | "deleted";
    }
  | {
      kind: "text";
      title: string;
      content: string;
    };

export interface FileChangesItem {
  id: string;
  type: "file_changes";
  seq: number;
  cell: DiffCellState;
  added: number;
  removed: number;
  files: {
    path: string;
    added: number;
    removed: number;
    status: "modified" | "created" | "deleted";
    patch?: string;
  }[];
}

export interface PlanTimelineItem {
  id: string;
  type: "plan";
  seq: number;
  cell: PlanCellState;
}

export interface BrowserPreviewItem {
  id: string;
  type: "browser_preview";
  seq: number;
  title: string;
  url?: string;
  status: "running" | "completed" | "failed";
}

export interface SystemStatusItem {
  id: string;
  type: "system_status";
  seq: number;
  content: string;
  detail?: string;
  ariaLabel?: string;
  tone: "subtle" | "warning" | "error";
}
