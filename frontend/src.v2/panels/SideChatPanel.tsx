import { useEffect, useMemo, useRef } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendChatMessage } from "../chat/sendChatMessage";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { ToolCallCard } from "../chat/tool-calls/ToolCallCard";
import { StreamingCursor } from "../chat/messages/StreamingCursor";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { toBackendPermissionMode } from "../protocol/permissions";

const newSideChatId = (): string =>
  `side-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

export const SideChatPanel = () => {
  const sideChats = useAppStore((s) => s.sideChats);
  const ensureSideChat = useAppStore((s) => s.ensureSideChat);
  const removeSideChat = useAppStore((s) => s.removeSideChat);
  const setDraft = useAppStore((s) => s.setSideChatDraft);
  const startMessage = useAppStore((s) => s.startSideChatMessage);

  const idRef = useRef<string>("");
  if (!idRef.current) {
    idRef.current = newSideChatId();
  }
  const id = idRef.current;
  const thread = sideChats[id];

  useEffect(() => {
    ensureSideChat(id);
    const ws = getWebSocket();
    ws?.send({
      type: "conversation.create",
      conversation_id: id,
      title: "Side chat",
      side_chat: true,
      permission_mode: toBackendPermissionMode(useAppStore.getState().permissionMode),
    });
    return () => {
      const wsClose = getWebSocket();
      wsClose?.send({
        type: "conversation.delete",
        conversation_id: id,
      });
      removeSideChat(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [thread?.messages.length, thread?.messages.at(-1)?.content]);

  const submit = () => {
    const content = (thread?.draft ?? "").trim();
    if (!content || !thread || thread.isStreaming) return;
    const inheritedPrefix = thread.messages.length === 0 && thread.inheritedContext
      ? `${thread.inheritedContext}\n\nSide-chat question:\n`
      : "";
    const sent = sendChatMessage({
      displayContent: content,
      backendContent: `${inheritedPrefix}${content}`,
      conversationId: id,
      allowWhileStreaming: false,
      skipLocalAppend: true,
    });
    if (sent) startMessage(id, content);
  };

  const stop = () => {
    const ws = getWebSocket();
    ws?.send({ type: "interrupt", conversation_id: id });
  };

  const sendDisabled = useMemo(
    () => !thread || thread.isStreaming || !(thread?.draft ?? "").trim(),
    [thread],
  );

  if (!thread) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div
        style={{
          padding: "6px 10px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ flex: 1 }}>
          Side chat / inherited context /{" "}
          <span style={{ fontFamily: "var(--font-mono)" }}>{id}</span>
        </span>
        <span
          style={{
            color: thread.isStreaming ? "var(--state-info)" : "var(--text-muted)",
          }}
        >
          {thread.isStreaming ? "streaming..." : "idle"}
        </span>
      </div>

      <div
        ref={listRef}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "12px 14px",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        {thread.messages.length === 0 ? (
          <div
            style={{
              color: "var(--text-muted)",
              fontSize: "var(--text-sm)",
              textAlign: "center",
              padding: 20,
            }}
          >
            This side chat inherits a compact summary of the current thread, then stays separate.
            {thread.inheritedContext && (
              <div style={{
                marginTop: 10,
                padding: 10,
                border: "1px solid var(--border-subtle)",
                borderRadius: "var(--radius-sm, 6px)",
                background: "var(--surface-page)",
                color: "var(--text-muted)",
                fontFamily: "var(--font-mono)",
                fontSize: "var(--text-xs)",
                textAlign: "left",
                whiteSpace: "pre-wrap",
                maxHeight: 120,
                overflow: "auto",
              }}>
                {thread.inheritedContext}
              </div>
            )}
          </div>
        ) : (
          thread.messages.map((m) =>
            m.role === "user" ? (
              <div
                key={m.id}
                style={{ display: "flex", justifyContent: "flex-end" }}
              >
                <div
                  style={{
                    maxWidth: "85%",
                    background: "var(--surface-raised)",
                    color: "var(--text-primary)",
                    padding: "6px 10px",
                    borderRadius: "var(--radius-md, 10px)",
                    fontSize: "var(--text-sm)",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                  }}
                >
                  {m.content}
                </div>
              </div>
            ) : (
              <div
                key={m.id}
                style={{ display: "flex", flexDirection: "column", gap: 6 }}
              >
                {getToolCallsFromMessage(m).length > 0 && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {getToolCallsFromMessage(m).map((tc) => (
                      <ToolCallCard key={tc.id} record={tc} />
                    ))}
                  </div>
                )}
                {(m.content || m.isStreaming) && (
                  <div
                    style={{
                      color: "var(--text-primary)",
                      fontSize: "var(--text-sm)",
                      lineHeight: "var(--leading-md, 1.55)",
                    }}
                  >
                    {m.content && <MarkdownRenderer content={m.content} />}
                    {m.isStreaming && <StreamingCursor />}
                  </div>
                )}
              </div>
            ),
          )
        )}
      </div>

      <div
        style={{
          padding: 10,
          borderTop: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
          display: "flex",
          flexDirection: "column",
          gap: 6,
        }}
      >
        <textarea
          value={thread.draft}
          onChange={(e) => setDraft(id, e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Side-chat message..."
          rows={2}
          style={{
            background: "var(--surface-base)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm, 6px)",
            padding: "6px 8px",
            color: "var(--text-primary)",
            fontFamily: "var(--font-ui)",
            fontSize: "var(--text-sm)",
            resize: "none",
            outline: 0,
          }}
        />
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 6 }}>
          {thread.isStreaming && (
            <button
              onClick={stop}
              style={{
                background: "var(--state-danger)",
                color: "var(--text-primary)",
                border: 0,
                borderRadius: "var(--radius-sm, 6px)",
                padding: "4px 10px",
                cursor: "pointer",
                fontSize: "var(--text-xs)",
                fontWeight: 600,
              }}
            >
              Stop
            </button>
          )}
          <button
            onClick={submit}
            disabled={sendDisabled}
            style={{
              background: sendDisabled
                ? "var(--surface-soft)"
                : "var(--accent-primary)",
              color: sendDisabled ? "var(--text-muted)" : "var(--text-primary)",
              border: 0,
              borderRadius: "var(--radius-sm, 6px)",
              padding: "4px 12px",
              cursor: sendDisabled ? "not-allowed" : "pointer",
              fontSize: "var(--text-xs)",
              fontWeight: 600,
            }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
};
