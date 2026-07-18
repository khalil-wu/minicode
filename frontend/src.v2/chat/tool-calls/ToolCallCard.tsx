import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  TerminalSquare,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { getWebSocket } from "../../hooks/useWebSocket";
import { useSharedSecondTick } from "../../lib/shared-tick";
import type { ToolCallRecord, ToolCallStatus } from "../../lib/tool-call-reducer";
import {
  extractToolFilePath,
  shortToolPath,
  summarizeToolInput,
  ToolGlyph,
  toolDisplayName,
} from "../toolUtils";
import { StatusIcon, statusIconColor } from "../../components/icons";
import { useAppStore } from "../../stores";
import type { ViewMode } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import { openWebInPreview } from "../openWebInPreview";
import { getToolRenderer } from "./toolRendererRegistry";
import {
  CommandResultView,
  registerBuiltInToolRenderers,
  WebSearchResultsView,
} from "./renderers";

registerBuiltInToolRenderers();

const LOCAL_URL_RE = /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:[/?#][^\s'"<>]*)?/i;

const InlineDiff = ({ patch }: { patch: string }) => {
  const lines = useMemo(() => patch.split("\n").slice(0, 200), [patch]);
  return (
    <div className="py-2 border-b border-[var(--border-subtle)] font-mono text-xs leading-normal overflow-x-auto">
      {lines.map((line, i) => {
        let bg = "transparent";
        let color = "var(--text-secondary)";
        if (line.startsWith("+") && !line.startsWith("+++")) {
          bg = "color-mix(in oklch, var(--state-success) 12%, transparent)";
          color = "var(--state-success)";
        } else if (line.startsWith("-") && !line.startsWith("---")) {
          bg = "color-mix(in oklch, var(--state-danger) 12%, transparent)";
          color = "var(--state-danger)";
        } else if (line.startsWith("@@")) {
          color = "var(--accent-primary)";
        }
        return (
          <div key={i} className="px-3.5 whitespace-pre" style={{ background: bg, color }}>
            {line}
          </div>
        );
      })}
      {patch.split("\n").length > 200 && (
        <div className="py-1 px-3.5 text-[var(--text-muted)] italic">
          ... {patch.split("\n").length - 200} more lines
        </div>
      )}
    </div>
  );
};

function formatToolResult(summary: string): string {
  const trimmed = summary.trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (parsed && typeof parsed === "object") {
      const record = parsed as Record<string, unknown>;
      const preferred = record.summary ?? record.message ?? record.output ?? record.result ?? record.error;
      if (typeof preferred === "string") return preferred;
      const keys = Object.keys(record).slice(0, 5);
      if (keys.length > 0) return keys.map((key) => `${key}: ${String(record[key]).slice(0, 80)}`).join(" | ");
    }
  } catch {
    /* not json */
  }
  return trimmed.length > 1200 ? `${trimmed.slice(0, 1200)}\n...` : trimmed;
}

function isCommandTool(name: string): boolean {
  return /(?:run_command|terminal|shell|bash|powershell|cmd)/i.test(name);
}

function isCommandRecord(record: ToolCallRecord): boolean {
  return record.resultKind === "command" || isCommandTool(record.name);
}

function isWebSearchTool(name: string): boolean {
  return name === "web_search" || name === "search_web";
}

function isWebRecord(record: ToolCallRecord): boolean {
  return record.resultKind === "web" || isWebSearchTool(record.name);
}

function evidenceLabel(record: ToolCallRecord): string {
  const parts: string[] = [];
  if (record.evidenceType === "candidate") parts.push("Candidate source");
  else if (record.evidenceType === "fetched") parts.push("Fetched evidence");
  else if (record.evidenceType) parts.push(record.evidenceType);
  if (record.extractionStatus) parts.push(`extraction: ${record.extractionStatus}`);
  return parts.join(" - ");
}

const RawJsonDetails = ({ args }: { args: Record<string, unknown> }) => {
  if (Object.keys(args).length === 0) return null;
  return (
    <details className="border-t border-[var(--border-subtle)] pt-2">
      <summary className="cursor-pointer text-[var(--text-muted)] text-xs inline-flex items-center gap-[5px]">
        Raw JSON
      </summary>
      <pre className="mt-[7px] mb-0 p-2 border border-[var(--border-subtle)] rounded bg-[var(--surface-soft)] text-[var(--text-secondary)] whitespace-pre-wrap break-words">
        {JSON.stringify(args, null, 2)}
      </pre>
    </details>
  );
};

