import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Code2,
  ExternalLink,
  FileDiff,
  FileText,
  Globe,
  MessageSquare,
  Search,
  TerminalSquare,
  Wrench,
  X,
} from "lucide-react";
import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";
import type { PendingApproval, PendingAskUser, PendingDiffReview } from "../stores/types";

export const InlineAgentPrompt = () => {
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);

  if (!pendingApproval && !pendingDiffReview && !pendingAskUser) return null;

  return (
    <div style={shellStyle} aria-label="Agent is waiting for input">
      {pendingDiffReview && <DiffApprovalCard request={pendingDiffReview} />}
      {pendingApproval && <ToolApprovalCard request={pendingApproval} queue={approvalQueue} />}
      {pendingAskUser && <AskUserCard request={pendingAskUser} />}
    </div>
  );
};

const ToolApprovalCard = ({ request, queue }: { request: PendingApproval; queue: PendingApproval[] }) => {
  const [responding, setResponding] = useState(false);
  const summary = useMemo(() => summarizeArgs(request.args), [request.args]);
  const total = 1 + queue.length;
  const displayName = toolDisplayName(request.toolName);

  const respond = (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    getWebSocket()?.send({
      type: "approval",
      tool_call_id: request.requestId,
      action: allowed ? "approve" : "reject",
    });
    useAppStore.getState().clearApproval(request.requestId);
  };

  const allowAll = () => {
    const store = useAppStore.getState();
    const all = [request, ...store.approvalQueue];
    for (const item of all) {
      getWebSocket()?.send({
        type: "approval",
        tool_call_id: item.requestId,
        action: "approve",
      });
    }
    store.clearApprovals(all.map((item) => item.requestId));
  };

  return (
    <section style={approvalBarStyle}>
      <div style={approvalIconStyle}>
        <ToolGlyph name={request.toolName} />
      </div>

      <div style={approvalMainStyle}>
        <div style={approvalTitleRowStyle}>
          <span style={titleStyle}>Allow {displayName}?</span>
          {total > 1 && <span style={pendingPillStyle}>{total} pending</span>}
        </div>
        <div style={subtitleStyle}>Permission required before this tool can run.</div>

        <div style={compactSummaryRowStyle}>
          {summary.slice(0, 2).map((item) => (
            <span key={item.label} style={approvalArgStyle} title={`${item.label}: ${item.value}`}>
              <span style={approvalArgLabelStyle}>{item.label}</span>
              <span style={approvalArgValueStyle}>{item.value}</span>
            </span>
          ))}
        </div>

        {queue.length > 0 && (
          <div style={queueStyle}>
            Next: {queue.map((item) => toolDisplayName(item.toolName)).join(", ")}
          </div>
        )}
      </div>

      <div style={compactButtonRowStyle}>
        <button type="button" onClick={() => respond(false)} disabled={responding} style={secondaryButtonStyle} aria-label="Deny tool use">
          <X size={13} />
          Deny
        </button>
        <button type="button" onClick={() => respond(true)} disabled={responding} style={primaryButtonStyle} aria-label="Allow tool use">
          <Check size={13} />
          Allow
        </button>
        {queue.length > 0 && (
          <button type="button" onClick={allowAll} disabled={responding} style={accentButtonStyle} aria-label="Allow all pending tool requests">
            Allow all
          </button>
        )}
      </div>
    </section>
  );
};

const ToolGlyph = ({ name }: { name: string }) => {
  const props = { size: 15, color: "var(--state-warning)" };
  if (name.includes("web")) return <Globe {...props} />;
  if (name.includes("command") || name.includes("terminal") || name.includes("bash")) return <TerminalSquare {...props} />;
  if (name.includes("write") || name.includes("edit") || name.includes("patch")) return <Code2 {...props} />;
  if (name.includes("read") || name.includes("file")) return <FileText {...props} />;
  if (name.includes("grep") || name.includes("glob") || name.includes("search")) return <Search {...props} />;
  return <Wrench {...props} />;
};

