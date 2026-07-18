import { useState, useEffect, useRef, useCallback } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildAskUserResponseCommand } from "../protocol/prompt-responses";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { useFocusTrap } from "../hooks/useFocusTrap";

export const AskUserPrompt = () => {
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const visibleAskUser = pendingPromptTargetsConversation(pendingAskUser, activeConversationId, activeConversationId)
    ? pendingAskUser
    : null;
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useFocusTrap(Boolean(visibleAskUser));
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
    const handler = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (e.key === "Enter" && !e.shiftKey && answer.trim()) {
        e.preventDefault();
        respond(answer.trim());
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
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
            {visibleAskUser.options?.map((opt, index) => (
              <button
                key={opt}
                className="ask-user-option-card"
                onClick={() => respond(opt)}
                style={optionCardStyle}
              >
                <span className="ask-user-option-letter" style={optionLetterStyle}>{optionLetter(index)}</span>
                <span style={optionCardTitleStyle}>{opt}</span>
              </button>
            ))}
          </div>
        )}
        <div style={customAnswerRowStyle}>
          <div style={customAnswerWrapStyle}>
            {hasOptions && (
              <div style={customAnswerLabelStyle}>
                <span className="ask-user-option-letter" style={optionLetterStyle}>{optionLetter(visibleAskUser.options?.length ?? 0)}</span>
                自定义回答
              </div>
            )}
          <input
            ref={inputRef}
            className="ask-user-input"
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
              width: "100%",
            }}
          />
          </div>
          <button
            className="ask-user-send"
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

const optionGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 10,
};

const optionCardStyle: React.CSSProperties = {
  minHeight: 42,
  display: "grid",
  gridTemplateColumns: "24px minmax(0, 1fr)",
  alignItems: "center",
  gap: 9,
  textAlign: "left",
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 10px",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
};

const optionLetterStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 22,
  height: 22,
  borderRadius: "var(--radius-sm, 5px)",
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 750,
  lineHeight: 1,
  flexShrink: 0,
};

const optionCardTitleStyle: React.CSSProperties = {
  fontSize: "var(--text-sm)",
  fontWeight: 650,
  minWidth: 0,
  overflowWrap: "anywhere",
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
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};

const sendBtn: React.CSSProperties = {
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
  border: 0,
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 16px",
  fontWeight: 600,
  fontSize: "var(--text-sm)",
};

function optionLetter(index: number): string {
  return String.fromCharCode(65 + Math.max(0, index));
}
