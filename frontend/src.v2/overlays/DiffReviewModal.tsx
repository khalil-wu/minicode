import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, Code2 } from "lucide-react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { MonacoDiffView } from "../components/MonacoDiffView";
import { guessLanguageFromPath } from "../lib/monaco-colorize";

export const DiffReviewModal = () => {
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const visibleDiffReview = pendingPromptTargetsConversation(pendingDiffReview, activeConversationId, activeConversationId)
    ? pendingDiffReview
    : null;
  const containerRef = useRef<HTMLDivElement>(null);
  const [useMonaco, setUseMonaco] = useState(false);

  const respond = useCallback((accepted: boolean) => {
    if (!visibleDiffReview) return;
    const ws = getWebSocket();
    ws?.send(buildApprovalResponseCommand(
      visibleDiffReview.requestId,
      accepted ? "approve" : "reject",
      visibleDiffReview.protocol,
    ));
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === visibleDiffReview.requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: accepted ? "approved" : "rejected",
      });
    }
    useAppStore.getState().clearDiffReview();
  }, [visibleDiffReview]);

  useEffect(() => {
    if (!visibleDiffReview) return;
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
  }, [visibleDiffReview, respond]);

  if (!visibleDiffReview) return null;

  const openDiffPanel = () => {
    useAppStore.getState().addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "Diff Review",
    });
  };

  const { lines, stats } = useMemo(() => {
    const raw = visibleDiffReview.diff;
    const ls = raw.split("\n");
    let plus = 0, minus = 0;
    for (const l of ls) {
      if (l.startsWith("+") && !l.startsWith("+++")) plus++;
      else if (l.startsWith("-") && !l.startsWith("---")) minus++;
    }
    return { lines: ls, stats: { plus, minus } };
  }, [visibleDiffReview.diff]);

  return (
    <div
      className="overlay-backdrop fixed inset-0 flex items-center justify-center"
      style={{
        background: "rgba(0,0,0,0.6)",
        zIndex: "var(--z-approval)",
      }}
      onClick={() => respond(false)}
    >
      <div
        className="modal-content border shadow-lg p-5 flex flex-col gap-3"
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
          borderColor: "var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
        }}
      >
        <div className="flex items-center gap-3">
          <h3 id="diff-review-title" className="m-0 flex-1" style={{ color: "var(--text-primary)", fontSize: "var(--text-md)" }}>
            Diff Review
            {visibleDiffReview.filePath && (
              <span className="ml-2.5 font-normal" style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {visibleDiffReview.filePath}
              </span>
            )}
          </h3>
          <span style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)" }}>
            <span style={{ color: "var(--state-success)" }}>+{stats.plus}</span>
            {" "}
            <span style={{ color: "var(--state-danger)" }}>-{stats.minus}</span>
          </span>
          <button
            onClick={() => setUseMonaco((v) => !v)}
            title={useMonaco ? "Switch to simple view" : "Switch to Monaco diff view"}
            className="border rounded cursor-pointer inline-flex items-center gap-1.5 px-2 py-1"
            style={{
              background: "var(--surface-soft)",
              color: "var(--text-primary)",
              borderColor: "var(--border-subtle)",
              borderRadius: "var(--radius-sm, 6px)",
              fontSize: "var(--text-xs)",
              opacity: useMonaco ? 1 : 0.7,
            }}
          >
            <Code2 size={13} />
            Monaco
          </button>
        </div>
        {useMonaco ? (
          <div className="flex-1 min-h-0 overflow-hidden">
            <MonacoDiffView
              patch={visibleDiffReview.diff}
              filePath={visibleDiffReview.filePath}
              language={visibleDiffReview.filePath ? guessLanguageFromPath(visibleDiffReview.filePath) : "plaintext"}
              height="100%"
            />
          </div>
        ) : (
          <div
            className="flex-1 overflow-auto border rounded leading-[1.7]"
            style={{
              background: "var(--surface-base)",
              borderRadius: "var(--radius-sm, 6px)",
              borderColor: "var(--border-subtle)",
              fontFamily: "var(--font-mono)",
              fontSize: "var(--text-xs)",
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
                <div key={i} className="flex" style={{ background: bg }}>
                  <span className="w-10 shrink-0 text-right pr-2 select-none opacity-50" style={{ color: "var(--text-muted)" }}>
                    {i + 1}
                  </span>
                  <span className="px-2 whitespace-pre flex-1" style={{ color }}>
                    {line}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        <div className="flex gap-2 justify-between items-center">
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            Enter accepts, Esc rejects
          </span>
          <div className="flex gap-2">
            <button onClick={openDiffPanel} className="border rounded cursor-pointer inline-flex items-center gap-1.5 py-2 px-3" style={openBtn}>
              <ExternalLink size={14} /> Open in Diff
            </button>
            <button onClick={() => respond(false)} className="border rounded py-2 px-5 cursor-pointer" style={rejectBtn}>
              Reject
            </button>
            <button onClick={() => respond(true)} className="border-0 rounded py-2 px-5 font-semibold cursor-pointer" style={acceptBtn}>
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
  fontSize: "var(--text-sm)",
};

const rejectBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  borderColor: "var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  fontSize: "var(--text-sm)",
};

const openBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  borderColor: "var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  fontSize: "var(--text-sm)",
};