const toolDisplayName = (name: string): string => {
  if (name === "web_search" || name === "search_web") return "Search web";
  if (name === "web_fetch") return "Fetch page";
  if (name === "run_command") return "Run command";
  if (name === "read_file") return "Read file";
  if (name === "write_file") return "Write file";
  if (name === "edit_file") return "Edit file";
  if (name === "apply_patch") return "Apply patch";
  if (name === "grep_files" || name === "grep") return "Search files";
  if (name === "glob_files" || name === "glob") return "Scan files";
  if (name === "git_status") return "Check git";
  return name.replace(/_/g, " ");
};
const DiffApprovalCard = ({ request }: { request: PendingDiffReview }) => {
  const diffReview = useAppStore((s) => s.diffReview);
  const stats = useMemo(() => diffStats(request.diff), [request.diff]);

  const respond = (allowed: boolean) => {
    getWebSocket()?.send({
      type: "approval",
      tool_call_id: request.requestId,
      action: allowed ? "approve" : "reject",
    });
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === request.requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: allowed ? "approved" : "rejected",
      });
    }
    useAppStore.getState().clearDiffReview();
  };

  const openDiff = () => {
    useAppStore.getState().setRightStackTab("inspector");
    useAppStore.getState().addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "Diff Review",
    });
  };

  return (
    <section style={cardStyle}>
      <div style={headerStyle}>
        <FileDiff size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>Review file changes</div>
          <div style={subtitleStyle}>
            {request.filePath || diffReview?.toolName || "Tool edit"} · <span style={{ color: "var(--state-success)" }}>+{stats.plus}</span>{" "}
            <span style={{ color: "var(--state-danger)" }}>-{stats.minus}</span>
          </div>
        </div>
      </div>

      <div style={diffPreviewStyle}>
        {stats.preview.length > 0 ? stats.preview.map((line, index) => (
          <div key={`${index}-${line}`} style={diffLineStyle(line)}>
            {line}
          </div>
        )) : <span style={{ color: "var(--text-muted)" }}>Open the diff panel to inspect the proposed changes.</span>}
      </div>

      <div style={buttonRowStyle}>
        <button type="button" onClick={openDiff} style={secondaryButtonStyle}>
          <ExternalLink size={13} />
          Open diff
        </button>
        <button type="button" onClick={() => respond(false)} style={secondaryButtonStyle} aria-label="Deny file changes">
          <X size={13} />
          Deny
        </button>
        <button type="button" onClick={() => respond(true)} style={primaryButtonStyle} aria-label="Allow file changes">
          <Check size={13} />
          Allow
        </button>
      </div>
    </section>
  );
};

const AskUserCard = ({ request }: { request: PendingAskUser }) => {
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const hasOptions = Boolean(request.options && request.options.length > 0);

  useEffect(() => {
    setAnswer("");
    if (!hasOptions) window.setTimeout(() => inputRef.current?.focus(), 40);
  }, [request.requestId]);

  const respond = (text: string) => {
    getWebSocket()?.send({
      type: "answer",
      tool_call_id: request.requestId,
      answer: text,
    });
    useAppStore.getState().clearAskUser();
  };

  return (
    <section style={{ ...cardStyle, ...askUserCardStyle }}>
      <div style={headerStyle}>
        <MessageSquare size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>Agent needs input</div>
          <div style={askSubtitleStyle}>{request.question}</div>
        </div>
      </div>

      {hasOptions && (
        <div style={choiceGridStyle}>
          {request.options?.map((option) => (
            <button key={option} type="button" onClick={() => respond(option)} style={choiceCardStyle}>
              <span style={choiceCardTitleStyle}>{option}</span>
              <span style={choiceCardHintStyle}>Click to answer</span>
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (answer.trim()) respond(answer.trim());
        }}
        style={askInputRowStyle}
      >
        <div style={askInputWrapStyle}>
          {hasOptions && <div style={askInputLabelStyle}>Other answer</div>}
        <input
          ref={inputRef}
          value={answer}
          onChange={(event) => setAnswer(event.target.value)}
          placeholder={hasOptions ? "Type a custom answer..." : "Type your answer..."}
          style={inputStyle}
        />
        </div>
        <button type="submit" disabled={!answer.trim()} style={{ ...primaryButtonStyle, opacity: answer.trim() ? 1 : 0.55 }}>
          Send
        </button>
      </form>
    </section>
  );
};

const summarizeArgs = (args: Record<string, unknown>): { label: string; value: string }[] => {
  const preferred = ["command", "cmd", "path", "file_path", "target", "filename", "query", "pattern", "url", "cwd"];
  const rows: { label: string; value: string }[] = [];
  for (const key of preferred) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) rows.push({ label: humanizeKey(key), value });
    else if (typeof value === "number" || typeof value === "boolean") rows.push({ label: humanizeKey(key), value: String(value) });
  }
  if (rows.length > 0) return rows.slice(0, 4);
  const fallback = Object.entries(args)
    .filter(([, value]) => typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    .slice(0, 4)
    .map(([key, value]) => ({ label: humanizeKey(key), value: String(value) }));
  return fallback.length > 0 ? fallback : [{ label: "request", value: "No concise parameters available" }];
};

const humanizeKey = (key: string) => key.replace(/_/g, " ");

const diffStats = (diff: string) => {
  const lines = diff.split("\n");
  let plus = 0;
  let minus = 0;
  const preview: string[] = [];
  for (const line of lines) {
    if (line.startsWith("+") && !line.startsWith("+++")) plus++;
    else if (line.startsWith("-") && !line.startsWith("---")) minus++;
    if (preview.length < 8 && (line.startsWith("@@") || line.startsWith("+") || line.startsWith("-"))) {
      preview.push(line);
    }
  }
  return { plus, minus, preview };
};

const shellStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  width: "min(980px, calc(100% - 44px))",
  margin: "0 auto 8px",
  flexShrink: 0,
};

const approvalBarStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  background: "color-mix(in oklch, var(--surface-page) 92%, var(--accent-primary) 8%)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 9px",
  display: "grid",
  gridTemplateColumns: "26px minmax(0, 1fr) auto",
  alignItems: "center",
  gap: 9,
  overflow: "hidden",
};

const approvalIconStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-sm, 5px)",
  background: "color-mix(in oklch, var(--state-warning) 10%, var(--surface-soft))",
  border: "1px solid color-mix(in oklch, var(--state-warning) 28%, var(--border-subtle))",
};

const approvalMainStyle: React.CSSProperties = {
  minWidth: 0,
  display: "grid",
  gap: 3,
};

const approvalTitleRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  minWidth: 0,
};

const pendingPillStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "1px 6px",
  borderRadius: 999,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const compactSummaryRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
  overflow: "hidden",
  flex: 1,
};

const approvalArgStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  minWidth: 0,
  maxWidth: "min(320px, 45vw)",
  fontSize: "var(--text-xs)",
};

const approvalArgLabelStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
};

const approvalArgValueStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
};

const compactButtonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 6,
  flexShrink: 0,
  minWidth: "max-content",
};

const cardStyle: React.CSSProperties = {
  border: "1px solid color-mix(in oklch, var(--state-warning) 45%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--state-warning) 8%, var(--surface-page))",
  borderRadius: "var(--radius-sm, 6px)",
  padding: 10,
  display: "grid",
  gap: 9,
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 9,
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
};

const subtitleStyle: React.CSSProperties = {
  marginTop: 2,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const queueStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 7,
  flexWrap: "wrap",
};

const baseButtonStyle: React.CSSProperties = {
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 5,
  borderRadius: "var(--radius-sm, 4px)",
  padding: "0 10px",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: 0,
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
};

const accentButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: "1px solid color-mix(in oklch, var(--accent-primary) 40%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))",
  color: "var(--accent-primary)",
};

const secondaryButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
};

const askUserCardStyle: React.CSSProperties = {
  gap: 12,
  padding: 12,
};

const askSubtitleStyle: React.CSSProperties = {
  marginTop: 4,
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  lineHeight: 1.5,
};

const choiceGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 8,
};

const choiceCardStyle: React.CSSProperties = {
  minHeight: 62,
  display: "grid",
  gap: 4,
  alignContent: "center",
  justifyItems: "start",
  textAlign: "left",
  padding: "10px 12px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  cursor: "pointer",
};

const choiceCardTitleStyle: React.CSSProperties = {
  fontSize: "var(--text-sm)",
  fontWeight: 650,
  lineHeight: 1.35,
};

const choiceCardHintStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const askInputRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "end",
  flexWrap: "wrap",
};

const askInputWrapStyle: React.CSSProperties = {
  flex: 1,
  minWidth: "min(280px, 100%)",
  display: "grid",
  gap: 6,
};

const askInputLabelStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  textTransform: "uppercase",
};

const inputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  padding: "0 9px",
  height: 30,
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};

const diffPreviewStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  maxHeight: 140,
  overflow: "auto",
  padding: 8,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const diffLineStyle = (line: string): React.CSSProperties => ({
  color: line.startsWith("+") && !line.startsWith("+++")
    ? "var(--state-success)"
    : line.startsWith("-") && !line.startsWith("---")
      ? "var(--state-danger)"
      : line.startsWith("@@")
        ? "var(--accent-primary)"
        : "var(--text-secondary)",
  whiteSpace: "pre",
});
