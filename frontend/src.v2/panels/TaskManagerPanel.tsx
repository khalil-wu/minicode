import { useAppStore } from "../stores";
import type { TodoItem, BackgroundTaskEntry } from "../stores/types";
import { AgentProgressTrace } from "./AgentProgressTrace";

export const TaskManagerPanel = () => {
  const todos = useAppStore((s) => s.todos);
  const backgroundTasks = useAppStore((s) => s.backgroundTasks);
  const hasProgress = useAppStore((s) => {
    const key = s.conversationId || "__active__";
    return s.agentProgress.some((entry) => entry.conversationId === key || entry.conversationId === "__active__");
  });

  const hasTodos = todos.length > 0;
  const hasBg = backgroundTasks.length > 0;

  if (!hasTodos && !hasBg && !hasProgress) {
    return (
      <div style={{ padding: 16, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        No tasks. The agent will create tasks when working on complex operations.
      </div>
    );
  }

  // Group todos by status
  const inProgress = todos.filter(t => t.status === "in_progress");
  const pending = todos.filter(t => t.status === "pending");
  const completed = todos.filter(t => t.status === "completed");
  const total = todos.length;
  const completedCount = completed.length;
  const progress = total > 0 ? Math.round((completedCount / total) * 100) : 0;

  return (
    <div style={{ padding: 12, fontSize: "var(--text-sm)", overflow: "auto" }}>
      <AgentProgressTrace />

      {hasTodos && (
        <>
          {/* Header with stats */}
          <div style={{
            fontSize: "var(--text-xs)",
            color: "var(--text-muted)",
            marginTop: hasProgress ? 14 : 0,
            marginBottom: 8,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between"
          }}>
            <span style={{ textTransform: "uppercase", fontWeight: 650 }}>
              Tasks ({todos.length})
            </span>
            <span style={{ fontFamily: "var(--font-mono)" }}>
              {completedCount}/{total} ({progress}%)
            </span>
          </div>

          {/* Progress bar */}
          <div style={{
            height: 4,
            background: "var(--surface-base)",
            borderRadius: 2,
            overflow: "hidden",
            marginBottom: 12
          }}>
            <div style={{
              height: "100%",
              background: "var(--accent-primary)",
              width: `${progress}%`,
              transition: "width 300ms cubic-bezier(0.4, 0, 0.2, 1)",
              borderRadius: 2
            }} />
          </div>

          {/* In Progress Section */}
          {inProgress.length > 0 && (
            <TaskSection title="In Progress" count={inProgress.length} tasks={inProgress} />
          )}

          {/* Pending Section */}
          {pending.length > 0 && (
            <TaskSection title="Pending" count={pending.length} tasks={pending} />
          )}

          {/* Completed Section */}
          {completed.length > 0 && (
            <TaskSection title="Completed" count={completed.length} tasks={completed} isCompleted />
          )}

          {/* All done celebration */}
          {completed.length > 0 && inProgress.length === 0 && pending.length === 0 && (
            <div style={{
              marginTop: 12,
              padding: "8px 12px",
              background: "color-mix(in oklch, var(--state-success) 10%, transparent)",
              border: "1px solid color-mix(in oklch, var(--state-success) 30%, transparent)",
              borderRadius: "var(--radius-sm)",
              color: "var(--state-success)",
              fontSize: "var(--text-sm)",
              fontWeight: 600,
              textAlign: "center"
            }}>
              🎉 All tasks complete!
            </div>
          )}
        </>
      )}

      {hasBg && (
        <>
          <div style={{
            fontSize: "var(--text-xs)",
            color: "var(--text-muted)",
            marginTop: hasTodos ? 16 : 0,
            marginBottom: 8,
            textTransform: "uppercase",
            fontWeight: 650
          }}>
            Background ({backgroundTasks.length})
          </div>
          {backgroundTasks.map((bg) => (
            <BackgroundTaskRow key={bg.id} task={bg} />
          ))}
        </>
      )}
    </div>
  );
};

const TaskSection = ({
  title,
  count,
  tasks,
  isCompleted = false
}: {
  title: string;
  count: number;
  tasks: TodoItem[];
  isCompleted?: boolean;
}) => {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{
        fontSize: "var(--text-xs)",
        color: "var(--text-secondary)",
        marginBottom: 6,
        fontWeight: 650,
        display: "flex",
        alignItems: "center",
        gap: 6
      }}>
        <span>{title}</span>
        <span style={{
          background: "var(--surface-base)",
          padding: "1px 6px",
          borderRadius: "var(--radius-sm)",
          fontFamily: "var(--font-mono)"
        }}>
          {count}
        </span>
      </div>
      {tasks.map((t, idx) => (
        <TaskRow key={t.id} task={t} index={idx + 1} isCompleted={isCompleted} />
      ))}
    </div>
  );
};

