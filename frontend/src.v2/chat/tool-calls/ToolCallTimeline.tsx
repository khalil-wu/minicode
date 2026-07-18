import { useMemo } from "react";
import { useAppStore } from "../../stores";
import { getToolCallsFromMessage } from "../../lib/content-blocks";
import type { CSSProperties } from "react";
import type { AgentProgressEntry, ChatMessage } from "../../stores/types";

export type TimelinePhase = "context" | "model" | "tool" | "approval" | "subagent" | "workflow" | "cache" | "recovery" | "final";

export type TimelineItem = {
  id: string;
  phase: TimelinePhase;
  label: string;
  summary?: string;
  status: "running" | "completed" | "failed" | "blocked" | "partial" | "info";
  startedAt: number;
  finishedAt?: number;
  toolName?: string;
};

export type RunReplayEvent = {
  kind: "minicode_run_replay_event";
  schema_version: 1;
  seq: number;
  event: string;
  phase: TimelinePhase;
  status: TimelineItem["status"];
  label: string;
  summary: string;
  tool_name: string;
  started_at: number;
  finished_at: number | null;
  duration_ms: number | null;
};

export type RunReplaySummary = {
  events: number;
  phases: TimelinePhase[];
  coveragePercent: number;
  failedOrBlocked: number;
  running: number;
  firstStartedAt: number | null;
  lastFinishedAt: number | null;
  spanMs: number | null;
  outcome: "completed" | "needs_attention" | "running" | "empty";
};

const PHASE_LABELS: Record<TimelinePhase, string> = {
  context: "Context",
  model: "Model",
  tool: "Tools",
  approval: "Approval",
  subagent: "Subagents",
  workflow: "Workflow",
  cache: "Cache",
  recovery: "Recovery",
  final: "Final",
};

const PHASE_ORDER: TimelinePhase[] = ["context", "model", "tool", "approval", "subagent", "workflow", "cache", "recovery", "final"];

