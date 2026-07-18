import {
  ChevronDown,
  ChevronRight,
  Circle,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  Copy,
  FileText,
  GitBranch,
  Globe,
  ListChecks,
  PencilLine,
  Search,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { getWebSocket } from "../../hooks/useWebSocket";
import { useSharedSecondTick } from "../../lib/shared-tick";
import type { ToolCallRecord, ToolCallStatus } from "../../lib/tool-call-reducer";
import { useAppStore } from "../../stores";
import type { ViewMode } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";

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

const STATUS_COLOR: Record<ToolCallStatus, string> = {
  pending: "var(--text-muted)",
  running: "var(--state-info)",
  success: "var(--state-success)",
  failed: "var(--state-danger)",
  blocked: "var(--state-warning)",
  partial: "var(--state-warning)",
};

const STATUS_LABEL: Record<ToolCallStatus, string> = {
  pending: "等待中",
  running: "运行中",
  success: "已完成",
  failed: "失败",
  blocked: "已跳过",
  partial: "部分完成",
};

const StatusIcon = ({ status }: { status: ToolCallStatus }) => {
  const props = { size: 13, color: STATUS_COLOR[status], className: "shrink-0", role: "img", "aria-label": STATUS_LABEL[status] };
  if (status === "success") return <CircleCheck {...props} />;
  if (status === "failed") return <CircleAlert {...props} />;
  if (status === "blocked") return <CircleDashed {...props} />;
  if (status === "partial") return <CircleDashed {...props} />;
  return <Circle {...props} />;
};

const ToolIcon = ({ name }: { name: string }) => {
  const props = { size: 14, className: "shrink-0" };
  if (name.includes("web")) return <Globe {...props} />;
  if (name.includes("read") || name.includes("file")) return <FileText {...props} />;
  if (name.includes("write") || name.includes("edit") || name.includes("patch") || name.includes("delete") || name.includes("create")) return <PencilLine {...props} />;
  if (name.includes("list") || name.includes("todo")) return <ListChecks {...props} />;
  if (name.includes("grep") || name.includes("glob") || name.includes("search")) return <Search {...props} />;
  if (name.includes("command") || name.includes("terminal")) return <TerminalSquare {...props} />;
  if (name.includes("git")) return <GitBranch {...props} />;
  return <Wrench {...props} />;
};

function toolDisplayName(name: string): string {
  if (name === "web_search" || name === "search_web") return "Search web";
  if (name === "web_fetch") return "Fetch page";
  if (name === "run_command") return "Run command";
  if (name === "read_file") return "Read file";
  if (name === "write_file") return "Write file";
  if (name === "edit_file") return "Edit file";
  if (name === "grep_files" || name === "grep") return "Search files";
  if (name === "glob_files" || name === "glob") return "Scan files";
  if (name === "git_status") return "Check git";
  return name.replace(/_/g, " ");
}

function extractFilePath(args: Record<string, unknown>): string | null {
  const path = args.file_path ?? args.path ?? args.target ?? args.filename;
  return typeof path === "string" ? path : null;
}

function summarizeToolInput(name: string, args: Record<string, unknown>): string | null {
  const command = args.command ?? args.cmd;
  if ((name.includes("command") || name.includes("terminal")) && typeof command === "string") return command;
  const query = args.query ?? args.pattern;
  if ((name.includes("grep") || name.includes("glob") || name.includes("search") || name.includes("web")) && typeof query === "string") return query;
  const url = args.url;
  if (typeof url === "string") return url;
  const filePath = extractFilePath(args);
  if (filePath) return shortPath(filePath);
  const firstScalar = Object.values(args).find((value) => typeof value === "string" || typeof value === "number" || typeof value === "boolean");
  return firstScalar == null ? null : String(firstScalar);
}

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

function parseCommandSummary(summary: string): {
  output: string;
  stderr: string;
  exitCode: string | null;
  timedOut: boolean;
} {
  const trimmed = summary.trim();
  if (!trimmed) return { output: "", stderr: "", exitCode: null, timedOut: false };
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    const stdout = parsed.stdout ?? parsed.output ?? parsed.result ?? parsed.summary;
    const stderr = parsed.stderr ?? parsed.error;
    const exit = parsed.exit_code ?? parsed.exitCode ?? parsed.code;
    return {
      output: typeof stdout === "string" ? stdout : "",
      stderr: typeof stderr === "string" ? stderr : "",
      exitCode: typeof exit === "number" || typeof exit === "string" ? String(exit) : null,
      timedOut: parsed.timed_out === true || parsed.timeout === true,
    };
  } catch {
    return { output: trimmed, stderr: "", exitCode: null, timedOut: false };
  }
}

