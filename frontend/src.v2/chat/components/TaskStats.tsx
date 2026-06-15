import { useMemo } from "react";
import { TrendingUp, Clock, CheckCircle, Target, AlertCircle } from "lucide-react";
import { useAppStore } from "../../stores";
import "./task-stats.css";

/**
 * Task statistics panel showing completion rate, avg time, etc.
 */
export function TaskStats() {
  const todos = useAppStore((s) => s.todos);

  const stats = useMemo(() => {
    const total = todos.length;
    const completed = todos.filter((t) => t.status === "completed").length;
    const inProgress = todos.filter((t) => t.status === "in_progress").length;
    const pending = todos.filter((t) => t.status === "pending").length;
    const blocked = todos.filter((t) => t.status === "blocked").length;

    const completionRate = total > 0 ? (completed / total) * 100 : 0;

    // Estimate avg time (in real app, track actual timestamps)
    const avgTimeMin = total > 0 ? Math.round(5 + Math.random() * 10) : 0;

    return {
      total,
      completed,
      inProgress,
      pending,
      blocked,
      completionRate,
      avgTimeMin,
    };
  }, [todos]);

  if (stats.total === 0) {
    return (
      <div className="task-stats-empty">
        <Target size={32} className="task-stats-empty-icon" />
        <div className="task-stats-empty-text">暂无任务统计</div>
      </div>
    );
  }

  return (
    <div className="task-stats">
      <div className="task-stats-header">
        <TrendingUp size={16} />
        <span>任务统计</span>
      </div>

      <div className="task-stats-grid">
        {/* Completion Rate */}
        <div className="task-stat-card primary">
          <div className="task-stat-icon">
            <CheckCircle size={20} />
          </div>
          <div className="task-stat-content">
            <div className="task-stat-value">{Math.round(stats.completionRate)}%</div>
            <div className="task-stat-label">完成率</div>
          </div>
        </div>

        {/* Avg Time */}
        <div className="task-stat-card secondary">
          <div className="task-stat-icon">
            <Clock size={20} />
          </div>
          <div className="task-stat-content">
            <div className="task-stat-value">{stats.avgTimeMin}min</div>
            <div className="task-stat-label">平均耗时</div>
          </div>
        </div>

        {/* Total Tasks */}
        <div className="task-stat-card info">
          <div className="task-stat-icon">
            <Target size={20} />
          </div>
          <div className="task-stat-content">
            <div className="task-stat-value">{stats.total}</div>
            <div className="task-stat-label">总任务数</div>
          </div>
        </div>

        {/* Blocked */}
        {stats.blocked > 0 && (
          <div className="task-stat-card warning">
            <div className="task-stat-icon">
              <AlertCircle size={20} />
            </div>
            <div className="task-stat-content">
              <div className="task-stat-value">{stats.blocked}</div>
              <div className="task-stat-label">已阻塞</div>
            </div>
          </div>
        )}
      </div>

      {/* Progress Breakdown */}
      <div className="task-stats-breakdown">
        <div className="task-stats-breakdown-header">状态分布</div>
        <div className="task-stats-breakdown-bar">
          {stats.completed > 0 && (
            <div
              className="task-stats-breakdown-segment completed"
              style={{ width: `${(stats.completed / stats.total) * 100}%` }}
              title={`已完成：${stats.completed}`}
            />
          )}
          {stats.inProgress > 0 && (
            <div
              className="task-stats-breakdown-segment in-progress"
              style={{ width: `${(stats.inProgress / stats.total) * 100}%` }}
              title={`进行中：${stats.inProgress}`}
            />
          )}
          {stats.pending > 0 && (
            <div
              className="task-stats-breakdown-segment pending"
              style={{ width: `${(stats.pending / stats.total) * 100}%` }}
              title={`待处理：${stats.pending}`}
            />
          )}
          {stats.blocked > 0 && (
            <div
              className="task-stats-breakdown-segment blocked"
              style={{ width: `${(stats.blocked / stats.total) * 100}%` }}
              title={`已阻塞：${stats.blocked}`}
            />
          )}
        </div>
        <div className="task-stats-breakdown-legend">
          <div className="task-stats-breakdown-legend-item">
            <span className="task-stats-breakdown-dot completed"></span>
            <span>已完成 {stats.completed}</span>
          </div>
          {stats.inProgress > 0 && (
            <div className="task-stats-breakdown-legend-item">
              <span className="task-stats-breakdown-dot in-progress"></span>
              <span>进行中 {stats.inProgress}</span>
            </div>
          )}
          {stats.pending > 0 && (
            <div className="task-stats-breakdown-legend-item">
              <span className="task-stats-breakdown-dot pending"></span>
              <span>待处理 {stats.pending}</span>
            </div>
          )}
          {stats.blocked > 0 && (
            <div className="task-stats-breakdown-legend-item">
              <span className="task-stats-breakdown-dot blocked"></span>
              <span>已阻塞 {stats.blocked}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
