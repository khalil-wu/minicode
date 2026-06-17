import { TrendingUp, Clock, CheckCircle2, Target, AlertCircle, X } from "lucide-react";
import { useTaskStats } from "./useTaskStats";
import "./task-stats.css";

interface TaskStatsProps {
  onClose?: () => void;
}

/**
 * Task statistics panel showing completion rate, status distribution, etc.
 */
export function TaskStats({ onClose }: TaskStatsProps) {
  const stats = useTaskStats();

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
        {onClose && (
          <button
            type="button"
            className="task-stats-close"
            onClick={onClose}
            title="关闭统计"
            aria-label="关闭统计面板"
          >
            <X size={14} />
          </button>
        )}
      </div>

      <div className="task-stats-grid">
        {/* Completion Rate */}
        <div className="task-stat-card primary">
          <div className="task-stat-icon">
            <CheckCircle2 size={20} />
          </div>
          <div className="task-stat-content">
            <div className="task-stat-value">{Math.round(stats.progress)}%</div>
            <div className="task-stat-label">完成率</div>
          </div>
        </div>

        {/* In Progress */}
        <div className="task-stat-card secondary">
          <div className="task-stat-icon">
            <Clock size={20} />
          </div>
          <div className="task-stat-content">
            <div className="task-stat-value">{stats.inProgress}</div>
            <div className="task-stat-label">进行中</div>
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