const CommandResultView = ({
  command,
  summary,
  fallback,
}: {
  command: string | null;
  summary: string;
  fallback: string;
}) => {
  const parsed = useMemo(() => parseCommandSummary(summary), [summary]);
  const output = parsed.output || fallback;
  return (
    <div className="grid gap-1.5">
      {command && (
        <div className="grid grid-cols-[12px_minmax(0,1fr)] gap-1.5 px-2 py-1.5 border border-[var(--border-subtle)] rounded bg-[var(--surface-soft)] text-[var(--text-secondary)] overflow-hidden">
          <span className="text-[var(--accent-primary)] font-bold">$</span>
          <span>{command}</span>
        </div>
      )}
      {output && (
        <pre className="m-0 px-2 py-[7px] max-h-[220px] overflow-auto border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--text-secondary)] font-mono text-xs leading-normal whitespace-pre-wrap break-words">
          {output}
        </pre>
      )}
      {parsed.stderr && (
        <pre className="m-0 px-2 py-[7px] max-h-[220px] overflow-auto border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--state-danger)] font-mono text-xs leading-normal whitespace-pre-wrap break-words">
          {parsed.stderr}
        </pre>
      )}
      {(parsed.exitCode || parsed.timedOut) && (
        <div className="flex gap-2 text-[var(--text-muted)] text-xs font-mono">
          {parsed.exitCode && <span>exit {parsed.exitCode}</span>}
          {parsed.timedOut && <span>timeout</span>}
        </div>
      )}
    </div>
  );
};

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

