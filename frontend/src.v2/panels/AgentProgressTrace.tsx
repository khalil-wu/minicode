import { Activity, CheckCircle, Circle, Clock, Wrench, XCircle } from "lucide-react";
import { useMemo } from "react";
import { useAppStore } from "../stores";
import type { AgentProgressEntry } from "../stores/types";

interface AgentProgressTraceProps {
  mode?: "compact" | "full";
}

export const AgentProgressTrace = ({ mode = "full" }: AgentProgressTraceProps) => {
  const conversationId = useAppStore((s) => s.conversationId);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const entries = useMemo(() => {
    const key = conversationId || "__active__";
    const direct = agentProgress.filter((entry) => entry.conversationId === key);
    const scoped = direct.length ? direct : agentProgress.filter((entry) => entry.conversationId === "__active__");
    return scoped.filter((entry) =>
      entry.status !== "info" &&
      entry.visibility !== "debug" &&
      !(entry.stage === "approval" && entry.toolName === "ask_user")
    );
  }, [agentProgress, conversationId]);

  if (entries.length === 0) return null;

  const latestRunning = [...entries].reverse().find((entry) => entry.status === "running");
  const latest = latestRunning ?? entries[entries.length - 1];
  const completedTools = entries.filter((entry) => entry.stage === "tool" && entry.status === "completed").length;
  const failedCount = entries.filter((entry) => entry.status === "failed").length;
  const recent = entries.slice(mode === "compact" ? -3 : -8).reverse();

  return (
    <section style={traceWrapStyle} aria-label="Agent execution trace">
      <div style={traceHeaderStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, minWidth: 0 }}>
          <StatusIcon entry={latest} />
          <div style={{ minWidth: 0 }}>
            <div style={traceTitleStyle}>Progress</div>
            <div style={traceCurrentStyle} title={latest.message}>{displayMessage(latest)}</div>
          </div>
        </div>
        <span style={traceMetaStyle}>
          {latest.status === "running" ? "running" : failedCount ? `${failedCount} failed` : `${completedTools} tools`}
        </span>
      </div>
      {mode === "full" && latest.detail && <div style={traceDetailStyle}>{latest.detail}</div>}
      <div style={traceListStyle}>
        {recent.map((entry) => (
          <div key={`${entry.conversationId ?? ""}-${entry.id}`} style={traceRowStyle}>
            <StatusIcon entry={entry} small />
            <span style={traceRowMessageStyle} title={entry.detail || entry.message}>
              {displayMessage(entry)}
            </span>
            <span style={traceStageStyle}>{stageLabel(entry)}</span>
          </div>
        ))}
      </div>
    </section>
  );
};

const StatusIcon = ({ entry, small }: { entry: AgentProgressEntry; small?: boolean }) => {
  const size = small ? 12 : 14;
  if (entry.status === "failed") return <XCircle size={size} color="var(--state-danger)" style={{ flexShrink: 0 }} />;
  if (entry.status === "completed") return <CheckCircle size={size} color="var(--state-success)" style={{ flexShrink: 0 }} />;
  if (entry.stage === "tool") return <Wrench size={size} color="var(--state-info)" style={{ flexShrink: 0 }} />;
  if (entry.status === "running") return <Activity size={size} color="var(--state-info)" style={{ flexShrink: 0 }} />;
  if (entry.stage === "approval") return <Clock size={size} color="var(--state-warning)" style={{ flexShrink: 0 }} />;
  return <Circle size={size} color="var(--text-muted)" style={{ flexShrink: 0 }} />;
};

function stageLabel(entry: AgentProgressEntry): string {
  if (entry.label) return entry.label.toLowerCase();
  if (entry.toolName) return toolLabel(entry.toolName);
  if (entry.phase === "model" || entry.phase === "orienting") return "think";
  if (entry.phase === "recover") return "recover";
  if (entry.stage === "planning") return "plan";
  if (entry.stage === "approval") return "wait";
  if (entry.stage === "verification") return "verify";
  if (entry.stage === "final") return "final";
  return entry.stage;
}

function displayMessage(entry: AgentProgressEntry): string {
  if (entry.stage !== "tool" || !entry.toolName) {
    if (entry.phase === "model" || entry.phase === "orienting" || entry.stage === "planning") return stageLabel(entry);
    return safeProgressSummary(entry.summary || entry.message || stageLabel(entry));
  }
  if (entry.toolName === "todo_write") return "Updated tasks";
  if (entry.summary) {
    return safeProgressSummary(entry.summary);
  }
  if (!/\b(read_file|grep_files|glob_files|list_files|run_command|write_file|edit_file)\b/.test(entry.message)) {
    return toolLabel(entry.toolName);
  }
  const message = entry.message.replace(/^(Running|Completed|Preparing)\s+/, "");
  const target = message.replace(entry.toolName, "").trim();
  const label = toolLabel(entry.toolName);
  return target ? `${label} ${shortProgressTarget(target)}` : label;
}

function toolLabel(toolName: string): string {
  switch (toolName) {
    case "read_file":
      return "Read";
    case "list_files":
      return "Get";
    case "grep":
    case "grep_files":
      return "rg";
    case "glob":
    case "glob_files":
      return "glob";
    case "run_command":
      return "shell";
    case "write_file":
      return "Write";
    case "edit_file":
      return "Edit";
    case "todo_write":
      return "Tasks";
    default:
      return toolName;
  }
}

function safeProgressSummary(summary: string): string {
  const text = summary.replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (/允许的路径|禁止的路径|allowed paths|forbidden path|outside allowed|outside trusted/i.test(text)) {
    return "Outside allowed workspace";
  }
  if (/content_hash|saved as an artifact|approx \d+ tokens|<!DOCTYPE|<html\b|expected_hash|actual_hash/i.test(text)) {
    return "Working";
  }
  return text.length > 88 ? `${text.slice(0, 85)}...` : text;
}

function shortProgressTarget(value: string): string {
  const text = value.replace(/\s+/g, " ").trim();
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
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const traceMetaStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--accent-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
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
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const traceStageStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "10px",
  fontFamily: "var(--font-mono)",
};
