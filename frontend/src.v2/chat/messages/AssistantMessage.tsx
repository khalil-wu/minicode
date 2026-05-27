import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  GitBranch,
  Globe,
  Loader2,
  ListChecks,
  PencilLine,
  RefreshCw,
  Search,
  TerminalSquare,
  Trash2,
  Wrench,
} from "lucide-react";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import { getContentBlocks, groupBlocksForRender } from "../../lib/content-blocks";
import type { ChatMessage, Citation, ContentBlock, FileContextRef, MessageContextRef, MessageUsage } from "../../stores/types";
import { useAppStore } from "../../stores";
import { sendChatMessage } from "../sendChatMessage";
import { ArtifactCard } from "./ArtifactCard";
import { StreamingCursor } from "./StreamingCursor";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { visibleProgressRecords } from "./progress-visibility";

const ThinkingIndicator = () => (
  <div style={thinkingIndicatorStyle} aria-live="polite">
    <span className="thinking-mini-dot" aria-hidden="true" />
    <span style={{ fontSize: "var(--text-sm)", fontWeight: 650 }}>Thinking</span>
  </div>
);

const ThinkingBlock = ({
  content,
  isStreaming,
  defaultExpanded = false,
}: {
  content: string;
  isStreaming?: boolean;
  defaultExpanded?: boolean;
}) => {
  const userToggled = useRef(false);
  const startTimeRef = useRef<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const lineCount = content.split("\n").filter(Boolean).length || 1;
  const isOpen = expanded;

  useEffect(() => {
    if (isStreaming) {
      if (startTimeRef.current === null) startTimeRef.current = Date.now();
      const timer = setInterval(() => {
        setElapsed(Math.floor((Date.now() - (startTimeRef.current ?? Date.now())) / 1000));
      }, 1000);
      return () => clearInterval(timer);
    }
    if (startTimeRef.current !== null && !isStreaming) {
      setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
    }
  }, [isStreaming]);

  useEffect(() => {
    if (defaultExpanded && !userToggled.current) setExpanded(true);
  }, [defaultExpanded]);

  const thinkingLabel = isStreaming
    ? elapsed > 0 ? `Thinking ${elapsed}s` : "Thinking"
    : elapsed > 0
      ? `Thought for ${elapsed}s`
      : `Thought ${lineCount} line${lineCount === 1 ? "" : "s"}`;

  return (
    <div style={compactBlockStyle}>
      <button
        type="button"
        onClick={() => {
          userToggled.current = true;
          setExpanded((v) => !v);
        }}
        style={compactHeaderStyle}
        aria-expanded={isOpen}
      >
        {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {isStreaming ? (
          <span className="thinking-mini-dot" aria-hidden="true" />
        ) : (
          <span style={thoughtDotStyle} />
        )}
        <span style={{ fontWeight: 600, color: "var(--text-secondary)" }}>
          {thinkingLabel}
        </span>
      </button>
      {isOpen && (
        <div style={compactBodyStyle}>
          <div style={{ maxHeight: 220, overflowY: "auto", paddingRight: 4 }}>
            {content}
            {isStreaming && <StreamingCursor />}
          </div>
        </div>
      )}
    </div>
  );
};

const ProgressGroup = ({
  records,
  viewMode,
  isStreaming,
  isResumed,
}: {
  records: Extract<ContentBlock, { type: "progress" }>[];
  viewMode: "normal" | "verbose" | "summary";
  isStreaming?: boolean;
  isResumed?: boolean;
}) => {
  const visible = useMemo(() => {
    return visibleProgressRecords(records, viewMode);
  }, [records, viewMode]);
  const latestMatching = (status: string) => {
    for (let i = visible.length - 1; i >= 0; i -= 1) {
      if (visible[i].status === status) return visible[i];
    }
    return null;
  };
  const running = latestMatching("running");
  const failed = latestMatching("failed");
  const completedCount = visible.filter((record) => record.stage === "tool" && record.status === "completed").length;
  const summaryRecord = running || failed || visible.at(-1);
  const summary = summaryRecord ? displayProgressMessage(summaryRecord) : "Working";
  const longRunSummary = useMemo(() => summarizeProgress(records, visible), [records, visible]);
  const [expanded, setExpanded] = useState(false);
  const isOpen = viewMode === "verbose" || expanded;

  useEffect(() => {
    if (viewMode === "verbose") setExpanded(true);
  }, [viewMode]);

  if (viewMode === "summary" || visible.length === 0) return null;

  return (
    <div style={progressBlockStyle}>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        style={compactHeaderStyle}
        aria-expanded={isOpen}
      >
        {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {running && isStreaming ? (
          <Loader2 size={14} className="spinner" style={{ color: "var(--accent-primary)" }} />
        ) : (
          <CheckCircle2 size={14} style={{ color: failed ? "var(--state-danger)" : "var(--accent-primary)" }} />
        )}
          <span style={{ fontWeight: 650, color: "var(--text-secondary)" }}>
          {running && isStreaming
            ? isResumed
              ? "Resuming"
              : progressVerb(running)
            : failed
              ? "Stopped"
              : completedCount > 0
                ? `Ran ${completedCount} step${completedCount === 1 ? "" : "s"}`
                : "Done"}
        </span>
        <span style={headerPreviewStyle}>{summary}</span>
      </button>
      {longRunSummary && (
        <div style={progressSummaryStyle}>
          {longRunSummary}
        </div>
      )}
      {isOpen && (
        <div style={progressListStyle}>
          {visible.map((record) => (
            <div key={`${record.id}-${record.timestamp}`} style={progressRowStyle}>
              <span style={progressDotStyle(record.status)} />
              <div style={{ minWidth: 0 }}>
                <div style={progressMessageStyle}>{displayProgressMessage(record)}</div>
                {record.detail && <div style={progressDetailStyle}>{record.detail}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

type TodoStatus = "pending" | "in_progress" | "completed" | "blocked";
interface TodoRenderItem {
  id: string;
  content: string;
  status: TodoStatus;
  priority?: string;
}

const TodoListBlock = ({ records }: { records: ToolCallRecord[] }) => {
  const latest = records[records.length - 1];
  const todos = extractTodos(latest);
  if (todos.length === 0) return null;
  const completed = todos.filter((todo) => todo.status === "completed").length;
  return (
    <div style={todoListStyle}>
      <div style={todoHeaderStyle}>
        <ListChecks size={14} style={{ color: "var(--accent-primary)" }} />
        <span style={{ fontWeight: 650 }}>Tasks</span>
        <span style={todoCountStyle}>{completed}/{todos.length}</span>
      </div>
      <div style={todoRowsStyle}>
        {todos.map((todo, index) => (
          <div key={todo.id || index} style={todoRowStyle(todo.status)}>
            <TodoStatusMark status={todo.status} />
            <span style={todoTextStyle(todo.status)}>{todo.content}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

const TodoStatusMark = ({ status }: { status: TodoStatus }) => {
  if (status === "completed") {
    return <CheckCircle2 size={14} style={{ color: "var(--state-success)", flexShrink: 0 }} />;
  }
  if (status === "in_progress") {
    return <Loader2 size={14} className="spinner" style={{ color: "var(--accent-primary)", flexShrink: 0 }} />;
  }
  return <span style={todoBoxStyle(status)} />;
};

const ToolDiffBadge = ({ record }: { record: ToolCallRecord }) => {
  if (!record.diff || (!record.diff.plus && !record.diff.minus)) return null;
  return (
    <span style={toolDiffBadgeStyle}>
      <span style={toolDiffPlusStyle}>+{record.diff.plus}</span>
      <span style={toolDiffMinusStyle}> -{record.diff.minus}</span>
    </span>
  );
};

const extractTodos = (record?: ToolCallRecord): TodoRenderItem[] => {
  const raw = record?.args?.todos;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item, index) => {
    if (!item || typeof item !== "object") return [];
    const value = item as Record<string, unknown>;
    const content = String(value.content ?? "").trim();
    if (!content) return [];
    const statusValue = String(value.status ?? "pending");
    const status: TodoStatus = statusValue === "in_progress" || statusValue === "completed" || statusValue === "blocked"
      ? statusValue
      : "pending";
    return [{
      id: String(value.id ?? index),
      content,
      status,
      priority: String(value.priority ?? ""),
    }];
  });
};

function displayProgressMessage(record: Extract<ContentBlock, { type: "progress" }>): string {
  if (record.stage !== "tool" || !record.toolName) {
    if (record.phase === "model" || record.phase === "orienting" || record.stage === "planning") return progressVerb(record);
    return shortProgressText(record.summary || record.message || progressVerb(record));
  }
  if (record.toolName === "todo_write") return "Updated tasks";
  if (record.summary) {
    return shortProgressText(record.summary);
  }
  if (!/\b(read_file|grep_files|glob_files|list_files|run_command|write_file|edit_file)\b/.test(record.message)) {
    return progressToolLabel(record.toolName);
  }
  const message = record.message.replace(/^(Running|Completed|Preparing)\s+/, "");
  const target = message.replace(record.toolName, "").trim();
  const label = progressToolLabel(record.toolName);
  return target ? `${label} ${shortProgressText(target)}` : label;
}

function progressVerb(record: Extract<ContentBlock, { type: "progress" }>): string {
  if (record.label) return record.label;
  if (record.phase === "model" || record.phase === "orienting") return "Thinking";
  if (record.phase === "recover") return "Recovering";
  if (record.stage === "planning") return "Thinking";
  if (record.stage === "tool") return "Working";
  if (record.stage === "approval") return "Waiting";
  if (record.stage === "verification") return "Checking";
  return "Working";
}

function progressToolLabel(toolName: string): string {
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

function shortProgressText(value: string): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > 88 ? `${text.slice(0, 85)}...` : text;
}

function summarizeProgress(
  records: Extract<ContentBlock, { type: "progress" }>[],
  visible: Extract<ContentBlock, { type: "progress" }>[],
): string | null {
  const toolRecords = records.filter((record) => record.stage === "tool");
  if (toolRecords.length < 5) return null;
  const latestRunning = [...visible].reverse().find((record) => record.status === "running");
  const latest = latestRunning ?? visible.at(-1) ?? toolRecords.at(-1);
  const toolNames = Array.from(new Set(toolRecords.map((record) => record.toolName).filter(Boolean) as string[]));
  const action = latest ? displayProgressMessage(latest) : "working";
  const shownTools = toolNames.slice(0, 3).map(progressToolLabel).join(", ");
  return shownTools
    ? `${action} · ${toolRecords.length} steps · ${shownTools}${toolNames.length > 3 ? `, +${toolNames.length - 3}` : ""}`
    : `${action} · ${toolRecords.length} steps`;
}

const ToolCallGroup = ({
  records,
  viewMode,
  isStreaming,
}: {
  records: ToolCallRecord[];
  viewMode: "normal" | "verbose" | "summary";
  isStreaming?: boolean;
}) => {
  const traceRecords = useMemo(() => records.filter((record) =>
    record.name !== "todo_write" && record.name !== "ask_user"
  ), [records]);
  const hasRunning = traceRecords.some((record) => record.status === "running" || record.status === "pending");
  const hasFailed = traceRecords.some((record) => record.status === "failed" || record.status === "blocked");
  const summary = useMemo(() => summarizeToolCalls(traceRecords), [traceRecords]);
  const headline = useMemo(() => toolGroupHeadline(traceRecords), [traceRecords]);
  const visibleRecords = useMemo(() => visibleToolTraceRecords(traceRecords, viewMode), [traceRecords, viewMode]);
  const hiddenCount = Math.max(0, traceRecords.length - visibleRecords.length);
  const [expanded, setExpanded] = useState(() => viewMode === "verbose" || hasRunning || hasFailed);
  const isOpen = viewMode === "verbose" || expanded;
  const totalDuration = useMemo(() => toolGroupDuration(traceRecords), [traceRecords]);

  useEffect(() => {
    if (viewMode === "verbose" || hasRunning || hasFailed) {
      setExpanded(true);
    } else {
      setExpanded(false);
    }
  }, [hasFailed, hasRunning, viewMode]);

  if (traceRecords.length === 0) return null;

  if (viewMode === "summary") {
    return (
      <div style={summaryToolsStyle}>
        <ToolSummaryIcon records={traceRecords} />
        <span>{headline}</span>
        <span style={headerPreviewStyle}>{summary}</span>
      </div>
    );
  }

  return (
    <div style={toolDisclosureStyle(hasFailed, hasRunning && Boolean(isStreaming))}>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        style={toolDisclosureHeaderStyle}
        aria-expanded={isOpen}
        title={summary && summary !== headline ? summary : undefined}
      >
        {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <ToolSummaryIcon records={traceRecords} />
        <span style={toolDisclosureTitleStyle}>{headline}</span>
        {summary && summary !== headline && <span style={toolDisclosurePreviewStyle}>{summary}</span>}
        <span style={toolTraceStatusStyle}>
          {hasRunning ? "running" : hasFailed ? "stopped" : "done"}{totalDuration ? ` · ${totalDuration}` : ""}
        </span>
      </button>
      {isOpen && (
        <div style={toolTimelineStyle(hasFailed, hasRunning && Boolean(isStreaming))}>
          {visibleRecords.map((record) => (
            <ToolTraceRow key={record.id} record={record} />
          ))}
          {hiddenCount > 0 && (
            <div style={toolTraceMoreStyle}>+{hiddenCount} similar web queries hidden</div>
          )}
        </div>
      )}
    </div>
  );
};

const ToolTraceRow = ({ record }: { record: ToolCallRecord }) => {
  const label = describeToolCall(record);
  const result = compactToolResult(record);
  const status = toolStatusLabel(record.status);
  const duration = toolDuration(record);
  const evidence = toolEvidenceLabel(record);
  return (
    <div style={toolTraceRowStyle}>
      <span style={toolTraceIconStyle(record.status)}>
        <ToolIcon name={record.name} size={13} />
      </span>
      <div style={{ minWidth: 0 }}>
        <div style={toolTraceMainStyle}>
          <span style={toolTraceNameStyle}>{label}</span>
          <ToolDiffBadge record={record} />
          {evidence && <span style={toolTraceEvidenceStyle(record.extractionStatus)}>{evidence}</span>}
          <span style={toolTraceStatusStyle}>{status}{duration ? ` · ${duration}` : ""}</span>
        </div>
        {result && <div style={toolTraceResultStyle}>{result}</div>}
      </div>
    </div>
  );
};

const ToolSummaryIcon = ({ records }: { records: ToolCallRecord[] }) => {
  const running = records.find((record) => record.status === "running" || record.status === "pending");
  const latest = running ?? records[records.length - 1];
  if (!latest) return <Wrench size={13} style={toolIconBaseStyle} />;
  return <ToolIcon name={latest.name} size={13} />;
};

const ToolIcon = ({ name, size = 13 }: { name: string; size?: number }) => {
  const props = { size, style: toolIconBaseStyle };
  if (isWebToolName(name)) return <Globe {...props} />;
  if (isCommandToolName(name)) return <TerminalSquare {...props} />;
  if (name.includes("todo") || name.includes("task") || name.includes("list")) return <ListChecks {...props} />;
  if (isWriteToolName(name)) return <PencilLine {...props} />;
  if (name.includes("read") || name.includes("file")) return <FileText {...props} />;
  if (name.includes("grep") || name.includes("glob") || name.includes("search")) return <Search {...props} />;
  if (name.includes("git")) return <GitBranch {...props} />;
  return <Wrench {...props} />;
};

const isWriteToolName = (name: string): boolean => (
  name !== "todo_write" &&
  /(?:write|edit|patch|delete|remove|create|move|rename|save)/i.test(name)
);

const isCommandToolName = (name: string): boolean => (
  /(?:run_command|terminal|shell|bash|powershell|cmd)/i.test(name)
);

const isWebToolName = (name: string): boolean => (
  /(?:web|search_web|fetch_page)/i.test(name)
);

const summarizeToolCalls = (records: ToolCallRecord[]): string => {
  const webCount = records.filter(isWebLookupTool).length;
  if (records.length > 1 && records.every((record) => isCommandToolName(record.name))) {
    return "";
  }
  if (webCount === records.length) {
    return records
      .map(extractWebQuery)
      .filter((value): value is string => Boolean(value))
      .slice(-2)
      .map(shortSearchQuery)
      .join(", ");
  }
  const names = records.map((record) => {
    const target = extractToolTarget(record);
    const label = toolNameLabel(record.name);
    return target ? `${label} ${shortToolTarget(record.name, target)}` : label;
  });
  const unique = Array.from(new Set(names));
  const shown = unique.slice(0, 3).join(", ");
  return unique.length > 3 ? `${shown}, +${unique.length - 3}` : shown;
};

const toolGroupHeadline = (records: ToolCallRecord[]): string => {
  const running = records.find((record) => record.status === "running" || record.status === "pending");
  const failed = [...records].reverse().find((record) => record.status === "failed" || record.status === "blocked");
  const webCount = records.filter(isWebLookupTool).length;
  const commandCount = records.filter((record) => isCommandToolName(record.name)).length;
  const fileMutationCount = records.filter((record) => isWriteToolName(record.name) && Boolean(extractFilePath(record.args))).length;
  const fileCount = records.filter((record) => Boolean(extractFilePath(record.args))).length;
  if (webCount === records.length && records.length > 1) {
    const failedCount = records.filter((record) => record.status === "failed" || record.status === "blocked").length;
    if (running && isWebLookupTool(running)) return "Searching the web";
    if (failed && isWebLookupTool(failed)) return `Web search hit ${failedCount} issue${failedCount === 1 ? "" : "s"}`;
    const searchCount = records.filter((record) => record.name === "web_search" || record.name === "search_web").length;
    const fetchCount = records.filter((record) => record.name === "web_fetch").length;
    if (searchCount > 0 && fetchCount > 0) return `Searched web · ${fetchCount} source${fetchCount === 1 ? "" : "s"}`;
    if (searchCount > 0) return `Searched web · ${searchCount} quer${searchCount === 1 ? "y" : "ies"}`;
    return `Fetched ${fetchCount} source${fetchCount === 1 ? "" : "s"}`;
  }
  const latest = running ?? failed ?? records[records.length - 1];
  if (!latest) return "Working";
  if (records.length > 1 && !running && !failed) {
    if (commandCount === records.length) return `Ran ${commandCount} command${commandCount === 1 ? "" : "s"}`;
    if (fileMutationCount === records.length) {
      const diff = records.reduce((acc, record) => ({
        plus: acc.plus + (record.diff?.plus ?? 0),
        minus: acc.minus + (record.diff?.minus ?? 0),
      }), { plus: 0, minus: 0 });
      const suffix = diff.plus || diff.minus ? ` · +${diff.plus} -${diff.minus}` : "";
      return `Edited ${fileMutationCount} file${fileMutationCount === 1 ? "" : "s"}${suffix}`;
    }
    if (fileCount === records.length) return `Touched ${fileCount} file${fileCount === 1 ? "" : "s"}`;
    if (webCount > 0) return `Searched web · ${webCount} source${webCount === 1 ? "" : "s"}`;
    return summarizeToolCalls(records);
  }
  return describeToolCall(latest);
};

export const describeToolCallForTest = (record: ToolCallRecord): string => describeToolCall(record);

const describeToolCall = (record: ToolCallRecord): string => {
  const target = extractToolTarget(record);
  const label = toolNameLabel(record.name);
  const suffix = target ? ` ${shortToolTarget(record.name, target)}` : "";
  if (record.status === "failed" || record.status === "blocked") return `Stopped at ${label}${suffix}`;
  if (record.status === "running" || record.status === "pending") return `${toolPresentVerb(record.name)}${suffix}`;
  return `${label}${suffix}`;
};

const toolPresentVerb = (name: string): string => {
  if (isWriteToolName(name)) {
    if (name.includes("delete") || name.includes("remove")) return "Deleting";
    if (name.includes("create")) return "Creating";
    return name.includes("edit") ? "Editing" : "Writing";
  }
  if (isCommandToolName(name)) return "Running";
  if (isWebToolName(name)) return name.includes("fetch") ? "Fetching" : "Searching";
  switch (name) {
    case "read_file":
    case "read_artifact":
      return "Reading";
    case "list_files":
      return "Listing";
    case "grep":
    case "grep_files":
      return "Searching";
    case "glob":
    case "glob_files":
      return "Scanning";
    case "todo_write":
      return "Updating tasks";
    default:
      return "Using";
  }
};

const toolNameLabel = (name: string): string => {
  if (isWriteToolName(name)) {
    if (name.includes("delete") || name.includes("remove")) return "Deleted";
    if (name.includes("create")) return "Created";
    return name.includes("edit") ? "Edited" : "Wrote";
  }
  if (isCommandToolName(name)) return "Ran";
  if (isWebToolName(name)) return name.includes("fetch") ? "Fetched" : "Searched";
  switch (name) {
    case "read_file":
    case "read_artifact":
      return "Read";
    case "list_files":
      return "Listed";
    case "grep":
    case "grep_files":
      return "Searched";
    case "glob":
    case "glob_files":
      return "Scanned";
    case "todo_write":
      return "Updated tasks";
    default:
      return name;
  }
};

const extractToolTarget = (record: ToolCallRecord): string | null => {
  return extractFilePath(record.args) ?? extractArg(record.args, "command", "query", "pattern", "directory", "cwd");
};

const isWebLookupTool = (record: ToolCallRecord): boolean => {
  return record.name === "web_search" || record.name === "web_fetch" || record.name === "search_web";
};

const extractWebQuery = (record: ToolCallRecord): string | null => {
  return extractArg(record.args, "query", "q", "url");
};

const shortSearchQuery = (value: string): string => {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > 36 ? `${text.slice(0, 33)}...` : text;
};

const visibleToolTraceRecords = (
  records: ToolCallRecord[],
  viewMode: "normal" | "verbose" | "summary",
): ToolCallRecord[] => {
  if (viewMode === "verbose" || records.filter(isWebLookupTool).length < 3) return records;
  const failed = records.filter((record) => record.status === "failed" || record.status === "blocked");
  const running = records.filter((record) => record.status === "running" || record.status === "pending");
  const recent = records.slice(-3);
  const byId = new Map<string, ToolCallRecord>();
  [...failed.slice(-2), ...running, ...recent].forEach((record) => byId.set(record.id, record));
  return records.filter((record) => byId.has(record.id));
};

const extractFilePath = (args: Record<string, unknown>): string | null => {
  const path = args.file_path ?? args.path ?? args.target ?? args.filename;
  return typeof path === "string" ? path : null;
};

const extractArg = (args: Record<string, unknown>, ...keys: string[]): string | null => {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
};

const compactToolResult = (record: ToolCallRecord): string => {
  const text = String(record.summary || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (record.status === "failed" || record.status === "blocked") {
    return cleanFailureSummary(text);
  }
  if (isWebLookupTool(record)) {
    return safeOneLineSummary(text);
  }
  if (isCommandToolName(record.name)) {
    return "";
  }
  if (isWriteToolName(record.name)) {
    return record.diff ? "" : safeMutationSummary(text);
  }
  return "";
};

const cleanFailureSummary = (summary: string): string => {
  const text = summary.replace(/\s+/g, " ").trim();
  if (/不在允许范围|outside (?:the )?(?:allowed|trusted) workspace|outside allowed/i.test(text)) {
    return "Outside allowed workspace";
  }
  if (/File changed on disk|expected_hash|actual_hash|changed on disk/i.test(text)) {
    return "File changed on disk; re-read before editing";
  }
  if (/permission denied|access is denied|unauthorized/i.test(text)) {
    return "Permission denied";
  }
  if (/timeout|timed out|超时/i.test(text)) {
    return "Timed out";
  }
  return safeOneLineSummary(text);
};

const safeMutationSummary = (summary: string): string => {
  if (/saved as an artifact|content_hash|File\s+\.?[\\/]/i.test(summary)) return "";
  return safeOneLineSummary(summary);
};

const safeOneLineSummary = (summary: string): string => {
  const text = summary.replace(/\s+/g, " ").trim();
  if (looksLikeRawToolPayload(text)) return "";
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
};

const looksLikeRawToolPayload = (text: string): boolean => (
  /允许的路径|禁止的路径|allowed paths|forbidden path|expected_hash|actual_hash/i.test(text) ||
  /content_hash|saved as an artifact|approx \d+ tokens/i.test(text) ||
  /<!DOCTYPE|<html\b|<meta\b|^\s*(import|from|def|class|function|const|let|var)\s/i.test(text) ||
  /```|\{["']?[A-Za-z0-9_]+["']?\s*:/.test(text)
);

const toolStatusLabel = (status: ToolCallRecord["status"]): string => {
  switch (status) {
    case "running":
    case "pending":
      return "running";
    case "success":
      return "done";
    case "blocked":
      return "blocked";
    case "failed":
      return "failed";
    default:
      return status;
  }
};

const toolEvidenceLabel = (record: ToolCallRecord): string => {
  if (!isWebLookupTool(record)) return "";
  const kind =
    record.evidenceType === "candidate"
      ? "candidate"
      : record.evidenceType === "fetched"
        ? "fetched"
        : record.evidenceType || "";
  const status = record.extractionStatus || "";
  if (kind && status) return `${kind} · ${status}`;
  return kind || status;
};

const toolDuration = (record: ToolCallRecord): string => {
  if (!record.finishedAt || !record.startedAt) return "";
  const seconds = (record.finishedAt - record.startedAt) / 1000;
  if (seconds < 0.1) return "";
  return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
};

const formatDiffBadge = (record: ToolCallRecord): string => {
  if (!record.diff) return "";
  const { plus, minus } = record.diff;
  if (!plus && !minus) return "";
  return `+${plus} -${minus}`;
};

const toolGroupDuration = (records: ToolCallRecord[]): string => {
  const completed = records.filter((record) => record.startedAt && record.finishedAt);
  if (!completed.length) return "";
  const started = Math.min(...completed.map((record) => record.startedAt));
  const finished = Math.max(...completed.map((record) => record.finishedAt || record.startedAt));
  const seconds = Math.max(0, (finished - started) / 1000);
  if (seconds < 0.1) return "";
  return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
};

const shortPath = (fullPath: string): string => {
  const parts = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
  const value = parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
  return value.length > 76 ? `${value.slice(0, 73)}...` : value;
};

const shortToolTarget = (toolName: string, target: string): string => {
  const text = target.replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (toolName === "run_command") {
    return text.length > 88 ? `${text.slice(0, 85)}...` : text;
  }
  if (toolName === "web_search" || toolName === "web_fetch" || toolName === "search_web") {
    return text.length > 72 ? `${text.slice(0, 69)}...` : text;
  }
  if (/[\\/]/.test(text)) return shortPath(text);
  return text.length > 76 ? `${text.slice(0, 73)}...` : text;
};

const buildContextPrefix = (refs: MessageContextRef[]): string => {
  const lines: string[] = [];
  const fileRefs = refs.filter((ref): ref is FileContextRef => ref.kind === "file" || ref.kind === "folder" || ref.kind === "url");
  const skillRefs = refs.filter((ref): ref is Extract<MessageContextRef, { kind: "skill" }> => ref.kind === "skill");
  if (fileRefs.length > 0) {
    lines.push("Context references:");
    lines.push(...fileRefs.map((ref) => `- @${ref.kind}:${ref.path}`));
  }
  if (skillRefs.length > 0) {
    if (lines.length > 0) lines.push("");
    lines.push("Requested skills:");
    lines.push(...skillRefs.map((ref) => `- ${ref.name}${ref.description ? `: ${ref.description}` : ""}`));
  }
  return lines.length > 0 ? `${lines.join("\n")}\n\n` : "";
};

const RetryButton = ({ messageId }: { messageId: string }) => {
  const handleRetry = useCallback(async () => {
    const state = useAppStore.getState();
    const idx = state.messages.findIndex((m) => m.id === messageId);
    if (idx < 0) return;
    const userIdx = state.messages.slice(0, idx).map((m, i) => ({ m, i })).reverse().find(({ m }) => m.role === "user")?.i;
    if (userIdx == null) return;
    const removedCount = state.messages.length - userIdx;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "Retry",
      message: `Retry from the previous user message? This will remove ${removedCount} message${removedCount === 1 ? "" : "s"} from the current view before resending.`,
      confirmLabel: "Retry",
      danger: true,
    });
    if (!ok) return;
    const userMsg = state.messages[userIdx];
    if (!userMsg) return;
    const nextMessages = state.messages.slice(0, userIdx);
    if (state.conversationId) {
      state.hydrateConversationMessages(state.conversationId, nextMessages, { activate: true, isStreaming: false });
    } else {
      useAppStore.setState({ messages: nextMessages, isStreaming: false });
    }
    const prefix = buildContextPrefix(userMsg.contextRefs ?? []);
    sendChatMessage({
      displayContent: userMsg.content,
      backendContent: `${prefix}${userMsg.content}`.trim(),
      contextRefs: userMsg.contextRefs ?? [],
    });
  }, [messageId]);

  return (
    <button type="button" onClick={handleRetry} title="Retry from previous user message" style={actionButtonStyle}>
      <RefreshCw size={12} />
      Retry
    </button>
  );
};

const CopyMessageButton = ({ content }: { content: string }) => {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [content]);

  return (
    <button type="button" onClick={handleCopy} title="Copy message" style={actionButtonStyle}>
      <Copy size={12} />
      {copied ? "Copied" : "Copy"}
    </button>
  );
};

const DeleteMessageButton = ({ messageId }: { messageId: string }) => {
  const handleDelete = useCallback(async () => {
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "Delete reply",
      message: "Delete this assistant reply from the current conversation view?",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    useAppStore.getState().deleteMessage(messageId);
  }, [messageId]);

  return (
    <button type="button" onClick={handleDelete} title="Delete reply" style={actionButtonStyle}>
      <Trash2 size={12} />
      Delete
    </button>
  );
};

const formatTokenCount = (n: number): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
};

const TokenBadge = ({ usage }: { usage: MessageUsage }) => {
  const total = usage.input + usage.output + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
  if (total <= 0) return null;
  const title = `In: ${usage.input} | Out: ${usage.output}${usage.cacheRead ? ` | Cache read: ${usage.cacheRead}` : ""}`;
  return (
    <span title={title} style={tokenBadgeStyle}>
      {formatTokenCount(usage.input + usage.output)} tok
    </span>
  );
};

export const AssistantMessage = memo(({ message }: { message: ChatMessage }) => {
  const viewMode = useAppStore((s) => s.viewMode);
  const blocks = getContentBlocks(message);
  const isThinking = message.isStreaming && blocks.length === 0;
  const showThinking = viewMode !== "summary";
  const isResumed = message.resumeState === "resumed";
  const renderGroups = useMemo(() => groupBlocksForRender(blocks), [blocks]);
  const lastIdx = renderGroups.length - 1;
  const compactText = useMemo(() => isCompactAssistantReply(message, renderGroups), [message, renderGroups]);

  return (
    <div className="assistant-msg" style={{ display: "flex", maxWidth: "100%" }}>
      <div style={compactText ? assistantCompactColumnStyle : assistantColumnStyle}>
        {isThinking && <ThinkingIndicator />}
        {renderGroups.map((group, i) => {
          if (group.type === "thinking") {
            if (!showThinking) return null;
            return (
              <ThinkingBlock
                key={`thinking-${i}`}
                content={group.content}
                isStreaming={message.isThinkingStreaming && i === lastIdx}
                defaultExpanded={viewMode === "verbose" || isResumed}
              />
            );
          }
          if (group.type === "tool_call_group") {
            return (
              <ToolCallGroup
                key={`tools-${i}`}
                records={group.records}
                viewMode={viewMode}
                isStreaming={message.isStreaming}
              />
            );
          }
          if (group.type === "todo_list") {
            return (
              <TodoListBlock
                key={`todos-${i}`}
                records={group.records}
              />
            );
          }
          if (group.type === "progress_group") {
            return (
              <ProgressGroup
                key={`progress-${i}`}
                records={group.records}
                viewMode={viewMode}
                isStreaming={message.isStreaming}
                isResumed={isResumed}
              />
            );
          }
          return (
            <div key={`text-${i}`} className="md-prose" style={compactText ? compactContentStyle : contentStyle}>
              <MarkdownRenderer
                content={group.content}
                isStreaming={message.isStreaming && i === lastIdx}
                citations={message.citations}
              />
              {message.isStreaming && !message.isThinkingStreaming && i === lastIdx && <StreamingCursor />}
            </div>
          );
        })}
        {message.isStreaming && !message.isThinkingStreaming && !isThinking && lastIdx >= 0 && renderGroups[lastIdx].type !== "text" && (
          <StreamingCursor />
        )}
        {message.artifacts && message.artifacts.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {message.artifacts.map((a) => (
              <ArtifactCard key={a.artifactId} artifact={a} />
            ))}
          </div>
        )}
        {message.citations && message.citations.length > 0 && (
          <CitationList citations={message.citations} />
        )}
        {!message.isStreaming && message.content && (
          <div className="msg-actions" style={actionsStyle}>
            <CopyMessageButton content={message.content} />
            <RetryButton messageId={message.id} />
            <DeleteMessageButton messageId={message.id} />
            {message.usage && <TokenBadge usage={message.usage} />}
          </div>
        )}
      </div>
    </div>
  );
});

AssistantMessage.displayName = "AssistantMessage";

const isCompactAssistantReply = (
  message: ChatMessage,
  groups: ReturnType<typeof groupBlocksForRender>,
): boolean => {
  if (message.isStreaming || message.artifacts?.length || message.citations?.length) return false;
  if (groups.length !== 1 || groups[0]?.type !== "text") return false;
  const content = groups[0].content.trim();
  if (!content || /```|^\s*[-*]\s|^\s*\d+\.\s|^\s*#{1,6}\s|^\s*>/m.test(content)) return false;
  const lines = content.split(/\r?\n/).filter((line) => line.trim());
  return lines.length <= 2 && content.length <= 180;
};

const citationHref = (citation: Citation): string | null => {
  const candidate = citation.url || citation.source;
  return /^https?:\/\//i.test(candidate) ? candidate : null;
};

const citationLabel = (citation: Citation): string => {
  if (citation.label) return citation.label;
  if (citation.title) return citation.title;
  try {
    const href = citationHref(citation);
    if (href) return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    // Fall through to the source tail below.
  }
  return citation.source.split("/").filter(Boolean).pop() || citation.source;
};

const CitationList = ({ citations }: { citations: Citation[] }) => (
  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }} aria-label="Sources">
    {citations.map((c, i) => {
      const href = citationHref(c);
      const label = `[${i + 1}] ${citationLabel(c)}`;
      if (href) {
        return (
          <a
            key={`${c.source}-${i}`}
            title={c.title || c.source}
            style={citationStyle}
            href={href}
            target="_blank"
            rel="noreferrer"
          >
            {label}
          </a>
        );
      }
      return (
        <span key={`${c.source}-${i}`} title={c.title || c.source} style={citationStyle}>
          {label}
        </span>
      );
    })}
  </div>
);

const compactBlockStyle: React.CSSProperties = {
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  overflow: "hidden",
  fontSize: "var(--text-sm)",
};

const todoListStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  padding: "8px 10px",
  borderLeft: "1px solid color-mix(in oklch, var(--accent-primary) 38%, transparent)",
  background: "transparent",
  fontSize: "var(--text-sm)",
};

const todoHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  minWidth: 0,
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const todoCountStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "1px 5px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
};

const todoRowsStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  paddingLeft: 1,
};

const todoRowStyle = (status: TodoStatus): React.CSSProperties => ({
  display: "grid",
  gridTemplateColumns: "16px minmax(0, 1fr)",
  alignItems: "center",
  gap: 8,
  minHeight: 22,
  color: status === "completed" ? "var(--text-muted)" : "var(--text-secondary)",
});

const todoTextStyle = (status: TodoStatus): React.CSSProperties => ({
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  textDecoration: status === "completed" ? "line-through" : "none",
  opacity: status === "completed" ? 0.78 : 1,
});

const todoBoxStyle = (status: TodoStatus): React.CSSProperties => ({
  width: 12,
  height: 12,
  borderRadius: 3,
  border: `1.5px solid ${status === "blocked" ? "var(--state-danger)" : "var(--border-strong, var(--border-subtle))"}`,
  background: status === "blocked" ? "color-mix(in oklch, var(--state-danger) 12%, transparent)" : "transparent",
  flexShrink: 0,
});

const thinkingIndicatorStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  width: "fit-content",
  minHeight: 24,
  padding: "2px 0",
  color: "var(--text-secondary)",
  background: "transparent",
  border: 0,
  borderRadius: 999,
};

const assistantColumnStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  flex: 1,
  minWidth: 0,
};

const assistantCompactColumnStyle: React.CSSProperties = {
  ...assistantColumnStyle,
  flex: "0 1 auto",
  maxWidth: "78%",
};

const progressBlockStyle: React.CSSProperties = {
  ...compactBlockStyle,
  background: "transparent",
  borderColor: "transparent",
};

const toolDisclosureStyle = (hasFailed: boolean, hasRunning: boolean): React.CSSProperties => ({
  display: "grid",
  gap: 0,
  padding: 0,
  borderLeft: `1px solid ${
    hasFailed
      ? "color-mix(in oklch, var(--state-danger) 42%, transparent)"
      : hasRunning
        ? "color-mix(in oklch, var(--accent-primary) 38%, transparent)"
        : "var(--border-subtle)"
  }`,
  fontSize: "var(--text-sm)",
});

const toolDisclosureHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
  minHeight: 28,
  padding: "4px 0 4px 10px",
  border: 0,
  background: "transparent",
  color: "var(--text-secondary)",
  cursor: "pointer",
  textAlign: "left",
  fontSize: "var(--text-xs)",
};

const toolDisclosureTitleStyle: React.CSSProperties = {
  minWidth: 0,
  flexShrink: 0,
  maxWidth: "42%",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
  fontWeight: 650,
};

const toolDisclosurePreviewStyle: React.CSSProperties = {
  minWidth: 0,
  flex: "1 1 auto",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
};

const toolTimelineStyle = (hasFailed: boolean, hasRunning: boolean): React.CSSProperties => ({
  display: "grid",
  gap: 0,
  padding: "1px 0 1px 20px",
  fontSize: "var(--text-sm)",
});

const compactHeaderStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 30,
  display: "flex",
  alignItems: "center",
  gap: 7,
  padding: "5px 8px",
  background: "transparent",
  border: 0,
  cursor: "pointer",
  textAlign: "left",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const toolTraceRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "16px minmax(0, 1fr)",
  gap: 8,
  alignItems: "start",
  minHeight: 24,
  padding: "4px 0 4px 9px",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const toolIconBaseStyle: React.CSSProperties = {
  color: "currentColor",
  flexShrink: 0,
};

const toolTraceIconStyle = (status: ToolCallRecord["status"]): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 14,
  height: 14,
  marginTop: 4,
  color:
    status === "failed" || status === "blocked"
      ? "var(--state-danger)"
      : status === "running" || status === "pending"
        ? "var(--accent-primary)"
        : "var(--text-muted)",
  opacity: status === "success" ? 0.72 : 0.95,
});

const toolTraceDotStyle = (status: ToolCallRecord["status"]): React.CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: "50%",
  marginTop: 7,
  background:
    status === "failed" || status === "blocked"
      ? "var(--state-danger)"
      : status === "success"
        ? "var(--state-success, var(--accent-primary))"
        : "var(--accent-primary)",
  opacity: status === "running" || status === "pending" ? 0.9 : 0.55,
  boxShadow: status === "running" || status === "pending"
    ? "0 0 0 3px color-mix(in oklch, var(--accent-primary) 12%, transparent)"
    : undefined,
});

const toolTraceMainStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: 8,
  minWidth: 0,
};

const toolTraceNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
  fontWeight: 600,
};

const toolTraceStatusStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const toolTraceEvidenceStyle = (status?: string): React.CSSProperties => ({
  flexShrink: 0,
  padding: "0 5px",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: status === "failed"
    ? "color-mix(in oklch, var(--state-danger) 9%, transparent)"
    : status === "partial"
      ? "color-mix(in oklch, var(--state-warning) 10%, transparent)"
      : "var(--surface-soft)",
  color: status === "failed"
    ? "var(--state-danger)"
    : status === "partial"
      ? "var(--state-warning)"
      : "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
});

const toolDiffBadgeStyle: React.CSSProperties = {
  flexShrink: 0,
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const toolDiffPlusStyle: React.CSSProperties = {
  color: "var(--state-success)",
};

const toolDiffMinusStyle: React.CSSProperties = {
  color: "var(--state-danger)",
};

const toolTraceResultStyle: React.CSSProperties = {
  marginTop: 2,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
};

const toolTraceMoreStyle: React.CSSProperties = {
  padding: "5px 0 2px 27px",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
};

const progressListStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  padding: "4px 8px 8px 28px",
};

const progressSummaryStyle: React.CSSProperties = {
  margin: "0 8px 6px 28px",
  padding: "6px 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
};

const progressRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "10px minmax(0, 1fr)",
  gap: 8,
  alignItems: "start",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const progressDotStyle = (status: string): React.CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: "50%",
  marginTop: 7,
  background:
    status === "failed"
      ? "var(--state-danger)"
      : status === "completed"
        ? "var(--state-success, var(--accent-primary))"
        : "var(--accent-primary)",
  opacity: status === "running" ? 0.75 : 0.55,
});

const progressMessageStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const progressDetailStyle: React.CSSProperties = {
  marginTop: 2,
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const compactBodyStyle: React.CSSProperties = {
  borderTop: "1px solid var(--border-subtle)",
  padding: "8px 10px",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  color: "var(--text-secondary)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  lineHeight: 1.55,
  background: "var(--surface-base)",
};

const thoughtDotStyle: React.CSSProperties = {
  width: 7,
  height: 7,
  borderRadius: "50%",
  background: "var(--accent-primary)",
  opacity: 0.7,
  flexShrink: 0,
};

const headerPreviewStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  color: "var(--text-muted)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const summaryToolsStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "2px 0",
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const contentStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-md)",
  lineHeight: 1.75,
  wordBreak: "break-word",
  paddingTop: 0,
};

const compactContentStyle: React.CSSProperties = {
  ...contentStyle,
  display: "inline-block",
  width: "fit-content",
  maxWidth: "100%",
  padding: "9px 12px",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  lineHeight: 1.6,
};

const actionsStyle: React.CSSProperties = {
  display: "flex",
  gap: 6,
  marginTop: 2,
  opacity: 0,
  transition: "opacity 150ms",
  alignItems: "center",
};

const actionButtonStyle: React.CSSProperties = {
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  background: "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "0 7px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const tokenBadgeStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  color: "var(--text-muted)",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "1px 6px",
  cursor: "default",
};

const citationStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  color: "var(--accent-primary)",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "2px 6px",
  cursor: "pointer",
  textDecoration: "none",
};