const WebSearchResultsView = ({ text, structured }: { text: string; structured?: string }) => {
  const items = useMemo(() => {
    // Prefer structured JSON from backend (content_preview field)
    if (structured) {
      try {
        const parsed = JSON.parse(structured) as { title: string; url: string; snippet?: string }[];
        if (Array.isArray(parsed) && parsed.length > 0) {
          return parsed.map((r, i) => ({ index: i + 1, title: r.title, url: r.url, snippet: r.snippet ?? "" }));
        }
      } catch { /* fall through */ }
    }
    // Fallback: parse legacy text format "[1] Title\n    URL: ...\n    Snippet: ..."
    try {
      const parsedItems: { index: number; title: string; url: string; snippet: string }[] = [];
      const blocks = text.split(/\[\d+\]\s+/);
      for (let i = 1; i < blocks.length; i++) {
        const block = blocks[i];
        const lines = block.split("\n");
        const title = lines[0].trim();
        let url = "";
        let snippet = "";
        for (const line of lines.slice(1)) {
          const trimmed = line.trim();
          if (trimmed.startsWith("URL: ")) {
            url = trimmed.slice(5).trim();
          } else if (trimmed.startsWith("片段: ") || trimmed.startsWith("摘要: ") || trimmed.startsWith("snippet: ") || trimmed.startsWith("Snippet: ")) {
            snippet = trimmed.replace(/^(?:片段|摘要|snippet):\s*/i, "").trim();
          }
        }
        if (title && url) {
          parsedItems.push({ index: i, title, url, snippet });
        }
      }
      return parsedItems;
    } catch {
      return [];
    }
  }, [text, structured]);

  const openUrl = (url: string) => {
    useAppStore.getState().openLivePreview(url);
    getWebSocket()?.send({ type: "preview.navigate", url });
  };

  if (items.length === 0) {
    return <div className="whitespace-pre-wrap">{text}</div>;
  }

  return (
    <div className="grid gap-3 w-full font-[var(--font-ui)] mt-1.5">
      <div className="text-[var(--text-muted)] text-xs font-medium font-mono">
        SEARCH RESULTS ({items.length})
      </div>
      <div className="grid gap-2">
        {items.map((item) => {
          let hostname = "";
          try {
            hostname = new URL(item.url).hostname;
          } catch {
            hostname = "";
          }
          return (
            <div
              key={item.index}
              className="bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded p-2.5 grid gap-1"
            >
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-1.5 min-w-0 flex-1">
                  {hostname && <span style={domainGlyphStyle}>{hostname.slice(0, 1).toUpperCase()}</span>}
                  <button
                    type="button"
                    onClick={() => openUrl(item.url)}
                    className="bg-transparent border-0 p-0 cursor-pointer font-semibold text-sm text-[var(--accent-primary)] text-left leading-tight underline underline-offset-2 overflow-hidden text-ellipsis whitespace-nowrap"
                  >
                    {item.title}
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => openUrl(item.url)}
                  className="text-xs px-1.5 py-0.5 rounded bg-[var(--surface-base)] border border-[var(--border-subtle)] text-[var(--text-secondary)] cursor-pointer"
                >
                  Open in Preview Pane
                </button>
              </div>
              <div className="text-xs text-[var(--text-muted)] font-mono break-all">
                {item.url}
              </div>
              {item.snippet && (
                <div className="text-xs text-[var(--text-secondary)] leading-normal mt-0.5 font-[var(--font-ui)]">
                  {item.snippet}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

function shortPath(fullPath: string): string {
  const parts = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 3) return parts.join("/");
  return `.../${parts.slice(-2).join("/")}`;
}

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
  return "Working";
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
  // Default collapsed for cleaner viewport (Codex-style). Only auto-expand in verbose mode.
  const [open, setOpen] = useState(viewMode === "verbose");
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
    // Keep user's toggle preference; don't auto-expand running tools (prevents viewport spam)
    if (!userToggled.current) {
      setOpen(false);
    }
  }, [compact, record.status, viewMode]);
  const duration =
    record.finishedAt && record.startedAt
      ? `${((record.finishedAt - record.startedAt) / 1000).toFixed(1)}s`
      : record.status === "running"
        ? formatElapsed(now - (record.startedAt ?? now))
        : "";
  const longRunning = record.status === "running" && now - (record.startedAt ?? now) > 10_000;

  const filePath = extractFilePath(record.args);
  const inputSummary = record.inputSummary || summarizeToolInput(record.name, record.args);
  const previewUrl = record.summary ? normalizeLocalUrl(record.summary.match(LOCAL_URL_RE)?.[0] ?? "") : "";
  const rawResultSummary = record.summary ? formatToolResult(record.summary) : "";
  const resultSummary = record.displaySummary || rawResultSummary;
  const evidence = evidenceLabel(record);

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
    useAppStore.getState().openLivePreview(previewUrl);
    getWebSocket()?.send({ type: "preview.navigate", url: previewUrl });
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
        <StatusIcon status={record.status} />
        <ToolIcon name={record.name} />
        <span className="text-[var(--text-secondary)] font-semibold">
          {toolDisplayName(record.name)}
        </span>
        {filePath && (
          <span style={summaryValueStyle}>{shortPath(filePath)}</span>
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
        borderLeft: compact ? 0 : `3px solid ${STATUS_COLOR[record.status]}`,
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
        {(record.status === "running" || record.status === "pending") ? <Spinner /> : <ToolIcon name={record.name} />}
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
            {record.status === "running" ? `${toolVerb(record.name)} - ${duration}` : record.status === "pending" ? "Preparing..." : duration}
          </span>
        )}
        <StatusIcon status={record.status} />
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
              Long running task. You can switch panels while it continues.
            </div>
          )}
          <div className="grid gap-2 p-2.5 px-3.5 font-mono text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-words">
            {inputSummary && (
              <div style={humanSummaryStyle}>
            {isCommandRecord(record) ? <TerminalSquare size={13} /> : <ToolIcon name={record.name} />}
            <span>{inputSummary}</span>
          </div>
        )}
            {record.limitation && (
              <div style={limitationBadgeStyle}>
                {record.limitation}
              </div>
            )}
            {resultSummary && (
              <div>
                {isWebRecord(record) ? (
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

const domainGlyphStyle: React.CSSProperties = {
  width: 14,
  height: 14,
  borderRadius: "var(--radius-sm, 3px)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  background: "color-mix(in oklch, var(--accent-primary) 12%, var(--surface-base))",
  border: "1px solid var(--border-subtle)",
  color: "var(--accent-primary)",
  fontSize: "9px",
  fontWeight: 700,
  fontFamily: "var(--font-ui)",
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

const sourceUrlStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  wordBreak: "break-all",
};

const commandResultStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
};

const commandLineStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "12px minmax(0, 1fr)",
  gap: 6,
  padding: "6px 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  overflow: "hidden",
};

const commandPromptStyle: React.CSSProperties = {
  color: "var(--accent-primary)",
  fontWeight: 700,
};

const commandOutputStyle: React.CSSProperties = {
  margin: 0,
  padding: "7px 8px",
  maxHeight: 220,
  overflow: "auto",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const commandMetaStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
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
