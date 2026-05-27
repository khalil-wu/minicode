import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  Code2,
  FileText,
  Globe,
  Search,
  TerminalSquare,
  Wrench,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";

export const ApprovalModal = () => {
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const queuedCount = approvalQueue.length;
  const [respondingId, setRespondingId] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const respond = useCallback((approved: boolean) => {
    if (!pendingApproval || respondingId === pendingApproval.requestId) return;
    setRespondingId(pendingApproval.requestId);
    const sent = sendClientCommand({
      type: "approval",
      tool_call_id: pendingApproval.requestId,
      action: approved ? "approve" : "reject",
    });
    if (sent) {
      useAppStore.getState().markApprovalSubmitted(pendingApproval.requestId);
      window.setTimeout(() => useAppStore.getState().clearApproval(pendingApproval.requestId), 250);
    } else {
      useAppStore.getState().markApprovalError(pendingApproval.requestId, "Connection is offline");
      setRespondingId(null);
    }
  }, [pendingApproval, respondingId]);

  const approveAll = useCallback(() => {
    if (!pendingApproval) return;
    const store = useAppStore.getState();
    const all = [pendingApproval, ...store.approvalQueue];
    const submitted: string[] = [];
    for (const item of all) {
      const sent = sendClientCommand({
        type: "approval",
        tool_call_id: item.requestId,
        action: "approve",
      });
      if (sent) submitted.push(item.requestId);
      else store.markApprovalError(item.requestId, "Connection is offline");
    }
    for (const id of submitted) store.markApprovalSubmitted(id);
    window.setTimeout(() => store.clearApprovals(submitted), 250);
  }, [pendingApproval]);

  useEffect(() => {
    setRespondingId(null);
  }, [pendingApproval?.requestId]);

  useEffect(() => {
    if (!pendingApproval) return;
    const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusFirst = () => {
      const focusable = getFocusable(dialogRef.current);
      (focusable[0] ?? dialogRef.current)?.focus();
    };
    window.setTimeout(focusFirst, 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        respond(false);
      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey || !isTextEditingTarget(event.target))) {
        event.preventDefault();
        respond(true);
      } else if (event.key === "Tab") {
        const focusable = getFocusable(dialogRef.current);
        if (focusable.length === 0) {
          event.preventDefault();
          dialogRef.current?.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      previousActive?.focus();
    };
  }, [pendingApproval, respond]);

  if (!pendingApproval) return null;

  const isResponding = respondingId === pendingApproval.requestId || pendingApproval.status === "submitted";
  const totalPending = 1 + queuedCount;
  const summary = summarizeArgs(pendingApproval.args);

  return (
    <div
      className="overlay-backdrop"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 110,
      }}
    >
      <div
        ref={dialogRef}
        className="modal-content"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-modal-title"
        tabIndex={-1}
        style={{
          width: "min(520px, 92vw)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-sm, 8px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          padding: 14,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <div style={modalHeaderStyle}>
          <div style={modalIconStyle}>
            <ToolGlyph name={pendingApproval.toolName} />
          </div>
          <div style={{ minWidth: 0, flex: 1 }}>
            <h3 id="approval-modal-title" style={modalTitleStyle}>
              Allow {toolDisplayName(pendingApproval.toolName)}?
            </h3>
            <div style={modalSubtitleStyle}>Permission required before this tool can run.</div>
          </div>
          {totalPending > 1 && (
            <span style={pendingPillStyle}>
              {totalPending} pending
            </span>
          )}
        </div>
        {queuedCount > 0 && (
          <div style={queueStyle}>
            Next: {approvalQueue.map((a) => `${toolDisplayName(a.toolName)}${formatApprovalTarget(a.args)}`).join(", ")}
          </div>
        )}
        <div style={summaryBoxStyle}>
          {summary.slice(0, 3).map((item) => (
            <div key={item.label} style={summaryRowStyle} title={`${item.label}: ${item.value}`}>
              <span style={summaryLabelStyle}>{item.label}</span>
              <span style={summaryValueStyle}>{item.value}</span>
            </div>
          ))}
          <details style={detailsStyle}>
            <summary style={detailsSummaryStyle}>Raw parameters</summary>
            <pre style={rawJsonStyle}>
              {JSON.stringify(pendingApproval.args, null, 2)}
            </pre>
          </details>
        </div>
        {pendingApproval.status === "submitted" && (
          <div style={statusStyle}>Approval submitted.</div>
        )}
        {pendingApproval.status === "error" && pendingApproval.error && (
          <div style={{ ...statusStyle, color: "var(--state-danger)" }}>{pendingApproval.error}</div>
        )}
        <div style={buttonRowStyle}>
          <button
            aria-label="Deny tool approval"
            disabled={isResponding}
            onClick={() => respond(false)}
            style={{ ...denyBtn, opacity: isResponding ? 0.6 : 1 }}
          >
            <X size={13} />
            Deny
          </button>
          <button
            aria-label="Approve tool approval"
            disabled={isResponding}
            onClick={() => respond(true)}
            title="Approve (Enter or Ctrl+Enter)"
            style={{ ...approveBtn, opacity: isResponding ? 0.6 : 1 }}
          >
            <Check size={13} />
            Allow
          </button>
          {queuedCount > 0 && (
            <button
              aria-label="Approve all pending approvals"
              disabled={isResponding}
              onClick={approveAll}
              style={{ ...approveAllBtn, opacity: isResponding ? 0.6 : 1 }}
            >
              Allow all
            </button>
          )}
        </div>
      </div>
    </div>
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

const getFocusable = (root: HTMLElement | null): HTMLElement[] => {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("disabled") && element.offsetParent !== null);
};

const isTextEditingTarget = (target: EventTarget | null): boolean => {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || target.isContentEditable;
};

const formatApprovalTarget = (args: Record<string, unknown>): string => {
  const value = args.file_path ?? args.path ?? args.command ?? args.cwd;
  if (typeof value !== "string" || !value.trim()) return "";
  const text = value.length > 32 ? `${value.slice(0, 14)}...${value.slice(-14)}` : value;
  return ` (${text})`;
};

const modalHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  minWidth: 0,
};

