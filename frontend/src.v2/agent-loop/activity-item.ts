/**
 * Canonical activity-item projection shared by the Agents panel, the Activity
 * feed and future replay views. Currently produced only by
 * projection/project-subagent-activity-items.ts.
 */

export type ActivityItemStatus =
  | "queued"
  | "running"
  | "blocked"
  | "completed"
  | "cancelled"
  | "failed";

export type ActivityItemPhase =
  | "researching"
  | "implementing"
  | "verifying"
  | "finalizing";

/** Where an item may be rendered: `activity` = user-facing feed, `developer` = Inspector only. */
export type ActivityItemVisibility = "activity" | "developer";

export interface ActivityItem {
  id: string;
  taskId?: string;
  agentId?: string;
  parentId?: string;
  kind: "agent" | string;
  status: ActivityItemStatus;
  phase?: ActivityItemPhase;
  title: string;
  summary?: string;
  finishedAt?: number;
  visibility: ActivityItemVisibility;
  /** Items with the same groupKey render as one lifecycle group. */
  groupKey: string;
  hasFailure: boolean;
  hasPendingUserAction: boolean;
}
