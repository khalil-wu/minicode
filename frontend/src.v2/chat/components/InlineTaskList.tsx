import { CheckCircle2, Circle, ListChecks, Loader2, OctagonAlert } from "lucide-react";
import { useMemo } from "react";
import { useAppStore } from "../../stores";
import { hasVisiblePlanSteps } from "../../lib/planVisibility";
import type { GitChangesState, PlanState, TodoItem } from "../../stores/types";
import "./inline-task-list.css";

interface InlineTaskListProps {
  wide?: boolean;
}

const MAX_VISIBLE_TASKS = 6;

export function InlineTaskList({ wide = false }: InlineTaskListProps = {}) {
  const todos = useAppStore((s) => s.todos);
  const plan = useAppStore((s) => s.plan);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const gitChanges = useAppStore((s) => s.gitChanges);

  const summary = useMemo(() => summarizeWork(todos, plan), [todos, plan]);
  if (!summary) return null;

  const { kind, total, completed, activeTask, activeIndex, allCompleted, visibleTasks, hiddenCount } = summary;
  const gitStats = summarizeGitChanges(gitChanges);
  const progress = total > 0 ? Math.round((completed / total) * 100) : 0;
  const stateLabel = allCompleted
    ? "完成"
    : activeTask?.status === "blocked"
      ? "受阻"
      : activeTask?.status === "in_progress" || isStreaming
        ? "进行中"
        : "待处理";
  const stateTone = allCompleted
    ? "completed"
    : activeTask?.status === "blocked"
      ? "blocked"
      : activeTask?.status === "in_progress" || isStreaming
        ? "running"
        : "pending";
  const activeLabel = allCompleted
    ? kind === "plan" ? "计划已完成" : "任务已完成"
    : activeTask?.status === "blocked"
      ? `受阻：${activeTask.activeForm || activeTask.content}`
      : activeTask?.activeForm || activeTask?.content || (isStreaming ? "正在处理任务" : "任务待处理");
  const countLabel = allCompleted ? `${completed}/${total}` : `${activeIndex}/${total}`;
  const visibleCountLabel = allCompleted
    ? `已完成 ${countLabel}`
    : kind === "plan" ? `第 ${countLabel} 步` : `任务 ${countLabel}`;
  const headerLabel = kind === "plan" ? "计划进度" : "任务进度";
  const ariaLabel = `${visibleCountLabel}：${activeLabel}${gitStats ? `，${gitStats.label}` : ""}`;

  return (
    <div
      className="inline-task-list"
      data-state={stateTone}
      role="status"
      aria-live="polite"
      style={{
        width: wide ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)",
      }}
    >
      <button
        type="button"
        className="inline-task-pill"
        aria-label={ariaLabel}
      >
        <span className="inline-task-pill-icon" aria-hidden="true">
          {stateTone === "completed"
            ? <CheckCircle2 size={14} />
            : stateTone === "blocked"
              ? <OctagonAlert size={14} />
              : <ListChecks size={14} />}
        </span>
        <span className="inline-task-pill-state">{stateLabel}</span>
        <span className="inline-task-pill-title">{activeLabel}</span>
        <span key={visibleCountLabel} className="inline-task-pill-count">
          {visibleCountLabel}
        </span>
        {gitStats && (
          <span className="inline-task-pill-change">
            <span>{gitStats.label}</span>
            {gitStats.additions > 0 && <span className="inline-task-pill-add">+{gitStats.additions.toLocaleString()}</span>}
            {gitStats.deletions > 0 && <span className="inline-task-pill-del">-{gitStats.deletions.toLocaleString()}</span>}
          </span>
        )}
        <span className="inline-task-progress-track" aria-hidden="true">
          <span className="inline-task-progress-fill" style={{ width: `${progress}%` }} />
        </span>
      </button>

      <div className="inline-task-popover" role="tooltip">
        <div className="inline-task-popover-header">
          <span>{headerLabel}</span>
          <span>{completed}/{total}</span>
        </div>
        <ol className="inline-task-popover-list">
          {visibleTasks.map(({ todo, index }) => (
            <li key={todo.id} className="inline-task-popover-row" data-status={todo.status}>
              <span className="inline-task-row-index">{index}</span>
              <span className="inline-task-row-icon" aria-hidden="true">
                <TaskIcon status={todo.status} />
              </span>
              <span className="inline-task-row-text">
                {todo.content}
              </span>
            </li>
          ))}
        </ol>
        {hiddenCount > 0 && (
          <div className="inline-task-popover-more">+{hiddenCount} 个任务</div>
        )}
      </div>
    </div>
  );
}

