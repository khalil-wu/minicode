import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Activity, CheckCircle, Loader2, Pencil, XCircle, Pause, Play } from "lucide-react";
import { useAppStore } from "../../stores";
import { useAnimatedNumber } from "../../lib/use-animated-number";

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
 * Polish vs. a bare spinner (mirrors cc's SpinnerAnimationRow):
 *   - shows an elapsed timer while streaming
 *   - surfaces the in-progress todo's activeForm ("Running tests", not "Thinking...")
 *   - shifts the indicator color after a few seconds of silence so the user
 *     can tell "thinking" from "stuck" (cc useStalledAnimation)
 */
const STALL_AFTER_MS = 3000;

function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

/**
 * Tracks elapsed time since streaming started and whether the stream has gone
 * quiet (no progress events or streaming text for STALL_AFTER_MS). The stall
 * flag drives a color shift so silence is visible.
 */
function useStreamingProgress(
  isStreaming: boolean,
  progressSignature: unknown,
  contentSignature: number,
): { elapsed: string; stalled: boolean } {
  const [elapsed, setElapsed] = useState("");
  const [stalled, setStalled] = useState(false);
  const startRef = useRef<number | null>(null);
  const lastActivityRef = useRef<number>(Date.now());

  // (Re)start the clock when streaming begins; record activity whenever the
  // progress list grows or the streaming message content changes.
  useEffect(() => {
    if (!isStreaming) {
      startRef.current = null;
      setElapsed("");
      setStalled(false);
      return;
    }
    if (startRef.current == null) startRef.current = Date.now();
    lastActivityRef.current = Date.now();
    setStalled(false);
  }, [isStreaming, progressSignature, contentSignature]);

  useEffect(() => {
    if (!isStreaming) return;
    const tick = () => {
      const start = startRef.current ?? Date.now();
      setElapsed(formatElapsed(Date.now() - start));
      setStalled(Date.now() - lastActivityRef.current > STALL_AFTER_MS);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [isStreaming]);

  return { elapsed, stalled };
}

export const AgentStatusBar = memo(function AgentStatusBar() {
  const isStreaming = useAppStore((s) => s.isStreaming);
  const isPaused = useAppStore((s) => s.isPaused);
  const pauseStreaming = useAppStore((s) => s.pauseStreaming);
  const resumeStreaming = useAppStore((s) => s.resumeStreaming);
  const conversationId = useAppStore((s) => s.conversationId);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const messages = useAppStore((s) => s.messages);
  const todos = useAppStore((s) => s.todos);
  const lastUsage = useAppStore((s) => s.lastUsage);

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

  // The in-progress todo's activeForm (e.g. "Running tests") — when present,
  // replaces the generic "Thinking..." so the bar states what's happening.
  const activeTodo = todos.find((t) => t.status === "in_progress");
  const activeLabel = activeTodo?.activeForm || activeTodo?.content;

  // Signatures that change on real activity (progress events or streaming
  // text). Used to detect stalls.
  const progressSignature = agentProgress.length;
  const lastMsgForSig = messages[messages.length - 1];
  const contentSignature = lastMsgForSig ? String(lastMsgForSig.content ?? "").length : 0;
  const { elapsed, stalled } = useStreamingProgress(
    isStreaming,
    progressSignature,
    contentSignature,
  );

  // Smooth token counter (cc SpinnerAnimationRow style): the displayed count
  // eases toward the real usage instead of jumping on each update.
  const targetTokens = lastUsage ? lastUsage.input + lastUsage.output : 0;
  const animatedTokens = useAnimatedNumber(targetTokens, isStreaming);
  const showTokens = isStreaming && targetTokens > 0;

  if (!state) return null;

  const showPauseButton = isStreaming && (state.kind === "thinking" || state.kind === "writing" || state.kind === "executing");
  const showElapsed = isStreaming && elapsed && (state.kind === "thinking" || state.kind === "executing" || state.kind === "writing");
  const stalledActive = stalled && !isPaused;

  return (
    <div style={barStyle} data-testid="agent-status-bar">
      <StatusIcon kind={state.kind} stalled={stalledActive} />
      <span style={labelStyle}>
        <StatusLabel state={state} activeLabel={activeLabel} />
        {showElapsed && <span style={{ marginLeft: 8, opacity: 0.7 }}>· {elapsed}</span>}
        {showTokens && (
          <span style={{ marginLeft: 8, opacity: 0.7 }}>· {formatTokens(animatedTokens)} tok</span>
        )}
        {isPaused && <span style={{ marginLeft: 8, color: "var(--state-warning)" }}>(Paused)</span>}
      </span>
      {showPauseButton && (
        <button
          type="button"
          onClick={isPaused ? () => resumeStreaming() : () => pauseStreaming()}
          title={isPaused ? "继续" : "暂停"}
          style={pauseButtonStyle}
        >
          {isPaused ? <Play size={12} /> : <Pause size={12} />}
        </button>
      )}
    </div>
  );
});

function StatusIcon({ kind, stalled = false }: { kind: string; stalled?: boolean }) {
  const size = 13;
  const liveKinds = kind === "thinking" || kind === "recovering" || kind === "writing" || kind === "executing";
  const color = stalled && liveKinds ? "var(--state-warning)" : "var(--state-info)";
  const transitionStyle = { transition: "color 600ms ease-in-out" };
  switch (kind) {
    case "thinking":
    case "recovering":
      return <Loader2 size={size} style={{ animation: "spin 1s linear infinite", color, ...transitionStyle }} />;
    case "executing":
      return <Activity size={size} style={{ color, ...transitionStyle }} />;
    case "writing":
      return <Pencil size={size} style={{ color, ...transitionStyle }} />;
    case "done":
      return <CheckCircle size={size} color="var(--state-success)" />;
    case "failed":
      return <XCircle size={size} color="var(--state-danger)" />;
    default:
      return null;
  }
}

function StatusLabel({ state, activeLabel }: { state: { kind: string; toolName?: string; label?: string }; activeLabel?: string }) {
  switch (state.kind) {
    case "thinking":
      return <>{activeLabel || state.label || "Thinking..."}</>;
    case "executing":
      return (
        <>
          Executing: <strong>{formatToolName(state.toolName || "tool")}</strong>
        </>
      );
    case "writing":
      return <>{activeLabel ? `${activeLabel}` : "Writing answer..."}</>;
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

const pauseButtonStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 24,
  height: 24,
  marginLeft: 12,
  padding: 0,
  background: "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm)",
  color: "var(--text-muted)",
  cursor: "pointer",
  transition: "var(--transition-fast)",
};

// CSS animation for the spinner
const styleSheet = document.createElement("style");
styleSheet.textContent = `@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`;
if (typeof document !== "undefined" && !document.querySelector("style[data-agent-status-bar]")) {
  styleSheet.setAttribute("data-agent-status-bar", "true");
  document.head.appendChild(styleSheet);
}
