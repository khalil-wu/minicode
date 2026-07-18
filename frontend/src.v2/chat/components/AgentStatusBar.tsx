import { memo, useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle, XCircle } from "lucide-react";
import { useAppStore } from "../../stores";
import { useAnimatedNumber } from "../../lib/use-animated-number";
import { deriveRuntimeSummary, runtimePhaseLabel, type RuntimePhase, type RuntimeSummary } from "../runtimeSummary";

/**
 * Runtime strip — a slim inline indicator above the message list that explains
 * the current turn in product language: phase, blocking reason, collaboration,
 * recovery, and only the tool hint that matters right now.
 */
const STALL_AFTER_MS = 3000;

function messageActivitySignature(message: ReturnType<typeof useAppStore.getState>["messages"][number] | undefined): number {
  if (!message) return 0;
  const contentLength = String(message.content ?? "").length;
  const blockLength = (message.blocks || []).reduce((total, block) => {
    if (block.type === "text" || block.type === "thinking" || block.type === "process") {
      return total + String(block.content ?? "").length;
    }
    if (block.type === "progress") {
      return total + String(block.message ?? block.summary ?? block.label ?? "").length;
    }
    if (block.type === "tool_call") {
      return total + String(block.record.outputPreview ?? block.record.summary ?? block.record.displaySummary ?? "").length;
    }
    return total;
  }, 0);
  return contentLength + blockLength;
}

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
  const conversationId = useAppStore((s) => s.conversationId);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const messages = useAppStore((s) => s.messages);
  const todos = useAppStore((s) => s.todos);
  const subagents = useAppStore((s) => s.subagents);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const lastUsage = useAppStore((s) => s.lastUsage);
  const inspectorEntries = useAppStore((s) => s.inspectorEntries);

  const summary = useMemo(() => deriveRuntimeSummary({
    conversationId,
    isStreaming,
    messages,
    todos,
    agentProgress,
    subagents,
    pendingApproval,
    approvalQueue,
    pendingDiffReview,
    pendingAskUser,
  }), [
    conversationId,
    isStreaming,
    messages,
    todos,
    agentProgress,
    subagents,
    pendingApproval,
    approvalQueue,
    pendingDiffReview,
    pendingAskUser,
  ]);

  // Signatures that change on real activity (progress events or streaming
  // text). Used to detect stalls.
  const progressSignature = agentProgress.length;
  const lastMsgForSig = messages[messages.length - 1];
  const contentSignature = messageActivitySignature(lastMsgForSig);
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
  const cacheHint = useMemo(() => {
    for (let index = inspectorEntries.length - 1; index >= 0; index -= 1) {
      const entry = inspectorEntries[index];
      if (entry.targetKind !== "cache" || entry.payload.type !== "cache.lookup" || !entry.payload.hit) continue;
      const savedMs = Number(entry.payload.estimated_saved_ms || 0);
      if (!Number.isFinite(savedMs) || savedMs < 300) continue;
      return `缓存加速约${Math.round(savedMs)}ms`;
    }
    return "";
  }, [inspectorEntries]);

  if (!summary) return null;

  const showElapsed = isStreaming && elapsed && summary.phase !== "failed" && summary.phase !== "done";
  const stalledActive = stalled;

  return (
    <div style={barStyle} data-testid="agent-status-bar">
      <StatusIcon phase={summary.phase} stalled={stalledActive} />
      <span style={phaseChipStyle(summary.phase)}>{runtimePhaseLabel(summary.phase)}</span>
      <span style={labelStyle} title={summary.detail || summary.headline}>
        <StatusLabel summary={summary} />
        {showElapsed && <span style={{ marginLeft: 8, opacity: 0.7 }}>· {elapsed}</span>}
        {summary.blockingLabel && <span style={mutedInlineStyle}>· {summary.blockingLabel}</span>}
        {summary.toolLabel && <span style={mutedInlineStyle}>· {summary.toolLabel}</span>}
        {summary.collaborationLabel && <span style={accentInlineStyle}>· {summary.collaborationLabel}</span>}
        {summary.recoveryLabel && <span style={recoveryInlineStyle}>· {summary.recoveryLabel}</span>}
        {cacheHint && <span style={recoveryInlineStyle}>· {cacheHint}</span>}
        {summary.attentionLabel && <span style={warningInlineStyle}>· {summary.attentionLabel}</span>}
        {stalledActive && <span style={warningInlineStyle}>· 等待超过 3s</span>}
        {showTokens && (
          <span style={{ marginLeft: 8, opacity: 0.7 }}>· {formatTokens(animatedTokens)} tok</span>
        )}
      </span>
    </div>
  );
});

function StatusIcon({ phase, stalled = false }: { phase: RuntimePhase; stalled?: boolean }) {
  const size = 13;
  const liveKinds = phase !== "failed" && phase !== "done";
  const color = stalled && liveKinds ? "var(--state-warning)" : "var(--state-info)";
  const transitionStyle = { transition: "color 600ms ease-in-out" };
  switch (phase) {
    case "thinking":
    case "waiting_user":
    case "recovering":
    case "executing":
    case "searching":
    case "finalizing":
      return <PulseStatusDot />;
    case "done":
      return <CheckCircle size={size} style={{ color: "var(--state-success)", ...transitionStyle }} />;
    case "failed":
      return <XCircle size={size} style={{ color: "var(--state-danger)", ...transitionStyle }} />;
    default:
      return null;
  }
}

function PulseStatusDot() {
  return (
    <span
      aria-hidden="true"
      className="agent-status-pulse-dot"
      style={{
        width: 5,
        height: 5,
        borderRadius: 999,
        background: "var(--text-muted)",
        boxShadow: "none",
        transition: "opacity 600ms ease-in-out",
        flexShrink: 0,
      }}
    />
  );
}

function StatusLabel({ summary }: { summary: RuntimeSummary }) {
  return (
    <>
      {summary.headline}
      {summary.detail && <span style={mutedInlineStyle}> · {summary.detail}</span>}
    </>
  );
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
  borderBottom: 0,
  background: "transparent",
  minHeight: 32,
  flexShrink: 0,
};
const labelStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  minWidth: 0,
};

const mutedInlineStyle: React.CSSProperties = {
  marginLeft: 8,
  opacity: 0.72,
};

const accentInlineStyle: React.CSSProperties = {
  marginLeft: 8,
  color: "var(--accent-primary)",
};

const warningInlineStyle: React.CSSProperties = {
  marginLeft: 8,
  color: "var(--state-warning)",
};

const recoveryInlineStyle: React.CSSProperties = {
  marginLeft: 8,
  color: "var(--state-success)",
};

const phaseChipStyle = (phase: RuntimePhase): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  height: 20,
  padding: "0 7px",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: phase === "waiting_user"
    ? "color-mix(in oklch, var(--state-warning) 10%, transparent)"
    : phase === "recovering"
      ? "color-mix(in oklch, var(--state-success) 9%, transparent)"
      : "var(--surface-soft)",
  color: phase === "waiting_user"
    ? "var(--state-warning)"
    : phase === "failed"
      ? "var(--state-danger)"
      : "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
  flexShrink: 0,
});

