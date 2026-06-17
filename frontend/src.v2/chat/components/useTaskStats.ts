import { useMemo } from "react";
import { useAppStore } from "../../stores";

export interface TaskStatsResult {
  total: number;
  completed: number;
  inProgress: number;
  pending: number;
  blocked: number;
  active: number;
  completedCount: number;
  progress: number;
  allCompleted: boolean;
}

/**
 * Shared hook that computes task statistics from the global todos store.
 * Used by InlineTaskList (header + progress bar) and TaskStats (dashboard).
 */
export function useTaskStats(): TaskStatsResult {
  const todos = useAppStore((s) => s.todos);

  return useMemo(() => {
    const total = todos.length;
    const completed = todos.filter((t) => t.status === "completed").length;
    const inProgress = todos.filter((t) => t.status === "in_progress").length;
    const pending = todos.filter((t) => t.status === "pending").length;
    const blocked = todos.filter((t) => t.status === "blocked").length;
    const active = total - completed;
    const progress = total > 0 ? (completed / total) * 100 : 0;
    const allCompleted = active === 0 && completed > 0;

    return { total, completed, inProgress, pending, blocked, active, completedCount: completed, progress, allCompleted };
  }, [todos]);
}
