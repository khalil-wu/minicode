import type { AgentTimelineStatus } from "../types";

export type RawAgentLoopEventType =
  | "agent.turn.started"
  | "agent.turn.completed"
  | "agent.loop.started"
  | "agent.loop.completed"
  | "agent.item.created"
  | "agent.item.updated"
  | "agent.item.completed"
  | "agent.tool_group.started"
  | "agent.tool_group.updated"
  | "agent.tool_group.completed"
  | "agent.file_changes.updated"
  | "agent.system_status"
  | "agent_item"
  | "tool_call"
  | "tool_result"
  | "final_answer_started"
  | "final_answer_delta"
  | "final_answer_committed"
  | "done"
  | "context_compacted"
  | "error";

export interface RawAgentLoopEvent {
  type: RawAgentLoopEventType | string;
  event_id?: string;
  eventId?: string;
  turn_id?: string;
  turnId?: string;
  loop_id?: string;
  loopId?: string;
  item_id?: string;
  itemId?: string;
  group_id?: string;
  groupId?: string;
  id?: string;
  seq?: number;
  order?: number;
  timestamp?: string;
  created_at?: number | string;
  source?: "model" | "runtime" | "tool" | "system" | string;
  visibility?: "timeline" | "subtle" | "debug" | string;
  status?: AgentTimelineStatus | "done" | "success" | "failed" | "error" | "interrupted" | string;
  kind?: string;
  role?: string;
  title?: string;
  summary?: string;
  content?: string;
  detail?: string;
  message?: string;
  tool_name?: string;
  toolName?: string;
  name?: string;
  args?: Record<string, unknown>;
  input?: Record<string, unknown>;
  result?: Record<string, unknown> | string;
  output?: string;
  exit_code?: number;
  exitCode?: number;
  files?: RawFileChange[];
  added?: number;
  removed?: number;
  additions?: number;
  deletions?: number;
  url?: string;
}

export interface RawFileChange {
  path?: string;
  file_path?: string;
  status?: "modified" | "created" | "deleted" | "added" | "removed" | string;
  added?: number;
  removed?: number;
  additions?: number;
  deletions?: number;
}