function normalizeLocalUrl(url: string): string {
  return url.replace(/^https?:\/\/0\.0\.0\.0/i, (prefix) => prefix.replace("0.0.0.0", "localhost"));
}

const Spinner = () => (
  <span className="spinner w-3 h-3 shrink-0" />
);

function toolVerb(name: string): string {
  if (name.includes("read") || name.includes("file_read")) return "Reading";
  if (name.includes("write") || name.includes("file_write") || name.includes("edit")) return "Writing";
  if (name.includes("grep") || name.includes("glob") || name.includes("search")) return "Searching";
  if (name.includes("command") || name.includes("terminal") || name.includes("bash")) return "Running";
  if (name.includes("git")) return "Checking";
  if (name.includes("web") || name.includes("fetch")) return "Fetching";
  if (name.includes("list") || name.includes("todo")) return "Planning";
  if (name.includes("memory") || name.includes("recall")) return "Recalling";
  return "Running";
}

function toolCategory(record: ToolCallRecord): "read" | "search" | "write" | "command" | "web" | "git" | "approval" | "generic" {
  const name = record.name.toLowerCase();
  const kind = String(record.resultKind || record.activityKind || "").toLowerCase();
  if (record.projection === "approval" || /approval|permission/.test(`${name} ${kind}`)) return "approval";
  if (/git/.test(name)) return "git";
  if (/(?:write|edit|patch|delete|remove|create|move|rename|save|apply_patch)/.test(name) || kind === "edit") return "write";
  if (/(?:run_command|terminal|shell|bash|powershell|cmd|command)/.test(name) || kind === "command") return "command";
  if (/(?:web|browser|fetch|crawl|scrape)/.test(name) || kind === "web") return "web";
  if (/(?:grep|glob|search|find|list)/.test(name) || kind === "search") return "search";
  if (/(?:read|cat|file)/.test(name) || kind === "file") return "read";
  return "generic";
}

function phaseLabel(record: ToolCallRecord): string {
  if (record.status === "pending") return "Preparing";
  if (record.status === "running") {
    const category = toolCategory(record);
    if (category === "search" || category === "web" || category === "read") return "Gathering";
    if (category === "write") return "Changing files";
    if (category === "command") return "Executing";
    if (category === "approval") return "Waiting approval";
    return "Running";
  }
  if (record.status === "failed" || record.status === "timeout") return "Needs attention";
  if (record.status === "blocked") return "Blocked";
  if (record.status === "partial") return "Partial";
  return "Done";
}

function hasStderr(record: ToolCallRecord): boolean {
  return Boolean(String(record.stderrPreview || "").trim()) || /stderr/i.test(String(record.summary || ""));
}

function finishedDurationMs(record: ToolCallRecord): number {
  if (typeof record.durationMs === "number") return record.durationMs;
  if (record.finishedAt && record.startedAt) return record.finishedAt - record.startedAt;
  return 0;
}

function shouldAutoOpen(record: ToolCallRecord, viewMode: ViewMode, now: number): boolean {
  if (viewMode === "verbose") return true;
  if (record.requiresAttention) return true;
  if (record.status === "failed" || record.status === "blocked" || record.status === "timeout" || record.status === "partial") return true;
  if (record.diff && (record.diff.plus > 0 || record.diff.minus > 0 || record.diff.patch)) return true;
  if (hasStderr(record)) return true;
  if (finishedDurationMs(record) > 2000) return true;
  if ((record.status === "running" || record.status === "pending") && now - (record.startedAt ?? now) > 2000) return true;
  return false;
}

function waitingOnLabel(record: ToolCallRecord, longRunning: boolean): string {
  if (record.status === "pending") return "waiting on dispatch";
  if (record.status !== "running") return "";
  const category = toolCategory(record);
  if (category === "web") return "waiting on network";
  if (category === "command") return longRunning ? "waiting on process output" : "waiting on command";
  if (category === "search" || category === "read") return "waiting on filesystem";
  return longRunning ? "waiting on tool output" : "waiting on tool";
}

