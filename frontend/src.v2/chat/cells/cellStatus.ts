/**
 * One status vocabulary and one metadata format for every transcript cell.
 *
 * Commands, file changes, browser actions and generic tools all report the
 * same lifecycle, so they must read the same. Each cell maps its local status
 * union onto `CellStatus` once and renders from these helpers instead of
 * inventing its own labels, tones or duration formatting.
 */

export type CellStatus =
  | "pending"
  | "running"
  | "success"
  | "partial"
  | "failed"
  | "blocked"
  | "timeout"
  | "cancelled";

/** Visual tone. Fewer tones than statuses: several failures share one look. */
export type CellTone = "running" | "success" | "partial" | "failed" | "cancelled";

const STATUS_LABELS: Record<CellStatus, string> = {
  pending: "准备中",
  running: "运行中",
  success: "成功",
  partial: "未完整结束",
  failed: "失败",
  blocked: "已阻止",
  timeout: "超时",
  cancelled: "已取消",
};

const STATUS_TONES: Record<CellStatus, CellTone> = {
  pending: "running",
  running: "running",
  success: "success",
  partial: "partial",
  failed: "failed",
  blocked: "failed",
  timeout: "failed",
  cancelled: "cancelled",
};

export const cellStatusLabel = (status: CellStatus): string => STATUS_LABELS[status];

export const cellStatusTone = (status: CellStatus): CellTone => STATUS_TONES[status];

export const isRunningCellStatus = (status: CellStatus): boolean =>
  status === "pending" || status === "running";

/** Map ActivityCellState.status onto the shared vocabulary. */
export const activityCellStatus = (
  status: "running" | "done" | "partial" | "failed" | "interrupted",
): CellStatus => {
  if (status === "done") return "success";
  if (status === "interrupted") return "cancelled";
  return status;
};

/** Map ExecCellState.status onto the shared vocabulary. */
export const execCellStatus = (
  status: "pending_approval" | "running" | "success" | "partial" | "failed" | "cancelled",
): CellStatus => (status === "pending_approval" ? "pending" : status);

/**
 * Single duration format for every cell. Sub-second work keeps millisecond
 * precision because that is the only resolution that distinguishes a cache hit
 * from real work; anything longer reads in seconds.
 */
export function formatCellDuration(ms: number | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const wholeSeconds = Math.floor(seconds);
  return `${Math.floor(wholeSeconds / 60)}m${wholeSeconds % 60}s`;
}