const modalIconStyle: React.CSSProperties = {
  width: 28,
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-sm, 6px)",
  background: "color-mix(in oklch, var(--state-warning) 10%, var(--surface-soft))",
  border: "1px solid color-mix(in oklch, var(--state-warning) 28%, var(--border-subtle))",
  flexShrink: 0,
};

const modalTitleStyle: React.CSSProperties = {
  margin: 0,
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const modalSubtitleStyle: React.CSSProperties = {
  marginTop: 2,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const pendingPillStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "1px 7px",
  borderRadius: 999,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const queueStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const summaryBoxStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: 9,
};

const summaryRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "76px minmax(0, 1fr)",
  gap: 8,
  alignItems: "center",
  minWidth: 0,
  fontSize: "var(--text-xs)",
};

const summaryLabelStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const summaryValueStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const detailsStyle: React.CSSProperties = {
  borderTop: "1px solid var(--border-subtle)",
  paddingTop: 6,
  marginTop: 2,
};

const detailsSummaryStyle: React.CSSProperties = {
  cursor: "pointer",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const rawJsonStyle: React.CSSProperties = {
  margin: "7px 0 0",
  padding: 8,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  maxHeight: 180,
  overflow: "auto",
};

const statusStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 7,
  justifyContent: "flex-end",
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

const approveBtn: React.CSSProperties = {
  ...baseButtonStyle,
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
  border: 0,
};

const denyBtn: React.CSSProperties = {
  ...baseButtonStyle,
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
};

const approveAllBtn: React.CSSProperties = {
  ...baseButtonStyle,
  background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))",
  color: "var(--accent-primary)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 40%, var(--border-subtle))",
};
