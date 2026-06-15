import { memo, useMemo } from "react";
import { Activity, CheckCircle, Loader2, Pencil, XCircle } from "lucide-react";
import { useAppStore } from "../../stores";

/**
 * AgentStatusBar — a slim inline indicator above the message list
 * that tells the user what the model is currently doing.
 *
 * States:
 *   ● Thinking...           — model is generating text, no tool calls yet
 *   ● Executing: tool_name  — a tool is running
 *   ● Writing answer...     — final answer is being streamed
 *   ✓ Done                  — turn completed
 *   ✗ Failed                — turn failed
 *
 * Derived from: isStreaming, agentProgress, last message state.
 */
export const AgentStatusBar = memo(function AgentStatusBar() {
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationId = useAppStore((s) => s.conversationId);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const messages = useAppStore((s) => s.messages);

  const state = useMemo(() => {
    if (!isStreaming) {
      // Check if the last turn failed
      const lastMsg = messages[messages.length - 1];
      if (lastMsg?.terminalStatus === "failed") {
        return { kind: "failed" as const };
      }
      if (lastMsg?.terminalStatus === "completed") {
        return { kind: "done" as const };
      }
      return null; // idle — don't show the bar
    }

    // Find the latest running progress entry for this conversation
    const key = conversationId || "__active__";
    const entries = agentProgress.filter(
      (e) => e.conversationId === key && e.status === "running"
    );
    const latestRunning = entries[entries.length - 1];

    if (latestRunning) {
      if (latestRunning.stage === "tool" && latestRunning.toolName) {
        return {
          kind: "executing" as const,
          toolName: latestRunning.toolName,
          label: latestRunning.label,
        };
      }
      if (latestRunning.phase === "iteration") {
        return { kind: "thinking" as const, label: latestRunning.label };
      }
      if (latestRunning.phase === "recover") {
        return { kind: "recovering" as const, label: latestRunning.label };
      }
    }

    // Check if final answer is streaming
    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.isStreaming && !lastMsg?.isThinkingStreaming) {
      return { kind: "writing" as const };
    }

    return { kind: "thinking" as const };
  }, [isStreaming, conversationId, agentProgress, messages]);

  if (!state) return null;

  return (
    <div style={barStyle} data-testid="agent-status-bar">
      <StatusIcon kind={state.kind} />
      <span style={labelStyle}>
        <StatusLabel state={state} />
      </span>
    </div>
  );
});

function StatusIcon({ kind }: { kind: string }) {
  const size = 13;
  switch (kind) {
    case "thinking":
    case "recovering":
      return <Loader2 size={size} style={{ animation: "spin 1s linear infinite" }} color="var(--state-info)" />;
    case "executing":
      return <Activity size={size} color="var(--state-info)" />;
    case "writing":
      return <Pencil size={size} color="var(--state-info)" />;
    case "done":
      return <CheckCircle size={size} color="var(--state-success)" />;
    case "failed":
      return <XCircle size={size} color="var(--state-danger)" />;
    default:
      return null;
  }
}

function StatusLabel({ state }: { state: { kind: string; toolName?: string; label?: string } }) {
  switch (state.kind) {
    case "thinking":
      return <>{state.label || "Thinking..."}</>;
    case "executing":
      return (
        <>
          Executing: <strong>{formatToolName(state.toolName || "tool")}</strong>
        </>
      );
    case "writing":
      return <>Writing answer...</>;
    case "recovering":
      return <>{state.label || "Recovering..."}</>;
    case "done":
      return <>Done</>;
    case "failed":
      return <>Failed</>;
    default:
      return null;
  }
}

function formatToolName(name: string): string {
  // run_command → Run command, read_file → Read file
  return name
    .replace(/^mcp__\w+__/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Styles ───────────────────────────────────────────────────

const barStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 16px",
  fontSize: "var(--text-xs, 12px)",
  color: "var(--text-muted)",
  fontFamily: "var(--font-ui)",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  minHeight: 32,
  flexShrink: 0,
};

const labelStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

// CSS animation for the spinner
const styleSheet = document.createElement("style");
styleSheet.textContent = `@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`;
if (typeof document !== "undefined" && !document.querySelector("style[data-agent-status-bar]")) {
  styleSheet.setAttribute("data-agent-status-bar", "true");
  document.head.appendChild(styleSheet);
}
