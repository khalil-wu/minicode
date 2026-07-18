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
    const key = conversationId || "__active__";
    const direct = agentProgress.filter((entry) => entry.conversationId === key);
    const scoped = direct.length ? direct : agentProgress.filter((entry) => entry.conversationId === "__active__");
    // Only show meaningful phases, not individual tool calls
    const meaningfulPhases = new Set(["planning", "verify", "recover", "plan", "approval", "subagent", "workflow"]);
    return scoped.filter((entry) =>
      entry.status !== "info" &&
      entry.visibility !== "debug" &&
      !(entry.stage === "approval" && entry.toolName === "ask_user") &&
      (
        meaningfulPhases.has(entry.phase || "") ||
        entry.stage === "approval" ||
        entry.stage === "verification" ||
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
  if (entry.label && !hasRawIterationCounter(entry.label)) return entry.label.toLowerCase();
  if (entry.toolName) return toolLabel(entry.toolName);
  if (entry.phase === "iteration") return entry.stage === "final" ? "final" : "working";
  if (entry.phase === "subagent") return "agent";
  if (entry.phase === "workflow") return "flow";
  if (entry.phase === "cache") return "cache";
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
    const friendly = friendlyProgressMessage(entry);
    if (friendly) return friendly;
    if (entry.phase === "subagent" || entry.phase === "workflow") {
      return safeProgressSummary(entry.summary || entry.message || stageLabel(entry), stageLabel(entry));
    }
    if (entry.phase === "model" || entry.phase === "orienting" || entry.stage === "planning") return stageLabel(entry);
    return safeProgressSummary(entry.summary || entry.message || stageLabel(entry), stageLabel(entry));
  }
  if (entry.toolName === "todo_write") return "Updated tasks";
  if (entry.summary) {
    return safeProgressSummary(entry.summary, toolLabel(entry.toolName));
  }
  if (entry.toolName === "grep_files" && /^\s*(rg|grep)\b/i.test(entry.message)) {
    return shortProgressTarget(entry.message);
  }
  if (!/\b(read_file|grep_files|glob_files|list_files|run_command|write_file|edit_file)\b/.test(entry.message)) {
    return toolLabel(entry.toolName);
  }
  const message = entry.message.replace(/^(Running|Completed|Preparing)\s+/, "");
  const target = message.replace(entry.toolName, "").trim();
  const label = toolLabel(entry.toolName);
  return target ? `${label} ${shortProgressTarget(target)}` : label;
}

function displayDetail(entry: AgentProgressEntry, visibleMessage = displayMessage(entry)): string {
  if (!entry.detail) return "";
  const detail = safeProgressSummary(entry.detail, visibleMessage);
  return detail === visibleMessage ? "" : detail;
}

function friendlyProgressMessage(entry: AgentProgressEntry): string {
  if (entry.stage === "final" || entry.phase === "final") {
    return entry.status === "completed" ? "Final answer ready" : "Writing final answer";
  }
  if (entry.phase === "subagent" && entry.status === "failed") return "Subagent needs attention";
  if (entry.phase === "workflow" && entry.status === "failed") return "Workflow needs attention";
  const rawText = [
    entry.label,
    entry.summary,
    entry.message,
    entry.detail,
  ].filter(Boolean).join(" ");
  if (!hasRawIterationCounter(rawText)) return "";
  if (entry.phase === "model" || entry.phase === "orienting") return "Thinking";
  if (entry.stage === "planning" || entry.phase === "planning") return "Planning";
  if (entry.stage === "verification" || entry.phase === "verify") return "Checking work";
  return "Active";
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

function safeProgressSummary(summary: string, fallback = "Active"): string {
  const text = summary.replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (hasRawIterationCounter(text)) return fallback;
  if (/允许的路径|禁止的路径|allowed paths|forbidden path|outside allowed|outside trusted/i.test(text)) {
    return "Outside allowed workspace";
  }
  if (/content_hash|saved as an artifact|approx \d+ tokens|<!DOCTYPE|<html\b|expected_hash|actual_hash/i.test(text)) {
    return "Active";
  }
  return text.length > 88 ? `${text.slice(0, 85)}...` : text;
}

function hasRawIterationCounter(value: string): boolean {
  return /\b(?:iter(?:ation)?\.?)\s*\d+\s*(?:\/|of)\s*\d+\b|\biteration\s+\d+\b/i.test(value);
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
  fontSize: "10px",
};