const TaskRow = ({
  task,
  index,
  isCompleted
}: {
  task: TodoItem;
  index: number;
  isCompleted: boolean;
}) => {
  const isInProgress = task.status === "in_progress";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 8px",
        borderRadius: "var(--radius-sm)",
        background: isInProgress
          ? "color-mix(in oklch, var(--accent-primary) 5%, transparent)"
          : "transparent",
        border: "1px solid transparent",
        borderColor: isInProgress ? "color-mix(in oklch, var(--accent-primary) 20%, transparent)" : "transparent",
        marginBottom: 4,
        transition: "all var(--transition-fast)"
      }}
    >
      {/* Task ID */}
      <span style={{
        fontFamily: "var(--font-mono)",
        fontSize: "var(--text-xs)",
        color: "var(--text-muted)",
        width: 20,
        textAlign: "right"
      }}>
        #{index}
      </span>

      {/* Status indicator */}
      <button
        type="button"
        disabled
        title={`Status: ${task.status}`}
        style={{
          width: 14,
          height: 14,
          borderRadius: "50%",
          border: `2px solid ${statusColor(task.status)}`,
          background: task.status === "completed" ? statusColor(task.status) : "transparent",
          cursor: "default",
          padding: 0,
          flexShrink: 0
        }}
      />

      {/* Content */}
      <span
        style={{
          flex: 1,
          color: task.status === "completed" ? "var(--text-muted)" : "var(--text-primary)",
          textDecoration: task.status === "completed" ? "line-through" : "none",
          fontSize: "var(--text-sm)",
          fontWeight: isInProgress ? 600 : 400
        }}
      >
        {task.status === "in_progress" && task.activeForm ? task.activeForm : task.content}
      </span>

      {/* Status badge */}
      <span style={{
        fontSize: "var(--text-xs)",
        color: statusColor(task.status),
        fontFamily: "var(--font-mono)",
        textTransform: "capitalize",
        opacity: 0.8
      }}>
        {task.status === "in_progress" ? "●" : task.status === "completed" ? "✓" : "○"}
      </span>
    </div>
  );
};

const BackgroundTaskRow = ({ task }: { task: BackgroundTaskEntry }) => {
  const shortCmd = task.command.length > 50 ? task.command.slice(0, 47) + "..." : task.command;
  const ago = formatAgo(task.timestamp);
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "5px 0",
        borderBottom: "1px solid var(--border-subtle)"
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: task.status === "completed" ? "var(--state-success)" : "var(--state-danger)",
          flexShrink: 0
        }}
      />
      <span
        style={{
          flex: 1,
          fontFamily: "var(--font-mono)",
          fontSize: "var(--text-xs)",
          color: "var(--text-primary)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap"
        }}
        title={task.command}
      >
        {shortCmd}
      </span>
      {task.duration != null && (
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          {(task.duration / 1000).toFixed(1)}s
        </span>
      )}
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
        {ago}
      </span>
    </div>
  );
};

function statusColor(status: TodoItem["status"]): string {
  switch (status) {
    case "completed": return "var(--state-success)";
    case "in_progress": return "var(--accent-primary)";
    case "blocked": return "var(--state-warning)";
    default: return "var(--text-muted)";
  }
}

function formatAgo(ts: number): string {
  const diff = Math.max(0, Date.now() - ts);
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  return `${Math.floor(diff / 3_600_000)}h ago`;
}