function recoveryGuidance(record: ToolCallRecord): string {
  if (record.status !== "failed" && record.status !== "timeout" && record.status !== "blocked" && record.status !== "partial") return "";
  if (record.recoverable) return record.userSummary || "This looks recoverable; MiniCode can continue with a narrower retry or use completed results.";
  if (record.projection === "approval") return "This requires your decision before the run can continue.";
  if (record.status === "timeout") return "The tool timed out. Completed output is preserved; retry with a smaller scope if needed.";
  return record.userSummary || "Review the result before trusting the final answer.";
}

const SmallAction = ({
  children,
  label,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    onClick={(event) => {
      event.stopPropagation();
      onClick();
    }}
    className="h-[22px] inline-flex items-center gap-[5px] px-[7px] border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--text-secondary)] cursor-pointer text-xs shrink-0"
  >
    {children}
  </button>
);

export const ToolCallCard = memo(({ record, viewMode = "normal", compact = false }: { record: ToolCallRecord; viewMode?: ViewMode; compact?: boolean }) => {
  const [open, setOpen] = useState(() => shouldAutoOpen(record, viewMode, Date.now()));
  const [outputExpanded, setOutputExpanded] = useState(false);
  const userToggled = useRef(false);
  const isActive = record.status === "running" || record.status === "pending";
  // Shared 1s tick — one interval for all running tool cards, not N.
  const now = useSharedSecondTick(isActive);
  useEffect(() => {
    if (viewMode === "verbose") {
      setOpen(true);
      return;
    }
    if (!userToggled.current) {
      setOpen(shouldAutoOpen(record, viewMode, now));
    }
  }, [compact, record, record.status, viewMode, now]);
  const duration =
    record.finishedAt && record.startedAt
      ? `${((record.finishedAt - record.startedAt) / 1000).toFixed(1)}s`
      : record.status === "running"
        ? formatElapsed(now - (record.startedAt ?? now))
        : "";
  const longRunning = record.status === "running" && now - (record.startedAt ?? now) > 10_000;
  const waitingOn = waitingOnLabel(record, longRunning);

  const filePath = extractToolFilePath(record.args);
  const inputSummary = record.inputSummary || summarizeToolInput(record.name, record.args);
  const previewUrl = record.summary ? normalizeLocalUrl(record.summary.match(LOCAL_URL_RE)?.[0] ?? "") : "";
  const rawResultSummary = record.summary ? formatToolResult(record.summary) : "";
  const resultSummary = record.displaySummary || rawResultSummary;
  const evidence = evidenceLabel(record);
  const SpecificRenderer = getToolRenderer(record.name);
  const guidance = recoveryGuidance(record);

  const copyResult = () => {
    const text = resultSummary || record.summary || "";
    if (!text) return;
    void navigator.clipboard?.writeText(text)
      .then(() => pushToast("Tool result copied", "success", 1200))
      .catch(() => pushToast("Copy failed", "error", 1800));
  };

  const openArtifact = () => {
    if (!record.artifactId) return;
    const store = useAppStore.getState();
    store.setPreviewArtifact(null);
    store.addPanel({
      id: `artifact-${record.artifactId}`,
      kind: "preview",
      label: "Artifact",
    });
    getWebSocket()?.send({ type: "read_artifact", artifact_id: record.artifactId });
  };

  const openPreviewUrl = () => {
    if (!previewUrl) return;
    openWebInPreview(previewUrl);
  };

  const openDiff = () => {
    if (!record.diff?.patch) return;
    const store = useAppStore.getState();
    const path = filePath ?? `${record.name} result`;
    store.setDiffReviewState({
      requestId: record.id,
      toolName: record.name,
      diff: record.diff.patch,
      files: [
        {
          path,
          patch: record.diff.patch,
          additions: record.diff.plus,
          deletions: record.diff.minus,
        },
      ],
      selectedPath: path,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.addPanel({ id: "approval-diff", kind: "diff", label: "Diff" });
  };

  if (viewMode === "summary") {
    return (
      <div className="flex items-center gap-1.5 py-0.5 text-xs text-[var(--text-muted)]">
        <StatusIcon status={record.status} size={13} />
        <ToolGlyph name={record.name} size={14} className="shrink-0" />
        <span className="text-[var(--text-secondary)] font-semibold">
          {toolDisplayName(record.name)}
        </span>
        {filePath && (
          <span style={summaryValueStyle}>{shortToolPath(filePath)}</span>
        )}
        {!filePath && inputSummary && <span style={summaryValueStyle}>{inputSummary}</span>}
        {duration && <span>{duration}</span>}
      </div>
    );
  }

  return (
    <div
      className="tool-call-enter bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded overflow-hidden"
      data-testid={`tool-call-${record.id}`}
      style={{
        borderLeft: compact ? 0 : `3px solid ${statusIconColor(record.status)}`,
      }}
    >
      {(record.status === "running" || record.status === "pending") && (
        <div className="progress-bar h-0.5" />
      )}
      <div
        role="button"
        tabIndex={0}
        className={record.status === "running" ? "anim-tool-running" : undefined}
        onClick={() => {
          userToggled.current = true;
          setOpen((v) => !v);
        }}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          userToggled.current = true;
          setOpen((v) => !v);
        }}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 8,
          minHeight: compact ? 28 : 34,
          padding: compact ? "4px 7px" : "8px 12px",
          background: "transparent",
          border: 0,
          cursor: "pointer",
          textAlign: "left",
          color: "var(--text-primary)",
          fontSize: compact ? "var(--text-xs)" : "var(--text-sm)",
        }}
      >
        {(record.status === "running" || record.status === "pending") ? <Spinner /> : <ToolGlyph name={record.name} size={14} className="shrink-0" />}
        <span style={phaseBadgeStyle(record)}>{phaseLabel(record)}</span>
        <span className="text-[var(--accent-primary)] font-semibold">
          {toolDisplayName(record.name)}
        </span>
        {inputSummary && (
          <span style={toolInputInlineStyle}>
            {inputSummary}
          </span>
        )}
        <span className="flex-1" />
        {record.artifactId && (
          <SmallAction label="Open artifact preview" onClick={openArtifact}>
            <FileText size={12} />
            Artifact
          </SmallAction>
        )}
        {evidence && (
          <span style={evidenceBadgeStyle}>
            {evidence}
          </span>
        )}
        {previewUrl && (
          <SmallAction label={`Open ${previewUrl} in Preview Pane`} onClick={openPreviewUrl}>
            <Globe size={12} />
            Preview Pane
          </SmallAction>
        )}
        {resultSummary && (
          <SmallAction label="Copy tool result" onClick={copyResult}>
            <Copy size={12} />
            Copy
          </SmallAction>
        )}
        {record.diff && (record.diff.plus > 0 || record.diff.minus > 0) && (
          <SmallAction label="Open diff viewer" onClick={openDiff}>
            {record.diff.plus > 0 && (
              <span className="text-[var(--state-success)]">+{record.diff.plus}</span>
            )}
            {record.diff.minus > 0 && (
              <span className="text-[var(--state-danger)] ml-1">
                -{record.diff.minus}
              </span>
            )}
          </SmallAction>
        )}
        {duration && (
          <span className="text-[var(--text-muted)] text-xs">
            {record.status === "running" ? `${toolVerb(record.name)} - ${duration}` : record.status === "pending" ? `Preparing - ${duration}` : duration}
          </span>
        )}
        {waitingOn && (
          <span className="text-[var(--text-muted)] text-xs">
            {waitingOn}
          </span>
        )}
        <StatusIcon status={record.status} size={13} />
        <span className="text-[var(--text-muted)] inline-flex">
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </div>
      {open && (
        <div className="border-t border-[var(--border-subtle)] bg-[var(--surface-base)] overflow-y-auto"
          style={{
            maxHeight: compact ? 260 : 400,
          }}
        >
          {record.diff?.patch && (
            <>
              <div className="flex items-center pt-1 px-3.5 pb-0 gap-2 text-[var(--text-muted)] text-xs">
                <span className="flex-1 min-w-0 font-semibold">Diff preview</span>
                <button
                  type="button"
                  onClick={openDiff}
                  className="border-0 bg-transparent text-[var(--accent-primary)] cursor-pointer text-xs font-semibold py-px px-0"
                >
                  Open full diff
                </button>
              </div>
              <InlineDiff patch={record.diff.patch} />
            </>
          )}
          {longRunning && (
            <div style={longRunningStyle}>
              Long running task: {waitingOn || "waiting on tool output"}. You can switch panels while it continues.
            </div>
          )}
          <div className="grid gap-2 p-2.5 px-3.5 font-mono text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-words">
            {inputSummary && (
              <div style={humanSummaryStyle}>
            {isCommandRecord(record) ? <TerminalSquare size={13} /> : <ToolGlyph name={record.name} size={13} />}
            <span>{inputSummary}</span>
          </div>
        )}
            {record.limitation && (
              <div style={limitationBadgeStyle}>
                {record.limitation}
              </div>
            )}
            {guidance && (
              <div style={guidanceStyle}>
                {guidance}
              </div>
            )}
            {resultSummary && (
              <div>
                {SpecificRenderer ? (
                  <SpecificRenderer
                    record={record}
                    viewMode={viewMode}
                    compact={compact}
                    inputSummary={inputSummary}
                    resultSummary={resultSummary}
                    rawResultSummary={rawResultSummary}
                    outputExpanded={outputExpanded}
                    setOutputExpanded={setOutputExpanded}
                  />
                ) : isWebRecord(record) ? (
                  <WebSearchResultsView text={rawResultSummary || resultSummary} structured={record.contentPreview} />
                ) : isCommandRecord(record) ? (
                  <CommandResultView
                    command={typeof (record.args.command ?? record.args.cmd) === "string" ? String(record.args.command ?? record.args.cmd) : null}
                    summary={record.summary ?? ""}
                    fallback={resultSummary}
                  />
                ) : (
                  <>
                    <div className="text-[var(--text-muted)] mb-1 font-medium">result</div>
                    {resultSummary.length > 500 && !outputExpanded ? (
                      <>
                        <div>{resultSummary.slice(0, 500)}...</div>
                        <button
                          type="button"
                          onClick={() => setOutputExpanded(true)}
                          className="mt-2 text-[var(--accent-primary)] text-xs font-medium cursor-pointer bg-transparent border-0 p-0"
                        >
                          Show more
                        </button>
                      </>
                    ) : (
                      <div>{resultSummary}</div>
                    )}
                  </>
                )}
              </div>
            )}
            {record.sourceUrl && (
              <div style={sourceUrlStyle}>
                source: {record.sourceUrl}
              </div>
            )}
            {record.contentPreview && record.contentPreview !== resultSummary && !isWebRecord(record) && (
              <div>
                <div className="text-[var(--text-muted)] mb-1 font-medium">content preview</div>
                <div>{record.contentPreview}</div>
              </div>
            )}
            {viewMode === "verbose" && <RawJsonDetails args={record.args} />}
          </div>
        </div>
      )}
    </div>
  );
});