type InlineWorkItem = Pick<TodoItem, "id" | "content" | "activeForm" | "status">;
function summarizeGitChanges(gitChanges: GitChangesState): { label: string; additions: number; deletions: number } | null {
  const paths = new Set<string>();
  let additions = 0;
  let deletions = 0;
  for (const file of [...gitChanges.workingTree, ...gitChanges.staged]) {
    paths.add(file.path);
    additions += file.additions;
    deletions += file.deletions;
  }
  for (const path of gitChanges.untracked) {
    paths.add(path);
  }
  if (paths.size === 0) return null;
  return {
    label: `${paths.size} 个文件已更改`,
    additions,
    deletions,
  };
}

function summarizeWork(todos: TodoItem[], plan: PlanState | null) {
  if (todos.length > 0) return summarizeItems("todo", todos);
  if (!hasVisiblePlanSteps(plan)) return null;
  const planItems: InlineWorkItem[] = plan.steps
    .filter((step) => step.title.trim())
    .map((step) => ({
      id: step.id,
      content: step.title,
      activeForm: step.status === "running" ? step.title : "",
      status: planStepStatus(step.status),
    }));
  return summarizeItems("plan", planItems);
}

function summarizeItems(kind: "todo" | "plan", items: InlineWorkItem[]) {
  if (items.length === 0) return null;

  const completed = items.filter((todo) => todo.status === "completed").length;
  const allCompleted = completed === items.length;
  const activeIndexRaw = items.findIndex((todo) => todo.status === "in_progress");
  const blockedIndexRaw = items.findIndex((todo) => todo.status === "blocked");
  const pendingIndexRaw = items.findIndex((todo) => todo.status === "pending");
  const currentIndexRaw = activeIndexRaw >= 0
    ? activeIndexRaw
    : blockedIndexRaw >= 0
      ? blockedIndexRaw
      : pendingIndexRaw;
  const activeTask = currentIndexRaw >= 0 ? items[currentIndexRaw] : items.at(-1);
  const activeIndex = allCompleted ? items.length : Math.max(1, currentIndexRaw + 1);

  const prioritized = [...items]
    .map((todo, index) => ({ todo, index: index + 1 }))
    .sort((a, b) => taskPriority(a.todo) - taskPriority(b.todo) || a.index - b.index);

  return {
    kind,
    total: items.length,
    completed,
    activeTask,
    activeIndex,
    allCompleted,
    visibleTasks: prioritized.slice(0, MAX_VISIBLE_TASKS),
    hiddenCount: Math.max(0, items.length - MAX_VISIBLE_TASKS),
  };
}

function planStepStatus(status: string): TodoItem["status"] {
  switch (status) {
    case "done":
    case "skipped":
      return "completed";
    case "running":
      return "in_progress";
    case "failed":
      return "blocked";
    case "pending":
    default:
      return "pending";
  }
}

function taskPriority(todo: InlineWorkItem): number {
  switch (todo.status) {
    case "in_progress":
      return 0;
    case "blocked":
      return 1;
    case "pending":
      return 2;
    case "completed":
    default:
      return 3;
  }
}

function TaskIcon({ status }: { status: TodoItem["status"] }) {
  switch (status) {
    case "completed":
      return <CheckCircle2 size={14} />;
    case "in_progress":
      return <Loader2 size={14} />;
    case "blocked":
      return <OctagonAlert size={14} />;
    case "pending":
    default:
      return <Circle size={13} />;
  }
}
