import { useState, useEffect, useRef, useCallback } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";

export const AskUserPrompt = () => {
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const hasOptions = Boolean(pendingAskUser?.options && pendingAskUser.options.length > 0);

  const respond = useCallback((text: string) => {
    if (!pendingAskUser) return;
    const ws = getWebSocket();
    ws?.send({
      type: "answer",
      tool_call_id: pendingAskUser.requestId,
      answer: text,
    });
    useAppStore.getState().clearAskUser();
  }, [pendingAskUser]);

  useEffect(() => {
    if (pendingAskUser) {
      setAnswer("");
      if (!pendingAskUser.options || pendingAskUser.options.length === 0) {
        setTimeout(() => inputRef.current?.focus(), 50);
      }
    }
  }, [pendingAskUser]);

  useEffect(() => {
    if (!pendingAskUser) return;
    const handler = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (e.key === "Enter" && !e.shiftKey && answer.trim()) {
        e.preventDefault();
        respond(answer.trim());
      } else if (e.key === "Escape") {
        e.preventDefault();
        respond("");
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [answer, pendingAskUser, respond]);

  if (!pendingAskUser) return null;

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
        backdropFilter: "blur(4px)",
      }}
    >
      <div
        className="modal-content"
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
          {pendingAskUser.question}
        </p>
        {hasOptions && (
          <div style={optionGridStyle}>
            {pendingAskUser.options?.map((opt) => (
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
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          Enter to send, Esc to dismiss
        </span>
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
