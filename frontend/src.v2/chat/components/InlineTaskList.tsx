import { CheckCircle2, Circle, Loader2, ListChecks, OctagonAlert, ChevronDown, ChevronRight, X, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppStore } from "../../stores";
import type { TodoItem } from "../../stores/types";
import "./inline-task-list.css";

export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const addTodo = useAppStore((s) => s.addTodo);
  const [collapsed, setCollapsed] = useState(false);
  const [showCompleted, setShowCompleted] = useState(true);
  const [isAddingTask, setIsAddingTask] = useState(false);
  const [newTaskContent, setNewTaskContent] = useState("");

  if (todos.length === 0) return null;

  const active = todos
    .map((todo, index) => ({ todo, index: index + 1 }))
    .filter(({ todo }) => todo.status !== "completed");
  const completed = todos.filter((t) => t.status === "completed");
  const total = todos.length;
  const completedCount = completed.length;
  const progress = total > 0 ? (completedCount / total) * 100 : 0;
  const allCompleted = active.length === 0 && completed.length > 0;

  // Auto-collapse when all tasks are completed
  useEffect(() => {
    if (allCompleted && !isStreaming) {
      setCollapsed(true);
    }
  }, [allCompleted, isStreaming]);

  // Show filtered tasks
  const displayTasks = showCompleted
    ? todos.map((todo, index) => ({ todo, index: index + 1 }))
    : active;

  const handleAddTask = () => {
    if (!newTaskContent.trim()) return;
    const newTodo: TodoItem = {
      id: `todo-${Date.now()}`,
      content: newTaskContent.trim(),
      status: "pending",
    };
    addTodo(newTodo);
    setNewTaskContent("");
    setIsAddingTask(false);
  };

  return (
    <div className="inline-task-list" role="status" aria-live="polite">
      <div className="inline-task-header">
        <button
          type="button"
          className="inline-task-collapse-btn"
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? "Expand tasks" : "Collapse tasks"}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        <div className="inline-task-title">
          <ListChecks size={14} />
          <span>Tasks</span>
          {!collapsed && !allCompleted && (
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
          {collapsed && (
            <span className="inline-task-collapsed-summary">
              {allCompleted ? "✓ Complete" : `${completedCount}/${total}`}
            </span>
          )}
        </div>
        {!collapsed && completed.length > 0 && (
          <button
            type="button"
            className="inline-task-filter-btn"
            onClick={() => setShowCompleted(!showCompleted)}
            title={showCompleted ? "Hide completed" : "Show completed"}
          >
            {showCompleted ? "Hide" : "Show"} completed
          </button>
        )}
      </div>

      {!collapsed && (
        <>
          {displayTasks.length > 0 && (
            <div className="inline-task-section">
              {displayTasks.map(({ todo, index }) => (
                <TaskRow
                  key={todo.id}
                  todo={todo}
                  index={index}
                  isStreaming={isStreaming}
                />
              ))}
            </div>
          )}

          {allCompleted && (
            <div className="inline-task-done-header">
              <CheckCircle2 size={14} />
              <span>All tasks complete</span>
            </div>
          )}

          {/* Add new task */}
          {!isStreaming && (
            <div className="inline-task-add-section">
              {isAddingTask ? (
                <div className="inline-task-add-input-row">
                  <input
                    type="text"
                    className="inline-task-add-input"
                    placeholder="Enter task description..."
                    value={newTaskContent}
                    onChange={(e) => setNewTaskContent(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAddTask();
                      if (e.key === "Escape") {
                        setIsAddingTask(false);
                        setNewTaskContent("");
                      }
                    }}
                    autoFocus
                  />
                  <button
                    type="button"
                    className="inline-task-add-save-btn"
                    onClick={handleAddTask}
                    disabled={!newTaskContent.trim()}
                  >
                    Add
                  </button>
                  <button
                    type="button"
                    className="inline-task-add-cancel-btn"
                    onClick={() => {
                      setIsAddingTask(false);
                      setNewTaskContent("");
                    }}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="inline-task-add-btn"
                  onClick={() => setIsAddingTask(true)}
                >
                  <Plus size={12} />
                  <span>Add task</span>
                </button>
              )}
            </div>
          )}
        </>
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
  const removeTodo = useAppStore((s) => s.removeTodo);

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

  const handleDelete = () => {
    if (!isActive && !isStreaming) {
      removeTodo(todo.id);
    }
  };

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

      {!isActive && !isStreaming && (
        <button
          type="button"
          className="inline-task-delete-btn"
          onClick={handleDelete}
          title="删除任务"
          aria-label="删除任务"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}
