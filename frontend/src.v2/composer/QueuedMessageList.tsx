import { CornerDownLeft, Copy, MoreHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";

export const QueuedMessageList = ({ wide = false, minimal = false }: { wide?: boolean; minimal?: boolean }) => {
  const messages = useAppStore((state) => state.messages);
  const conversationId = useAppStore((state) => state.conversationId);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const menuRootRef = useRef<HTMLDivElement>(null);
  const queued = useMemo(
    () => messages
      .filter((message) => message.role === "user" && message.queueState === "queued")
      .sort((left, right) => (left.queuePosition ?? Number.MAX_SAFE_INTEGER) - (right.queuePosition ?? Number.MAX_SAFE_INTEGER)),
    [messages],
  );

  useEffect(() => {
    if (!menuFor) return;
    const close = (event: MouseEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) setMenuFor(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuFor(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [menuFor]);

  if (!conversationId || queued.length === 0) return null;

  const sendQueueAction = (type: "user_message.queue.cancel" | "user_message.queue.steer", message: typeof queued[number]) => {
    if (!message.queueMessageId) return;
    sendClientCommand({
      type,
      conversation_id: conversationId,
      message_id: message.queueMessageId,
      user_message_id: message.id,
    });
  };

  return (
    <div
      className="queued-message-list"
      aria-label="Queued messages"
      style={{ width: minimal ? "100%" : wide ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)" }}
    >
      {queued.map((message, index) => {
        const attachmentNames = (message.attachmentRefs ?? []).map((attachment) => attachment.name).filter(Boolean);
        const label = message.content.trim() || attachmentNames.join(", ") || "Attachment";
        const displayNumber = (message.queuePosition ?? index + 1) + 1;
        return (
          <div className="queued-message-row" key={message.id}>
            <span className="queued-message-number" aria-label={`Queue item ${displayNumber}`}>{displayNumber}</span>
            <span className="queued-message-text" title={label}>{label}</span>
            <button
              type="button"
              className="queued-message-steer"
              onClick={() => sendQueueAction("user_message.queue.steer", message)}
              title="Guide the running task with this message next"
            >
              <CornerDownLeft size={14} aria-hidden="true" />
              <span>引导</span>
            </button>
            <button
              type="button"
              className="queued-message-icon"
              onClick={() => sendQueueAction("user_message.queue.cancel", message)}
              title="Delete queued message"
              aria-label={`Delete queued message ${displayNumber}`}
            >
              <Trash2 size={14} />
            </button>
            <div className="queued-message-more" ref={menuFor === message.id ? menuRootRef : undefined}>
              <button
                type="button"
                className="queued-message-icon"
                onClick={() => setMenuFor((current) => current === message.id ? null : message.id)}
                title="More queued message actions"
                aria-label={`More actions for queued message ${displayNumber}`}
                aria-expanded={menuFor === message.id}
              >
                <MoreHorizontal size={15} />
              </button>
              {menuFor === message.id ? (
                <div className="queued-message-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      void navigator.clipboard?.writeText(message.content || label);
                      setMenuFor(null);
                    }}
                  >
                    <Copy size={13} aria-hidden="true" />
                    复制内容
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
};
