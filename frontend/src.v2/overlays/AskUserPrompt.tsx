import { useState, useEffect, useRef, useCallback } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildAskUserResponseCommand } from "../protocol/prompt-responses";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";

export const AskUserPrompt = () => {
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const visibleAskUser = pendingPromptTargetsConversation(pendingAskUser, activeConversationId, activeConversationId)
    ? pendingAskUser
    : null;
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const hasOptions = Boolean(visibleAskUser?.options && visibleAskUser.options.length > 0);

  const respond = useCallback((text: string) => {
    if (!visibleAskUser) return;
    const ws = getWebSocket();
    ws?.send(buildAskUserResponseCommand(visibleAskUser.requestId, text, visibleAskUser.protocol));
    useAppStore.getState().clearAskUser();
  }, [visibleAskUser]);

  const cancel = useCallback(() => {
    if (!visibleAskUser) return;
    useAppStore.getState().clearAskUser();
  }, [visibleAskUser]);

  useEffect(() => {
    if (visibleAskUser) {
      setAnswer("");
      if (!visibleAskUser.options || visibleAskUser.options.length === 0) {
        setTimeout(() => inputRef.current?.focus(), 50);
      }
    }
  }, [visibleAskUser]);

  useEffect(() => {
    if (!visibleAskUser) return;
    const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusFirst = () => {
      const focusable = getFocusable(dialogRef.current);
      (focusable[0] ?? dialogRef.current)?.focus();
    };
    window.setTimeout(focusFirst, 0);
    const handler = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (e.key === "Enter" && !e.shiftKey && answer.trim()) {
        e.preventDefault();
        respond(answer.trim());
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      } else if (e.key === "Tab") {
        const focusable = getFocusable(dialogRef.current);
        if (focusable.length === 0) {
          e.preventDefault();
          dialogRef.current?.focus();
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
  }, [answer, visibleAskUser, respond, cancel]);

  if (!visibleAskUser) return null;

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
        // 🔧 移除模糊效果 - 用户需要看清背景内容
      }}
    >
      <div
        ref={dialogRef}
        className="modal-content"
        role="dialog"
        aria-modal="true"
        aria-label="Agent needs input"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(520px, 90vw)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <h3 style={{ margin: 0, color: "var(--text-primary)", fontSize: "var(--text-md)" }}>
          Agent needs input
        </h3>
        <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
          {visibleAskUser.question}
        </p>
        {hasOptions && (
          <div style={optionGridStyle}>
            {visibleAskUser.options?.map((opt) => (
              <button
                key={opt}
                onClick={() => respond(opt)}
                style={optionCardStyle}
              >
                <span style={optionCardTitleStyle}>{opt}</span>
                <span style={optionCardHintStyle}>Click to answer</span>
              </button>
            ))}
          </div>
        )}
        <div style={customAnswerRowStyle}>
          <div style={customAnswerWrapStyle}>
            {hasOptions && <div style={customAnswerLabelStyle}>Other answer</div>}
          <input
            ref={inputRef}
            type="text"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder={hasOptions ? "Type a custom answer..." : "Type your answer..."}
            style={{
              padding: "8px 12px",
              background: "var(--surface-base)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-sm, 6px)",
              color: "var(--text-primary)",
              fontSize: "var(--text-sm)",
              outline: "none",
              width: "100%",
            }}
          />
          </div>
          <button
            onClick={() => answer.trim() && respond(answer.trim())}
            disabled={!answer.trim()}
            style={{
              ...sendBtn,
              opacity: answer.trim() ? 1 : 0.5,
              cursor: answer.trim() ? "pointer" : "not-allowed",
            }}
          >
            Send
          </button>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            Enter to send
          </span>
          <button
            onClick={cancel}
            className="btn-ghost"
            style={{
              border: 0,
              borderRadius: "var(--radius-sm, 6px)",
              padding: "4px 10px",
              cursor: "pointer",
              fontSize: "var(--text-xs)",
            }}
          >
            Cancel
          </button>
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

const optionGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 10,
};

const optionCardStyle: React.CSSProperties = {
  minHeight: 72,
  display: "grid",
  gap: 4,
  alignContent: "center",
  justifyItems: "start",
  textAlign: "left",
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
  padding: "10px 14px",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
};

const optionCardTitleStyle: React.CSSProperties = {
  fontSize: "var(--text-sm)",
  fontWeight: 650,
};

const optionCardHintStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const customAnswerRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "end",
  flexWrap: "wrap",
};

const customAnswerWrapStyle: React.CSSProperties = {
  flex: 1,
  minWidth: "min(280px, 100%)",
  display: "grid",
  gap: 6,
};

const customAnswerLabelStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  textTransform: "uppercase",
};

const sendBtn: React.CSSProperties = {
  background: "var(--accent-primary)",
  color: "var(--text-primary)",
  border: 0,
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 16px",
  fontWeight: 600,
  fontSize: "var(--text-sm)",
};
