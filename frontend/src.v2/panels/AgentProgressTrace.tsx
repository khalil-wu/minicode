import { Wrench } from "lucide-react";
import { useMemo } from "react";
import { useAppStore } from "../stores";
import type { AgentProgressEntry } from "../stores/types";
import { StatusIcon as SharedStatusIcon } from "../components/icons";

interface AgentProgressTraceProps {
  mode?: "compact" | "full";
}

export const AgentProgressTrace = ({ mode = "full" }: AgentProgressTraceProps) => {
  const conversationId = useAppStore((s) => s.conversationId);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const entries = useMemo(() => {
    if (!conversationId?.trim()) return [];
    const scoped = agentProgress.filter((entry) => entry.conversationId === conversationId.trim());
    // Only show meaningful phases, not individual tool calls
    const meaningfulPhases = new Set(["planning", "verify", "recover", "plan", "approval", "subagent"]);
    return scoped.filter((entry) =>
      entry.status !== "info" &&
      entry.visibility !== "debug" &&
      !(entry.stage === "approval" && entry.toolName === "ask_user") &&
      (
        meaningfulPhases.has(entry.phase || "") ||
        entry.stage === "approval" ||
        entry.stage === "final" ||
        entry.phase === "final"
      )
    );
  }, [agentProgress, conversationId]);

  if (entries.length === 0) return null;

  const latestRunning = [...entries].reverse().find((entry) => entry.status === "running");
  const latest = latestRunning ?? entries[entries.length - 1];
  const completedTools = entries.filter((entry) => entry.stage === "tool" && entry.status === "completed").length;
  const recent = entries.slice(mode === "compact" ? -3 : -8).reverse();
  const latestMessage = displayMessage(latest);
  const latestDetail = displayDetail(latest, latestMessage);

  return (
    <section style={traceWrapStyle} aria-label="Agent execution trace">
      <div style={traceHeaderStyle}>
        <div className="flex-row-center" style={{ gap: 7, minWidth: 0 }}>
          <StatusIcon entry={latest} />
          <div style={{ minWidth: 0 }}>
            <div style={traceTitleStyle}>Activity</div>
            <div className="truncate" style={traceCurrentStyle} title={latestMessage}>{latestMessage}</div>
          </div>
        </div>
        <span className="shrink-0 font-mono" style={traceMetaStyle}>
          {latest.status === "running" ? "running" : completedTools ? `${completedTools} tools` : `${entries.length} steps`}
        </span>
      </div>
      {mode === "full" && latestDetail && <div style={traceDetailStyle}>{latestDetail}</div>}
      <div style={traceListStyle}>
        {recent.map((entry) => {
          const message = displayMessage(entry);
          const detail = displayDetail(entry, message);
          return (
            <div key={`${entry.conversationId ?? ""}-${entry.id}`} style={traceRowStyle}>
              <StatusIcon entry={entry} small />
              <span className="truncate" style={traceRowMessageStyle} title={detail || message}>
                {message}
              </span>
              <span className="font-mono" style={traceStageStyle}>{stageLabel(entry)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
};

const StatusIcon = ({ entry, small }: { entry: AgentProgressEntry; small?: boolean }) => {
  const size = small ? 12 : 14;
  if (entry.stage === "tool") return <Wrench size={size} color="var(--state-info)" className="shrink-0" />;
  if (entry.status === "failed") return <SharedStatusIcon status="failed" size={size} />;
  if (entry.status === "completed") return <SharedStatusIcon status="success" size={size} />;
  if (entry.status === "running") return <SharedStatusIcon status="running" size={size} />;
  if (entry.stage === "approval") return <SharedStatusIcon status="pending_approval" size={size} />;
  return <SharedStatusIcon status="pending" size={size} />;
};

function stageLabel(entry: AgentProgressEntry): string {
  if (entry.phase === "iteration") return entry.stage === "final" ? "final" : "working";
  if (entry.phase === "subagent") return "agent";
  if (entry.phase === "cache") return "cache";
  if (entry.phase === "model" || entry.phase === "orienting") return "think";
  if (entry.phase === "recover") return "recover";
  if (entry.stage === "planning") return "plan";
  if (entry.stage === "approval") return "wait";
  if (entry.stage === "final") return "final";
  return entry.stage;
}

function displayMessage(entry: AgentProgressEntry): string {
  return safeProgressSummary(entry.message || entry.summary || entry.label || stageLabel(entry));
}

function displayDetail(entry: AgentProgressEntry, visibleMessage = displayMessage(entry)): string {
  if (!entry.detail) return "";
  const detail = safeProgressSummary(entry.detail);
  return detail === visibleMessage ? "" : detail;
}

function safeProgressSummary(summary: string): string {
  const text = summary.replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > 88 ? `${text.slice(0, 85)}...` : text;
}

const traceWrapStyle: React.CSSProperties = {
  display: "grid",
  gap: 8,
  padding: 8,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const traceHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 8,
};

const traceTitleStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  textTransform: "uppercase",
};

const traceCurrentStyle: React.CSSProperties = {
  marginTop: 2,
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
};

const traceMetaStyle: React.CSSProperties = {
  color: "var(--accent-primary)",
  fontSize: "var(--text-xs)",
};

const traceDetailStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
};

const traceListStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
};

const traceRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "14px minmax(0, 1fr) auto",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
};

const traceRowMessageStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const traceStageStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-3xs)",
};
