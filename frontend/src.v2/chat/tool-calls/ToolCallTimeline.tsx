import { useAppStore } from "../../stores";
import { getToolCallsFromMessage } from "../../lib/content-blocks";

export const ToolCallTimeline = () => {
  const messages = useAppStore((s) => s.messages);
  const allCalls = messages.flatMap((m) =>
    getToolCallsFromMessage(m).map((tc) => ({ ...tc, messageId: m.id })),
  );
  if (allCalls.length === 0) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", padding: 12 }}>
        No tool calls in this conversation yet.
      </div>
    );
  }
  const startedAt = Math.min(...allCalls.map((c) => c.startedAt));
  const finishedAt = Math.max(...allCalls.map((c) => c.finishedAt ?? c.startedAt + 500));
  const span = Math.max(1, finishedAt - startedAt);

  return (
    <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        style={{
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
          marginBottom: 6,
        }}
      >
        Activity · {allCalls.length} call{allCalls.length === 1 ? "" : "s"} ·{" "}
        {(span / 1000).toFixed(1)}s span
      </div>
      {allCalls.map((c) => {
        const status = c.status === "success"
          ? "Completed"
          : c.status === "failed"
            ? "Failed"
            : c.status === "blocked"
              ? "Blocked"
              : c.status === "partial"
                ? "Partial"
                : "Running";
        const summary = c.displaySummary || c.inputSummary || c.summary || c.contentPreview || "";
        const duration = c.finishedAt
          ? `${((c.finishedAt - c.startedAt) / 1000).toFixed(2)}s`
          : c.status === "running"
            ? "running"
            : "";
        return (
          <div
            key={c.id}
            style={{
              display: "grid",
              gridTemplateColumns: "8px minmax(0, 1fr) auto",
              alignItems: "center",
              gap: 10,
              minHeight: 42,
              padding: "8px 10px",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-sm, 6px)",
              background: "var(--surface-base)",
              fontSize: "var(--text-xs)",
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 7,
                height: 7,
                borderRadius: 999,
                background:
                  c.status === "success"
                    ? "var(--state-success)"
                    : c.status === "failed"
                      ? "var(--state-danger)"
                      : c.status === "blocked" || c.status === "partial"
                        ? "var(--state-warning)"
                        : "var(--accent-primary)",
                opacity: c.status === "running" ? 1 : 0.72,
              }}
            />
            <div style={{ minWidth: 0, display: "grid", gap: 2 }}>
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-primary)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={c.name}
              >
                {c.name}
              </span>
              {summary && (
                <span
                  style={{
                    color: "var(--text-muted)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={summary}
                >
                  {summary}
                </span>
              )}
            </div>
            <div style={{ display: "grid", gap: 2, justifyItems: "end", color: "var(--text-muted)" }}>
              <div
                style={{
                  color: c.status === "failed" ? "var(--state-danger)" : "var(--text-secondary)",
                  fontWeight: 600,
                }}
              >
                {status}
              </div>
              {duration && <span>{duration}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
};
