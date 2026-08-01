import { useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLink, Code2, ShieldCheck } from "lucide-react";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { MonacoDiffView } from "../components/MonacoDiffView";
import { guessLanguageFromPath } from "../lib/monaco-colorize";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { pushToast } from "./ToastContainer";

export const DiffReviewModal = () => {
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const visibleDiffReview = pendingPromptTargetsConversation(pendingDiffReview, activeConversationId, activeConversationId)
    ? pendingDiffReview
    : null;
  const containerRef = useFocusTrap(Boolean(visibleDiffReview));
  const [useMonaco, setUseMonaco] = useState(false);
  const [persistingRule, setPersistingRule] = useState(false);

  const submitDecision = useCallback((accepted: boolean) => {
    if (!visibleDiffReview) return;
    const sent = sendClientCommand(buildApprovalResponseCommand(
      visibleDiffReview.requestId,
      accepted ? "approve" : "reject",
      visibleDiffReview.protocol,
    ));
    if (!sent) return;
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === visibleDiffReview.requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: accepted ? "approved" : "rejected",
      });
    }
    useAppStore.getState().clearDiffReview(visibleDiffReview.requestId);
  }, [visibleDiffReview]);

  const respond = useCallback((accepted: boolean) => {
    if (persistingRule) return;
    submitDecision(accepted);
  }, [persistingRule, submitDecision]);

  // "Always allow edits here": persist an Edit(<dir>/**) content rule so future
  // edits under the same directory skip the review, then approve this one.
  const alwaysAllowHere = useCallback(async () => {
    if (persistingRule) return;
    if (!visibleDiffReview?.filePath) {
      submitDecision(true);
      return;
    }
    const normalized = visibleDiffReview.filePath.replace(/\\/g, "/");
    const slash = normalized.lastIndexOf("/");
    const glob = slash >= 0 ? `${normalized.slice(0, slash)}/**` : normalized;
    const rule = `Edit(${glob})`;
    setPersistingRule(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "permissions.content_rule.add",
        rule,
        deny: false,
        source: "diff_review.always_allow",
      }, "permissions.content_rule.add");
      const failed = ["error", "failed", "warning"].includes(String(result.level || "").toLowerCase());
      if (failed || result.data?.rule !== rule || result.data?.deny === true) {
        throw new Error(result.message || "The permission rule was not saved.");
      }
      submitDecision(true);
    } catch (error) {
      pushToast(
        error instanceof Error ? error.message : "Failed to save the permission rule.",
        "error",
        4200,
      );
    } finally {
      setPersistingRule(false);
    }
  }, [persistingRule, visibleDiffReview, submitDecision]);

  useEffect(() => {
    setPersistingRule(false);
  }, [visibleDiffReview?.requestId]);

  useEffect(() => {
    if (!visibleDiffReview) return;
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest("button")) return;
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !isTextEditingTarget(e.target))) {
        e.preventDefault();
        respond(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        respond(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [visibleDiffReview, respond]);

  const { lines, stats } = useMemo(() => {
    const raw = visibleDiffReview?.diff ?? "";
    const ls = raw.split("\n");
    let plus = 0, minus = 0;
    for (const l of ls) {
      if (l.startsWith("+") && !l.startsWith("+++")) plus++;
      else if (l.startsWith("-") && !l.startsWith("---")) minus++;
    }
    return { lines: ls, stats: { plus, minus } };
  }, [visibleDiffReview?.diff]);

  if (!visibleDiffReview) return null;

  const openDiffPanel = () => {
    useAppStore.getState().addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "Diff Review",
    });
  };

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
            <Code2 size={14} />
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
            <button disabled={persistingRule} onClick={openDiffPanel} className="border rounded cursor-pointer inline-flex items-center gap-1.5 py-2 px-3" style={openBtn}>
              <ExternalLink size={14} /> Open in Diff
            </button>
            <button disabled={persistingRule} onClick={() => respond(false)} className="border rounded py-2 px-5 cursor-pointer" style={rejectBtn}>
              Reject
            </button>
            {visibleDiffReview.filePath && (
              <button
                onClick={alwaysAllowHere}
                disabled={persistingRule}
                title="Approve and don't ask again for edits under this path"
                className="border rounded py-2 px-3 cursor-pointer inline-flex items-center gap-1.5"
                style={alwaysBtn}
              >
                <ShieldCheck size={14} /> {persistingRule ? "Saving…" : "Always here"}
              </button>
            )}
            <button disabled={persistingRule} onClick={() => respond(true)} className="border-0 rounded py-2 px-5 font-semibold cursor-pointer" style={acceptBtn}>
              Accept
            </button>
          </div>
        </div>
      </div>
    </div>
  );
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

const alwaysBtn: React.CSSProperties = {
  background: "color-mix(in oklch, var(--state-success) 12%, var(--surface-soft))",
  color: "var(--state-success)",
  borderColor: "color-mix(in oklch, var(--state-success) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 6px)",
  fontSize: "var(--text-sm)",
};
