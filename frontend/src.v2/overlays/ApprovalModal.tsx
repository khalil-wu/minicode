import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { ToolGlyph, toolDisplayName, summarizeArgs, humanizeKey } from "../chat/toolUtils";

export const ApprovalModal = () => {
  const pendingApprovalState = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const visibleApproval = pendingPromptTargetsConversation(pendingApprovalState, activeConversationId, activeConversationId)
    ? pendingApprovalState
    : null;
  const visibleQueue = approvalQueue.filter((item) =>
    pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
  );
  const queuedCount = visibleQueue.length;
  const [respondingId, setRespondingId] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const clearTimers = useRef<Set<number>>(new Set());

  const scheduleClear = (ids: string[]) => {
    const id = window.setTimeout(() => {
      clearTimers.current.delete(id);
      if (ids.length === 1) {
        useAppStore.getState().clearApproval(ids[0]);
      } else {
        useAppStore.getState().clearApprovals(ids);
      }
    }, 250);
    clearTimers.current.add(id);
  };

  // Clean up pending timers on unmount only
  useEffect(() => {
    const timers = clearTimers.current;
    return () => {
      timers.forEach((id) => window.clearTimeout(id));
      timers.clear();
    };
  }, []);

  const respond = useCallback((approved: boolean) => {
    if (!visibleApproval || respondingId === visibleApproval.requestId) return;
    setRespondingId(visibleApproval.requestId);
    const sent = sendClientCommand(buildApprovalResponseCommand(
      visibleApproval.requestId,
      approved ? "approve" : "reject",
      visibleApproval.protocol,
    ));
    if (sent) {
      useAppStore.getState().markApprovalSubmitted(visibleApproval.requestId);
      scheduleClear([visibleApproval.requestId]);
    } else {
      useAppStore.getState().markApprovalError(visibleApproval.requestId, "Connection is offline");
      setRespondingId(null);
    }
  }, [visibleApproval, respondingId]);

  const approveAll = useCallback(() => {
    if (!visibleApproval) return;
    const store = useAppStore.getState();
    const all = [visibleApproval, ...store.approvalQueue.filter((item) =>
      pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
    )];
    const submitted: string[] = [];
    for (const item of all) {
      const sent = sendClientCommand(buildApprovalResponseCommand(item.requestId, "approve", item.protocol));
      if (sent) submitted.push(item.requestId);
      else store.markApprovalError(item.requestId, "Connection is offline");
    }
    for (const id of submitted) store.markApprovalSubmitted(id);
    scheduleClear(submitted);
  }, [activeConversationId, visibleApproval]);

  useEffect(() => {
    setRespondingId(null);
  }, [visibleApproval?.requestId]);

  useEffect(() => {
    if (!visibleApproval) return;
    const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusFirst = () => {
      const focusable = getFocusable(dialogRef.current);
      (focusable[0] ?? dialogRef.current)?.focus();
    };
    const focusTimeoutId = window.setTimeout(focusFirst, 0);
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
      window.clearTimeout(focusTimeoutId);
      window.removeEventListener("keydown", handleKeyDown);
      previousActive?.focus();
    };
  }, [visibleApproval, respond]);

  if (!visibleApproval) return null;

  const pendingApproval = visibleApproval;
  const isResponding = respondingId === pendingApproval.requestId || pendingApproval.status === "submitted";
  const totalPending = 1 + queuedCount;
  const summary = summarizeArgs(pendingApproval.args);

  return (
    <div
      className="overlay-backdrop"
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: "var(--z-approval)",
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
            Next: {visibleQueue.map((a) => `${toolDisplayName(a.toolName)}${formatApprovalTarget(a.args)}`).join(", ")}
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