export const ToolCallTimeline = ({ limit = 40 }: { limit?: number } = {}) => {
  const messages = useAppStore((s) => s.messages);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const allItems = useMemo(() => buildRunTimelineItems(messages, agentProgress), [messages, agentProgress]);
  const items = limit > 0 && allItems.length > limit ? allItems.slice(-limit) : allItems;

  if (items.length === 0) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", padding: 12 }}>
        No runtime timeline events in this conversation yet.
      </div>
    );
  }

  const startedAt = Math.min(...items.map((item) => item.startedAt));
  const finishedAt = Math.max(...items.map((item) => item.finishedAt ?? item.startedAt + 500));
  const span = Math.max(1, finishedAt - startedAt);
  const grouped = groupTimelineItems(items);

  return (
    <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
      <div
        style={{
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
          marginBottom: 6,
        }}
      >
        Run trace · {items.length}{allItems.length > items.length ? ` of ${allItems.length}` : ""} event{allItems.length === 1 ? "" : "s"} ·{" "}
        {(span / 1000).toFixed(1)}s span
      </div>
      {PHASE_ORDER.filter((phase) => grouped[phase]?.length).map((phase) => (
        <section key={phase} style={{ display: "grid", gap: 6 }}>
          <div style={phaseHeaderStyle}>
            <span>{PHASE_LABELS[phase]}</span>
            <span>{grouped[phase].length}</span>
          </div>
          {grouped[phase].map((item) => {
            const status = statusLabel(item.status);
            const summary = item.summary || "";
            const duration = item.finishedAt
              ? `${((item.finishedAt - item.startedAt) / 1000).toFixed(2)}s`
              : item.status === "running"
                ? "running"
                : "";
            return (
              <div
                key={item.id}
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
                    background: statusColor(item.status),
                    opacity: item.status === "running" ? 1 : 0.72,
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
                    title={item.toolName || item.label}
                  >
                    {item.label}
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
                      color: item.status === "failed" ? "var(--state-danger)" : "var(--text-secondary)",
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
        </section>
      ))}
    </div>
  );
};

export function buildRunTimelineItems(
  messages: ChatMessage[],
  progress: AgentProgressEntry[],
  options: { includeDebugCache?: boolean } = {},
): TimelineItem[] {
  const toolItems = messages.flatMap((message) =>
    getToolCallsFromMessage(message).map((tc) => ({
      id: `tool:${tc.id}`,
      phase: phaseForTool(tc.name),
      label: tc.name,
      summary: tc.displaySummary || tc.inputSummary || tc.summary || tc.contentPreview || "",
      status: normalizeToolStatus(tc.status),
      startedAt: tc.startedAt || message.timestamp || Date.now(),
      finishedAt: tc.finishedAt,
      toolName: tc.name,
    } satisfies TimelineItem)),
  );

  const progressItems = progress
    .filter((entry) => entry.visibility !== "debug" || (options.includeDebugCache === true && entry.phase === "cache"))
    .map((entry) => ({
      id: `progress:${entry.id}`,
      phase: phaseForProgress(entry),
      label: entry.label || entry.message || PHASE_LABELS[phaseForProgress(entry)],
      summary: entry.summary || entry.detail,
      status: entry.status === "info" ? "info" : entry.status,
      startedAt: entry.timestamp || Date.now(),
      toolName: entry.toolName,
    } satisfies TimelineItem));

  return [...progressItems, ...toolItems].sort((a, b) => a.startedAt - b.startedAt);
}

export function runTimelineExportJsonl(messages: ChatMessage[], progress: AgentProgressEntry[]): string {
  return buildRunTimelineItems(messages, progress, { includeDebugCache: true })
    .map((item) => JSON.stringify({
      kind: "minicode_run_timeline_event",
      phase: item.phase,
      status: item.status,
      label: item.label,
      summary: item.summary || "",
      tool_name: item.toolName || "",
      started_at: item.startedAt,
      finished_at: item.finishedAt ?? null,
    }))
    .join("\n");
}

export function runTimelineReplayJsonl(messages: ChatMessage[], progress: AgentProgressEntry[]): string {
  return buildRunReplayEvents(messages, progress)
    .map((event) => JSON.stringify(event))
    .join("\n");
}

export function buildRunReplayEvents(messages: ChatMessage[], progress: AgentProgressEntry[]): RunReplayEvent[] {
  return buildRunTimelineItems(messages, progress, { includeDebugCache: true }).map((item, index) => ({
    kind: "minicode_run_replay_event",
    schema_version: 1,
    seq: index + 1,
    event: `${item.phase}.${item.status}`,
    phase: item.phase,
    status: item.status,
    label: item.label,
    summary: item.summary || "",
    tool_name: item.toolName || "",
    started_at: item.startedAt,
    finished_at: item.finishedAt ?? null,
    duration_ms: item.finishedAt ? Math.max(0, item.finishedAt - item.startedAt) : null,
  }));
}

export function buildRunReplaySummary(events: RunReplayEvent[]): RunReplaySummary {
  if (events.length === 0) {
    return {
      events: 0,
      phases: [],
      coveragePercent: 0,
      failedOrBlocked: 0,
      running: 0,
      firstStartedAt: null,
      lastFinishedAt: null,
      spanMs: null,
      outcome: "empty",
    };
  }
  const phases = PHASE_ORDER.filter((phase) => events.some((event) => event.phase === phase));
  const timed = events.filter((event) => Number.isFinite(event.started_at) && (event.finished_at == null || Number.isFinite(event.finished_at))).length;
  const failedOrBlocked = events.filter((event) => event.status === "failed" || event.status === "blocked" || event.status === "partial").length;
  const running = events.filter((event) => event.status === "running").length;
  const firstStartedAt = Math.min(...events.map((event) => event.started_at));
  const finishedValues = events.map((event) => event.finished_at ?? event.started_at).filter((value) => Number.isFinite(value));
  const lastFinishedAt = finishedValues.length > 0 ? Math.max(...finishedValues) : null;
  return {
    events: events.length,
    phases,
    coveragePercent: Math.round((timed / events.length) * 100),
    failedOrBlocked,
    running,
    firstStartedAt,
    lastFinishedAt,
    spanMs: lastFinishedAt != null ? Math.max(0, lastFinishedAt - firstStartedAt) : null,
    outcome: running > 0 ? "running" : failedOrBlocked > 0 ? "needs_attention" : "completed",
  };
}

function groupTimelineItems(items: TimelineItem[]): Record<TimelinePhase, TimelineItem[]> {
  return items.reduce((acc, item) => {
    acc[item.phase].push(item);
    return acc;
  }, {
    context: [],
    model: [],
    tool: [],
    approval: [],
    subagent: [],
    workflow: [],
    cache: [],
    recovery: [],
    final: [],
  } as Record<TimelinePhase, TimelineItem[]>);
}

function phaseForProgress(entry: AgentProgressEntry): TimelinePhase {
  if (entry.phase === "approval" || entry.stage === "approval") return "approval";
  if (entry.phase === "subagent") return "subagent";
  if (entry.phase === "workflow") return "workflow";
  if (entry.phase === "cache") return "cache";
  if (entry.phase === "recover") return "recovery";
  if (entry.phase === "tool" || entry.stage === "tool") return "tool";
  if (entry.phase === "final" || entry.stage === "final" || entry.stage === "verification") return "final";
  if (entry.phase === "model" || entry.phase === "planning" || entry.stage === "planning") return "model";
  return "context";
}

function phaseForTool(name: string): TimelinePhase {
  if (/(?:approve|review|permission)/i.test(name)) return "approval";
  return "tool";
}

function normalizeToolStatus(status: string): TimelineItem["status"] {
  if (status === "success") return "completed";
  if (status === "failed" || status === "blocked" || status === "partial" || status === "running") return status;
  return "info";
}

function statusLabel(status: TimelineItem["status"]): string {
  switch (status) {
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "blocked":
      return "Blocked";
    case "partial":
      return "Partial";
    case "running":
      return "Running";
    default:
      return "Info";
  }
}

function statusColor(status: TimelineItem["status"]): string {
  if (status === "completed") return "var(--state-success)";
  if (status === "failed") return "var(--state-danger)";
  if (status === "blocked" || status === "partial") return "var(--state-warning)";
  if (status === "info") return "var(--text-muted)";
  return "var(--accent-primary)";
}

const phaseHeaderStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: 0,
};
