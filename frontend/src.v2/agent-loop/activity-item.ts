export type ActivityItemKind =
  | "reasoning"
  | "tool"
  | "command"
  | "file_change"
  | "approval"
  | "agent"
  | "plan"
  | "system";

export type ActivityItemStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "blocked"
  | "info";

export type ActivityItemVisibility = "main" | "activity" | "developer";

/** Stable UI contract. Legacy ContentBlock and ToolCallRecord shapes stop at the adapter. */
export interface ActivityItem {
  id: string;
  taskId?: string;
  turnId?: string;
  seq?: number;
  messageId?: string;
  agentId?: string;
  parentId?: string;
  kind: ActivityItemKind;
  status: ActivityItemStatus;
  phase?: "orienting" | "planning" | "researching" | "implementing" | "verifying" | "finalizing";
  title: string;
  summary?: string;
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  visibility: ActivityItemVisibility;
  groupKey?: string;
  hasFailure: boolean;
  hasPendingUserAction: boolean;
}
