import { useCallback, useEffect, useMemo, useRef } from "react";
import { ExternalLink } from "lucide-react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";

export const DiffReviewModal = () => {
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const containerRef = useRef<HTMLDivElement>(null);

  const respond = useCallback((accepted: boolean) => {
    if (!pendingDiffReview) return;
    const ws = getWebSocket();
    ws?.send({
      type: "approval",
      tool_call_id: pendingDiffReview.requestId,
      action: accepted ? "approve" : "reject",
    });
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === pendingDiffReview.requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: accepted ? "approved" : "rejected",
      });
    }
    useAppStore.getState().clearDiffReview();
  }, [pendingDiffReview]);

  useEffect(() => {
    if (!pendingDiffReview) return;
    const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusFirst = () => {
      const focusable = getFocusable(containerRef.current);
      (focusable[0] ?? containerRef.current)?.focus();
    };
    window.setTimeout(focusFirst, 0);
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !isTextEditingTarget(e.target))) {
        e.preventDefault();
        respond(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        respond(false);
      } else if (e.key === "Tab") {
        const focusable = getFocusable(containerRef.current);
        if (focusable.length === 0) {
          e.preventDefault();
          containerRef.current?.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      previousActive?.focus();
    };
  }, [pendingDiffReview, respond]);

  if (!pendingDiffReview) return null;

  const openDiffPanel = () => {
    useAppStore.getState().addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "Diff Review",
    });
  };

  const { lines, stats } = useMemo(() => {
    const raw = pendingDiffReview.diff;
    const ls = raw.split("\n");
    let plus = 0, minus = 0;
    for (const l of ls) {
      if (l.startsWith("+") && !l.startsWith("+++")) plus++;
      else if (l.startsWith("-") && !l.startsWith("---")) minus++;
    }
    return { lines: ls, stats: { plus, minus } };
  }, [pendingDiffReview.diff]);

  return (
    <div
      className="overlay-backdrop"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 110,
        backdropFilter: "blur(4px)",
      }}
      onClick={() => respond(false)}
    >
      <div
        className="modal-content"
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="diff-review-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(780px, 92vw)",
          maxHeight: "82vh",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          padding: 20,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h3 id="diff-review-title" style={{ margin: 0, color: "var(--text-primary)", fontSize: "var(--text-md)", flex: 1 }}>
            Diff Review
            {pendingDiffReview.filePath && (
              <span style={{ marginLeft: 10, fontSize: "var(--text-sm)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontWeight: 400 }}>
                {pendingDiffReview.filePath}
              </span>
            )}
          </h3>
          <span style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)" }}>
            <span style={{ color: "var(--state-success)" }}>+{stats.plus}</span>
            {" "}
            <span style={{ color: "var(--state-danger)" }}>-{stats.minus}</span>
          </span>
        </div>
        <div
          style={{
            flex: 1,
            overflow: "auto",
            background: "var(--surface-base)",
            borderRadius: "var(--radius-sm, 6px)",
            border: "1px solid var(--border-subtle)",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-xs)",
            lineHeight: 1.7,
          }}
        >
          {lines.map((line, i) => {
            let color = "var(--text-secondary)";
            let bg = "transparent";
            if (line.startsWith("+") && !line.startsWith("+++")) {
              color = "var(--state-success)";
              bg = "color-mix(in oklch, var(--state-success) 8%, transparent)";
            } else if (line.startsWith("-") && !line.startsWith("---")) {
              color = "var(--state-danger)";
              bg = "color-mix(in oklch, var(--state-danger) 8%, transparent)";
            } else if (line.startsWith("@@")) {
              color = "var(--accent-primary)";
              bg = "color-mix(in oklch, var(--accent-primary) 5%, transparent)";
            } else if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("---") || line.startsWith("+++")) {
              color = "var(--text-muted)";
            }
            return (
              <div key={i} style={{ display: "flex", background: bg }}>
                <span style={{ width: 40, flexShrink: 0, textAlign: "right", padding: "0 8px 0 0", color: "var(--text-muted)", opacity: 0.5, userSelect: "none" }}>
                  {i + 1}
                </span>
                <span style={{ color, padding: "0 8px", whiteSpace: "pre", flex: 1 }}>
                  {line}
                </span>
              </div>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            Enter accepts, Esc rejects
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={openDiffPanel} style={openBtn}>
              <ExternalLink size={14} /> Open in Diff
            </button>
            <button onClick={() => respond(false)} style={rejectBtn}>
              Reject
            </button>
            <button onClick={() => respond(true)} style={acceptBtn}>
              Accept
            </button>
          </div>
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

const acceptBtn: React.CSSProperties = {
  background: "var(--state-success)",
  color: "var(--text-on-accent)",
  border: 0,
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 20px",
  fontWeight: 600,
  cursor: "pointer",
  fontSize: "var(--text-sm)",
};

const rejectBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 20px",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
};

const openBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 12px",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
};