ToolCallCard.displayName = "ToolCallCard";

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}s`;
}

const humanSummaryStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  overflow: "hidden",
};

const summaryValueStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-mono)",
};

const toolInputInlineStyle: React.CSSProperties = {
  minWidth: 0,
  maxWidth: "min(420px, 42vw)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
};

const evidenceBadgeStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  minHeight: 22,
  padding: "0 7px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  flexShrink: 0,
};

const phaseBadgeStyle = (record: ToolCallRecord): React.CSSProperties => {
  const status = record.status;
  const tone = status === "failed" || status === "timeout" || status === "blocked"
    ? "var(--state-warning)"
    : status === "success"
      ? "var(--state-success)"
      : "var(--text-muted)";
  return {
    display: "inline-flex",
    alignItems: "center",
    minHeight: 20,
    padding: "0 6px",
    border: "1px solid var(--border-subtle)",
    borderRadius: "var(--radius-sm, 4px)",
    background: "var(--surface-base)",
    color: tone,
    fontSize: "10px",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 0,
    flexShrink: 0,
  };
};

const limitationBadgeStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  width: "fit-content",
  padding: "5px 8px",
  border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 4px)",
  background: "color-mix(in oklch, var(--state-warning) 9%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
};

const guidanceStyle: React.CSSProperties = {
  display: "block",
  padding: "7px 9px",
  border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 4px)",
  background: "color-mix(in oklch, var(--state-warning) 9%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  lineHeight: 1.45,
};

const sourceUrlStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  wordBreak: "break-all",
};

const longRunningStyle: React.CSSProperties = {
  margin: "10px 14px 0",
  padding: "7px 9px",
  border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 4px)",
  background: "color-mix(in oklch, var(--state-warning) 9%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};
