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

  return (
    <div style={{ padding: 12, fontSize: "var(--text-sm)", overflow: "auto" }}>
      <AgentProgressTrace />
      {hasTodos && (
        <>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: hasProgress ? 14 : 0, marginBottom: 8, textTransform: "uppercase" }}>
            Tasks ({todos.length})
          </div>
          {todos.map((t) => (
            <div
              key={t.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "6px 0",
                borderBottom: "1px solid var(--border-subtle)",
              }}
            >
              <button
                type="button"
                disabled
                title={`Status: ${t.status}`}
                style={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  border: `2px solid ${statusColor(t.status)}`,
                  background: t.status === "completed" ? statusColor(t.status) : "transparent",
                  cursor: "default",
                  padding: 0,
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  flex: 1,
                  color: t.status === "completed" ? "var(--text-muted)" : "var(--text-primary)",
                  textDecoration: t.status === "completed" ? "line-through" : "none",
                }}
              >
                {t.status === "in_progress" ? t.activeForm || t.content : t.content}
              </span>
              <span style={{ fontSize: "var(--text-xs)", color: statusColor(t.status) }}>
                {t.status}
              </span>
            </div>
          ))}
        </>
      )}

      {hasBg && (
        <>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: hasTodos ? 16 : 0, marginBottom: 8, textTransform: "uppercase" }}>
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
        borderBottom: "1px solid var(--border-subtle)",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: task.status === "completed" ? "var(--state-success)" : "var(--state-danger)",
          flexShrink: 0,
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
          whiteSpace: "nowrap",
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
    case "in_progress": return "var(--state-info)";
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
