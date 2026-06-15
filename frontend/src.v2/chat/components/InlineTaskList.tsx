import { CheckCircle2, Circle, Loader2, ListChecks, OctagonAlert } from "lucide-react";
import { useAppStore } from "../../stores";
import type { TodoItem } from "../../stores/types";
import "./inline-task-list.css";

export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const isStreaming = useAppStore((s) => s.isStreaming);

  if (todos.length === 0) return null;

  const active = todos
    .map((todo, index) => ({ todo, index: index + 1 }))
    .filter(({ todo }) => todo.status !== "completed");
  const completed = todos.filter((t) => t.status === "completed");
  const total = todos.length;
  const completedCount = completed.length;
  const progress = total > 0 ? (completedCount / total) * 100 : 0;
  const allCompleted = active.length === 0 && completed.length > 0;

  return (
    <div className="inline-task-list" role="status" aria-live="polite">
      <div className="inline-task-header">
        <div className="inline-task-title">
          <ListChecks size={14} />
          <span>Tasks</span>
          {!allCompleted && (
            <>
              <div className="inline-task-progress-bar">
                <div
                  className="inline-task-progress-fill"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <span className="inline-task-progress-text">
                {completedCount}/{total}
              </span>
            </>
          )}
        </div>
      </div>

      {active.length > 0 && (
        <div className="inline-task-section">
          {active.map(({ todo, index }) => (
            <TaskRow
              key={todo.id}
              todo={todo}
              index={index}
              isStreaming={isStreaming}
            />
          ))}
        </div>
      )}

      {completed.length > 0 && active.length > 0 && (
        <div className="inline-task-divider" />
      )}

      {allCompleted && (
        <div className="inline-task-done-header">
          <CheckCircle2 size={14} />
          <span>Tasks complete</span>
        </div>
      )}
    </div>
  );
}

function TaskRow({
  todo,
  index,
  isStreaming
}: {
  todo: TodoItem;
  index: number;
  isStreaming: boolean;
}) {
  const isActive = todo.status === "in_progress";
  const isDone = todo.status === "completed";
  const isBlocked = todo.status === "blocked";

  const icon = isActive
    ? <Loader2 size={14} />
    : isDone
      ? <CheckCircle2 size={14} />
      : isBlocked
        ? <OctagonAlert size={14} />
        : <Circle size={13} />;

  const iconClass = `inline-task-icon ${
    isDone ? 'inline-task-icon-completed' :
    isActive ? 'inline-task-icon-in-progress' :
    isBlocked ? 'inline-task-icon-blocked' :
    'inline-task-icon-pending'
  }`;

  const contentClass = `inline-task-content ${
    isDone ? 'inline-task-content-completed' :
    isActive ? 'inline-task-content-in-progress' :
    isBlocked ? 'inline-task-content-blocked' :
    'inline-task-content-pending'
  }`;

  return (
    <div className="inline-task-row">
      <span className="inline-task-id">#{index}</span>

      <span className={iconClass}>
        {icon}
      </span>

      <span className={contentClass}>
        {todo.content}
      </span>

      {isActive && isStreaming && todo.activeForm && (
        <span className="inline-task-active-form" title={todo.activeForm}>
          {todo.activeForm}
        </span>
      )}
    </div>
  );
}
