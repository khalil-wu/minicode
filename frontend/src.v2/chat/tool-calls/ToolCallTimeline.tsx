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
  const t0 = Math.min(...allCalls.map((c) => c.startedAt));
  const tEnd = Math.max(...allCalls.map((c) => c.finishedAt ?? c.startedAt + 500));
  const span = Math.max(1, tEnd - t0);

  return (
    <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 4 }}>
      <div
        style={{
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
          marginBottom: 6,
        }}
      >
        Timeline · {allCalls.length} call{allCalls.length === 1 ? "" : "s"} ·{" "}
        {(span / 1000).toFixed(1)}s span
      </div>
      {allCalls.map((c) => {
        const start = ((c.startedAt - t0) / span) * 100;
        const end = (((c.finishedAt ?? tEnd) - t0) / span) * 100;
        const width = Math.max(1, end - start);
        return (
          <div
            key={c.id}
            style={{
              display: "grid",
              gridTemplateColumns: "120px 1fr 60px",
              alignItems: "center",
              gap: 8,
              fontSize: "var(--text-xs)",
            }}
          >
            <span
              style={{
                fontFamily: "var(--font-mono)",
                color: "var(--accent-primary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={c.name}
            >
              {c.name}
            </span>
            <div
              style={{
                position: "relative",
                height: 8,
                background: "var(--surface-soft)",
                borderRadius: 4,
              }}
            >
              <div
                style={{
                  position: "absolute",
                  left: `${start}%`,
                  width: `${width}%`,
                  top: 0,
                  bottom: 0,
                  borderRadius: 4,
                  background:
                    c.status === "success"
                      ? "var(--state-success)"
                      : c.status === "failed"
                        ? "var(--state-danger)"
                        : c.status === "blocked"
                          ? "var(--state-warning)"
                          : c.status === "partial"
                            ? "var(--state-warning)"
                            : "var(--accent-primary)",
                  opacity: 0.8,
                }}
              />
            </div>
            <span style={{ color: "var(--text-muted)", textAlign: "right" }}>
              {c.finishedAt
                ? `${((c.finishedAt - c.startedAt) / 1000).toFixed(2)}s`
                : c.status === "running"
                  ? "…"
                  : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
};
