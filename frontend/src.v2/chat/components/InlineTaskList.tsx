import { CheckCircle2, Circle, Loader2, ListChecks, OctagonAlert, ChevronDown, ChevronRight, X, Plus, LayoutTemplate, BarChart3 } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppStore } from "../../stores";
import { createTodoItem } from "../../lib/todo-utils";
import type { TodoItem } from "../../stores/types";
import { useTaskStats } from "./useTaskStats";
import { TaskTemplates } from "./TaskTemplates";
import { TaskStats } from "./TaskStats";
import "./inline-task-list.css";

export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const addTodo = useAppStore((s) => s.addTodo);
  const stats = useTaskStats();
  const [collapsed, setCollapsed] = useState(false);
  const [showCompleted, setShowCompleted] = useState(true);
  const [isAddingTask, setIsAddingTask] = useState(false);
  const [newTaskContent, setNewTaskContent] = useState("");
  const [showTemplates, setShowTemplates] = useState(false);
  const [showStats, setShowStats] = useState(false);

  if (todos.length === 0) return null;

  const active = todos
    .map((todo, index) => ({ todo, index: index + 1 }))
    .filter(({ todo }) => todo.status !== "completed");

  // Auto-collapse when all tasks are completed
  useEffect(() => {
    if (stats.allCompleted && !isStreaming) {
      setCollapsed(true);
    }
  }, [stats.allCompleted, isStreaming]);

  // Show filtered tasks
  const displayTasks = showCompleted
    ? todos.map((todo, index) => ({ todo, index: index + 1 }))
    : active;

  const handleAddTask = () => {
    if (!newTaskContent.trim()) return;
    addTodo(createTodoItem(newTaskContent.trim()));
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
          aria-label={collapsed ? "展开任务" : "折叠任务"}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        <div className="inline-task-title">
          <ListChecks size={14} />
          <span>任务</span>
          {!collapsed && !stats.allCompleted && (
            <>
              <div className="inline-task-progress-bar">
                <div
                  className="inline-task-progress-fill"
                  style={{ width: `${stats.progress}%` }}
                />
              </div>
              <span className="inline-task-progress-text">
                {stats.completedCount}/{stats.total}
              </span>
            </>
          )}
          {collapsed && (
            <span className="inline-task-collapsed-summary">
              {stats.allCompleted ? "✓ 已完成" : `${stats.completedCount}/${stats.total}`}
            </span>
          )}
        </div>
        {!collapsed && (
          <>
            {stats.completed > 0 && (
              <button
                type="button"
                className="inline-task-filter-btn"
                onClick={() => setShowCompleted(!showCompleted)}
                title={showCompleted ? "隐藏已完成" : "显示已完成"}
              >
                {showCompleted ? "隐藏" : "显示"}已完成
              </button>
            )}
            <button
              type="button"
              className="inline-task-filter-btn"
              onClick={() => setShowStats(!showStats)}
              title="查看统计"
            >
              <BarChart3 size={14} />
            </button>
            <button
              type="button"
              className="inline-task-filter-btn"
              onClick={() => setShowTemplates(!showTemplates)}
              title="加载模板"
            >
              <LayoutTemplate size={14} />
            </button>
          </>
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

          {stats.allCompleted && (
            <div className="inline-task-done-header">
              <CheckCircle2 size={14} />
              <span>所有任务已完成</span>
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
                    placeholder="输入任务描述..."
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
                    添加
                  </button>
                  <button
                    type="button"
                    className="inline-task-add-cancel-btn"
                    onClick={() => {
                      setIsAddingTask(false);
                      setNewTaskContent("");
                    }}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="inline-task-add-btn"
                  onClick={() => setIsAddingTask(true)}
                >
                  <Plus size={12} />
                  <span>添加任务</span>
                </button>
              )}
            </div>
          )}
        </>
      )}

      {/* 🔧 新增：模板选择器 */}
      {showTemplates && <TaskTemplates onApply={() => setShowTemplates(false)} />}

      {/* 🔧 新增：任务统计 */}
      {showStats && <TaskStats onClose={() => setShowStats(false)} />}
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
  const updateTodo = useAppStore((s) => s.updateTodo);
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState(todo.content);

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

  const handleDoubleClick = () => {
    if (!isActive && !isStreaming) {
      setIsEditing(true);
      setEditContent(todo.content);
    }
  };

  const handleSave = () => {
    const trimmed = editContent.trim();
    if (trimmed && trimmed !== todo.content) {
      updateTodo(todo.id, { content: trimmed });
    }
    setIsEditing(false);
  };

  const handleCancel = () => {
    setEditContent(todo.content);
    setIsEditing(false);
  };

  return (
    <div className="inline-task-row">
      <span className="inline-task-id">#{index}</span>

      <span className={iconClass}>
        {icon}
      </span>

      {isEditing ? (
        <input
          type="text"
          className="inline-task-edit-input"
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSave();
            if (e.key === "Escape") handleCancel();
          }}
          onBlur={handleSave}
          autoFocus
        />
      ) : (
        <span
          className={contentClass}
          onDoubleClick={handleDoubleClick}
          title={!isActive && !isStreaming ? "双击编辑" : ""}
        >
          {todo.content}
        </span>
      )}

      {isActive && isStreaming && todo.activeForm && (
        <span className="inline-task-active-form" title={todo.activeForm}>
          {todo.activeForm}
        </span>
      )}

      {!isActive && !isStreaming && !isEditing && (
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
