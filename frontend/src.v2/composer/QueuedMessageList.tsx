import { useI18n } from "../i18n";
import { CornerDownLeft, Copy, MoreHorizontal, Pencil, Trash2 } from "../lib/icons";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DndContext, KeyboardSensor, PointerSensor, useSensor, useSensors } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { useShallow } from "zustand/react/shallow";
import type { ChatMessage } from "../stores/types";
import { captureSteerTurnId } from "../lib/steer-command";
import { pushToast } from "../overlays/ToastContainer";
import { beginQueuedMessageEdit, deleteQueuedMessage } from "./queued-message-edit";
const NO_MESSAGES: ChatMessage[] = [];

export const QueuedMessageList = ({ wide = false, minimal = false, ownerId }: { wide?: boolean; minimal?: boolean; ownerId?: string }) => {
  const { t: tr } = useI18n();
  const queuedMessages = useAppStore(useShallow((state) => (ownerId ? state.sideChats[ownerId]?.messages ?? NO_MESSAGES : state.messages)
    .filter((message) => message.role === "user" && message.queueState === "queued")));
  const conversationId = useAppStore((state) => ownerId ?? state.conversationId);
  const isStreaming = useAppStore((state) => ownerId ? state.sideChats[ownerId]?.isStreaming : state.isStreaming);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const menuRootRef = useRef<HTMLDivElement>(null);
  const menuTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const dragging = useRef<{ ownerId: string; ids: string[] } | null>(null);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  useEffect(() => useAppStore.subscribe((state, previous) => {
    if (!ownerId && (state.conversationId !== previous.conversationId || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId)) {
      dragging.current = null;
      setMenuFor(null);
    }
  }), [ownerId]);
  const queued = useMemo(
    () => [...queuedMessages]
      .sort((left, right) => (left.queuePosition ?? Number.MAX_SAFE_INTEGER) - (right.queuePosition ?? Number.MAX_SAFE_INTEGER)),
    [queuedMessages],
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

  const sendQueueAction = (action: "cancel" | "send", message: typeof queued[number]) => {
    if (!message.queueMessageId) return;
    const expectedTurnId = action === "send" ? captureSteerTurnId(useAppStore.getState(), conversationId) : undefined;
    if (expectedTurnId === null) {
      pushToast(tr("当前任务尚未提供回合标识，无法引导；请稍后重试。"), "warning");
      return;
    }
    const target = {
      conversation_id: conversationId,
      message_id: message.queueMessageId,
      user_message_id: message.id,
    };
    if (action === "cancel") void deleteQueuedMessage(conversationId, message);
    else if (expectedTurnId === undefined) sendClientCommand({ type: "user_message.queue.start", ...target });
    else sendClientCommand({ type: "user_message.queue.steer", ...target, expected_turn_id: expectedTurnId });
  };

  return (
    <div
      className="queued-message-list"
      aria-label={tr("排队中的消息")}
      style={{ width: minimal ? "100%" : wide ? "var(--chat-code-composer-width)" : "var(--chat-composer-axis-width)" }}
    >
      <DndContext sensors={sensors} onDragStart={() => { dragging.current = { ownerId: conversationId, ids: queued.map(message => message.queueMessageId!) }; }}
        onDragCancel={() => { dragging.current = null; }} onDragEnd={({ active, over }) => {
          const captured = dragging.current; dragging.current = null;
          if (!captured || !over || active.id === over.id) return;
          const ids = [...captured.ids];
          const from = ids.indexOf(String(active.id)); const to = ids.indexOf(String(over.id));
          if (from < 0 || to < 0) return;
          ids.splice(to, 0, ids.splice(from, 1)[0]);
          void sendClientCommandAwaitResult({ type: "user_message.queue.reorder", conversation_id: captured.ownerId,
            message_ids: ids }, "user_message.queue.reorder", { silent: true }).then(result => {
              if (!commandResultSucceeded(result)) pushToast(result.message, "error");
            }, error => pushToast(error instanceof Error ? error.message : String(error), "error"));
        }}>
      <SortableContext items={queued.map(message => message.queueMessageId!)} strategy={verticalListSortingStrategy}>
      {queued.map((message, index) => {
        const attachmentNames = (message.attachmentRefs ?? []).map((attachment) => attachment.name).filter(Boolean);
        const commentCount = message.contextRefs?.filter(ref => ref.kind === "diff_comment").length ?? 0;
        const commentLabel = commentCount === 1 ? tr("1 条评论") : tr("{count} 条评论", { count: commentCount });
        const label = message.content.trim() || attachmentNames.join(", ") || (commentCount ? commentLabel : "Attachment");
        const displayNumber = (message.queuePosition ?? index + 1) + 1;
        return (
          <SortableQueuedRow key={message.id} id={message.queueMessageId!} number={displayNumber}
            numberLabel={tr("队列第 {count} 项", { count: displayNumber })} dragLabel={tr("拖动以重新排序排队消息")}>
            <span className="queued-message-text" title={label}>{label}</span>
            {message.queuePaused ? <span>{tr("已暂停")}</span> : null}
            <button
              type="button"
              className="queued-message-steer"
              onClick={() => sendQueueAction("send", message)}
              title={isStreaming ? tr("下一步用这条消息引导当前任务") : tr("发送这条消息并继续队列")}
            >
              <CornerDownLeft size={14} aria-hidden="true" />
              <span>{isStreaming ? tr("引导") : tr("发送")}</span>
            </button>
            <button
              type="button"
              className="queued-message-icon"
              onClick={() => sendQueueAction("cancel", message)}
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
                  {!ownerId && <button type="button" role="menuitem" onClick={() => {
                    setMenuFor(null); void beginQueuedMessageEdit(conversationId, message);
                  }}><Pencil size={14} aria-hidden="true" />{tr("编辑")}</button>}
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
          </SortableQueuedRow>
        );
      })}
      </SortableContext>
      </DndContext>
    </div>
  );
};

function SortableQueuedRow({ id, number, numberLabel, dragLabel, children }: {
  id: string; number: number; numberLabel: string; dragLabel: string; children: ReactNode;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } = useSortable({ id });
  return <div className="queued-message-row" ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }}>
    <span className="queued-message-number" ref={setActivatorNodeRef} {...attributes} {...listeners}
      aria-label={numberLabel} title={dragLabel} style={{ cursor: "grab", touchAction: "none" }}>{number}</span>
    {children}
  </div>;
}
