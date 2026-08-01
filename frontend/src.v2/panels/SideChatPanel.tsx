import { useEffect, useMemo, useRef } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendChatMessage } from "../chat/sendChatMessage";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { ToolCallCard } from "../chat/tool-calls/ToolCallCard";
import { StreamingCursor } from "../chat/messages/StreamingCursor";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { toBackendPermissionMode } from "../protocol/permissions";
import { sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { uniqueMessageId } from "../stores/shared-helpers";

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
      void sendConversationDeleteCommand({
        type: "conversation.delete",
        conversation_id: id,
      });
      removeSideChat(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [thread?.messages.length, thread?.messages.at(-1)?.content]);

  useEffect(() => {
    inputRef.current?.focus();
  }, [thread?.selectedContext?.text]);

  const submit = () => {
    const content = (thread?.draft ?? "").trim();
    if (!content || !thread || thread.isStreaming) return;
    const selectedPrefix = thread.messages.length === 0 && thread.selectedContext?.text
      ? `Selected context${thread.selectedContext.source ? ` (${thread.selectedContext.source})` : ""}:\n\n${thread.selectedContext.text}\n\n`
      : "";
    const inheritedPrefix = thread.messages.length === 0 && thread.inheritedContext
      ? `${thread.inheritedContext}\n\nSide-chat question:\n`
      : "";
    const assistantMessageId = uniqueMessageId("sa");
    const userMessageId = uniqueMessageId("su");
    const sent = sendChatMessage({
      displayContent: content,
      backendContent: `${selectedPrefix}${inheritedPrefix}${content}`,
      conversationId: id,
      allowWhileStreaming: false,
      skipLocalAppend: true,
      assistantMessageId,
      userMessageId,
    });
    if (sent) startMessage(id, content, { assistantMessageId, userMessageId });
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
    <div className="flex flex-col flex-1 min-h-0">
      <div
        className="px-2.5 py-1.5 flex items-center gap-2"
        style={{
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
        }}
      >
        <span className="flex-1">
          {thread.selectedContext ? "Ask about selection" : "Side chat"}
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
        className="flex-1 overflow-y-auto px-3.5 py-3 flex flex-col gap-3"
      >
        {thread.messages.length === 0 ? (
          <div
            className="text-center p-5"
            style={{
              color: "var(--text-muted)",
              fontSize: "var(--text-sm)",
            }}
          >
            This side chat stays separate from the main conversation.
            {thread.selectedContext && (
              <div
                className="mt-2.5 p-2.5 text-left whitespace-pre-wrap max-h-40 overflow-auto"
                style={{
                  border: "1px solid var(--accent-primary)",
                  borderRadius: "var(--radius-sm, 6px)",
                  background: "var(--surface-raised)",
                  color: "var(--text-secondary)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                }}
              >
                {thread.selectedContext.source && <div className="mb-1">{thread.selectedContext.source}</div>}
                {thread.selectedContext.text}
              </div>
            )}
            {thread.inheritedContext && (
              <div
                className="mt-2.5 p-2.5 text-left whitespace-pre-wrap max-h-30 overflow-auto"
                style={{
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-sm, 6px)",
                  background: "var(--surface-page)",
                  color: "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                }}
              >
                {thread.inheritedContext}
              </div>
            )}
          </div>
        ) : (
          thread.messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div
                  className="max-w-[85%] px-2.5 py-1.5 whitespace-pre-wrap break-words"
                  style={{
                    background: "var(--surface-raised)",
                    color: "var(--text-primary)",
                    borderRadius: "var(--radius-md, 10px)",
                    fontSize: "var(--text-sm)",
                  }}
                >
                  {m.content}
                </div>
              </div>
            ) : (
              <div key={m.id} className="flex flex-col gap-1.5">
                {getToolCallsFromMessage(m).length > 0 && (
                  <div className="flex flex-col gap-1">
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
        className="p-2.5 flex flex-col gap-1.5"
        style={{
          borderTop: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
        }}
      >
        <textarea
          ref={inputRef}
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
          className="px-2 py-1.5 resize-none outline-none"
          style={{
            background: "var(--surface-base)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm, 6px)",
            color: "var(--text-primary)",
            fontFamily: "var(--font-ui)",
            fontSize: "var(--text-sm)",
          }}
        />
        <div className="flex justify-end gap-1.5">
          {thread.isStreaming && (
            <button
              onClick={stop}
              className="border-0 px-2.5 py-1 cursor-pointer font-semibold"
              style={{
                background: "var(--state-danger)",
                color: "var(--text-primary)",
                borderRadius: "var(--radius-sm, 6px)",
                fontSize: "var(--text-xs)",
              }}
            >
              Stop
            </button>
          )}
          <button
            onClick={submit}
            disabled={sendDisabled}
            className="border-0 px-3 py-1 font-semibold"
            style={{
              background: sendDisabled
                ? "var(--surface-soft)"
                : "var(--accent-primary)",
              color: sendDisabled ? "var(--text-muted)" : "var(--text-primary)",
              borderRadius: "var(--radius-sm, 6px)",
              cursor: sendDisabled ? "not-allowed" : "pointer",
              fontSize: "var(--text-xs)",
            }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
};
