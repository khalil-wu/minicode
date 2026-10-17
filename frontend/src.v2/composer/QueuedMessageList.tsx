import { useI18n } from "../i18n";
import { CornerDownLeft, Copy, MoreHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
const NO_MESSAGES: ChatMessage[] = [];

export const QueuedMessageList = ({ wide = false, minimal = false, ownerId }: { wide?: boolean; minimal?: boolean; ownerId?: string }) => {
  const { t: tr } = useI18n();
  const messages = useAppStore((state) => ownerId ? state.sideChats[ownerId]?.messages ?? NO_MESSAGES : state.messages);
  const conversationId = useAppStore((state) => ownerId ?? state.conversationId);
  const isStreaming = useAppStore((state) => ownerId ? state.sideChats[ownerId]?.isStreaming : state.isStreaming);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const menuRootRef = useRef<HTMLDivElement>(null);
  const menuTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const queued = useMemo(
    () => messages
      .filter((message) => message.role === "user" && message.queueState === "queued")
      .sort((left, right) => (left.queuePosition ?? Number.MAX_SAFE_INTEGER) - (right.queuePosition ?? Number.MAX_SAFE_INTEGER)),
    [messages],
  );

  useEffect(() => {
    if (!menuFor) return;
    const menuRoot = menuRootRef.current;
    queueMicrotask(() => menuRoot?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus());
    const close = (event: MouseEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) setMenuFor(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuFor(null);
        menuTriggerRefs.current.get(menuFor)?.focus();
      }
    };
    const navigateMenu = (event: KeyboardEvent) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") return;
      const items = Array.from(menuRoot?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
      if (!items.length) return;
      event.preventDefault();
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "Home" || event.key === "End") {
        items[event.key === "Home" ? 0 : items.length - 1].focus();
        return;
      }
      const next = event.key === "ArrowDown" ? (current + 1 + items.length) % items.length : (current - 1 + items.length) % items.length;
      items[next].focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", closeOnEscape);
    menuRoot?.addEventListener("keydown", navigateMenu);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", closeOnEscape);
      menuRoot?.removeEventListener("keydown", navigateMenu);
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
      aria-label={tr("排队中的消息")}
      style={{ width: minimal ? "100%" : wide ? "var(--chat-code-composer-width)" : "var(--chat-composer-axis-width)" }}
    >
      {queued.map((message, index) => {
        const attachmentNames = (message.attachmentRefs ?? []).map((attachment) => attachment.name).filter(Boolean);
        const commentCount = message.contextRefs?.filter(ref => ref.kind === "diff_comment").length ?? 0;
        const commentLabel = commentCount === 1 ? tr("1 条评论") : tr("{count} 条评论", { count: commentCount });
        const label = message.content.trim() || attachmentNames.join(", ") || (commentCount ? commentLabel : "Attachment");
        const displayNumber = (message.queuePosition ?? index + 1) + 1;
        return (
          <div className="queued-message-row" key={message.id}>
            <span className="queued-message-number" aria-label={tr("队列第 {count} 项", { count: displayNumber })}>{displayNumber}</span>
            <span className="queued-message-text" title={label}>{label}</span>
            {message.queuePaused ? <span>{tr("已暂停")}</span> : null}
            <button
              type="button"
              className="queued-message-steer"
              onClick={() => sendQueueAction("user_message.queue.steer", message)}
              title={message.queuePaused ? tr("发送这条消息并继续队列") : isStreaming ? tr("下一步用这条消息引导当前任务") : tr("发送这条消息")}
            >
              <CornerDownLeft size={14} aria-hidden="true" />
              <span>{message.queuePaused || !isStreaming ? tr("发送") : tr("引导")}</span>
            </button>
            <button
              type="button"
              className="queued-message-icon"
              onClick={() => sendQueueAction("user_message.queue.cancel", message)}
              title={tr("删除排队消息")}
              aria-label={tr("删除排队消息 {count}", { count: displayNumber })}
            >
              <Trash2 size={14} />
            </button>
            <div className="queued-message-more" ref={menuFor === message.id ? menuRootRef : undefined}>
              <button
                type="button"
                className="queued-message-icon"
                ref={(node) => { if (node) menuTriggerRefs.current.set(message.id, node); else menuTriggerRefs.current.delete(message.id); }}
                onClick={() => setMenuFor((current) => current === message.id ? null : message.id)}
                title={tr("更多排队消息操作")}
                aria-label={tr("排队消息 {count} 的更多操作", { count: displayNumber })}
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
                    <Copy size={14} aria-hidden="true" />
                    {tr("复制内容")}</button>
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
};
