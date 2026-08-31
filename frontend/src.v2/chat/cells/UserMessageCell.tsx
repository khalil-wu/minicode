import { Clock3, Copy, CornerDownLeft, RotateCcw, X } from "lucide-react";
import { fileIcon } from "../../shell/fileTreeHelpers";
import { useCallback, useEffect, useState } from "react";
import type React from "react";
import type { UserMessageCellState } from "./cellTypes";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
} from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import { openAttachmentPreview, openLocalFilePreview } from "../openAttachmentPreview";
import { buildInterruptCommand } from "../../lib/interrupt-command";
import { pushToast } from "../../overlays/ToastContainer";
import "./cells.css";

const COLLAPSE_CHAR_THRESHOLD = 900;
const COLLAPSE_LINE_THRESHOLD = 14;

export function UserMessageCell({
  cell,
  isTranscriptMode = false,
  conversationId,
}: {
  cell: UserMessageCellState;
  isTranscriptMode?: boolean;
  conversationId?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [cancelPending, setCancelPending] = useState(false);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const ownerConversationId = String(conversationId || "").trim();

  const cancelQueuedMessage = useCallback(async () => {
    if (!ownerConversationId || !cell.queueMessageId || cancelPending) return;
    setCancelPending(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "user_message.queue.cancel",
        conversation_id: ownerConversationId,
        message_id: cell.queueMessageId,
        user_message_id: cell.id,
      }, "user_message.queue.cancel");
      if (!commandResultSucceeded(result)) {
        pushToast(result.message || "取消排队消息失败。", "error", 3500);
      } else if (String(result.level || "").toLowerCase() === "warning" && result.message) {
        pushToast(result.message, "warning", 3000);
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "取消排队消息失败。", "error", 3500);
    } finally {
      setCancelPending(false);
    }
  }, [cancelPending, cell.id, cell.queueMessageId, ownerConversationId]);

  const copy = useCallback(() => {
    navigator.clipboard.writeText(cell.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  }, [cell.content]);

  const recall = useCallback(async () => {
    const state = useAppStore.getState();
    const index = state.messages.findIndex((item) => item.id === cell.id);
    const removeCount = index >= 0 ? state.messages.length - index : 1;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "撤回消息",
      message: `将这条输入退回编辑框？当前视图中的 ${removeCount} 条消息将被移除。`,
      confirmLabel: "撤回",
      danger: removeCount > 1 || state.isStreaming,
    });
    if (!ok) return;
    const latest = useAppStore.getState();
    if (latest.isStreaming) {
      const sent = sendClientCommand(buildInterruptCommand(latest));
      if (!sent) return;
      pushToast("已请求停止，请在本轮结束后再撤回。", "info", 3000);
      return;
    }
    const recalled = await recallMessage(cell.id);
    if (recalled) queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
  }, [cell.id, recallMessage]);

  const openFilePreview = useCallback((
    attachment: NonNullable<UserMessageCellState["attachments"]>[number],
    index: number,
  ) => {
    if (attachment.artifactId) {
      openAttachmentPreview({
        artifactId: attachment.artifactId,
        name: attachment.name,
        mediaType: attachment.type,
        kind: attachment.type.startsWith("image/") ? "image" : "document",
        conversationId: ownerConversationId || undefined,
      });
      return;
    }
    if (!attachment.dataUrl) return;
    openLocalFilePreview({
      id: attachment.id || attachment.docId || `${cell.id}:${index}`,
      name: attachment.name,
      mediaType: attachment.type,
      kind: attachment.type.startsWith("image/") ? "image" : "document",
      url: attachment.dataUrl,
      conversationId: ownerConversationId || undefined,
    });
  }, [cell.id, ownerConversationId]);

  const visibleContent = cell.content.trim();
  const collapsible = Boolean(visibleContent) && (
    cell.content.length > COLLAPSE_CHAR_THRESHOLD
    || cell.content.split(/\r\n|\r|\n/).length > COLLAPSE_LINE_THRESHOLD
  );

  useEffect(() => {
    setExpanded(false);
  }, [cell.id, cell.content]);

  return (
    <div className="user-cell-wrap">
      {cell.messageSource?.kind === "scheduled_task" ? (
        <div
          className="user-cell-source"
          data-message-source="scheduled_task"
          title={sourceTitle(cell.messageSource)}
        >
          <Clock3 size={13} aria-hidden="true" />
          <span>定时任务</span>
          {cell.messageSource.runId ? (
            <span className="user-cell-source-id">运行 {compactSourceId(cell.messageSource.runId)}</span>
          ) : null}
        </div>
      ) : null}
      <div className="edit-bubble-wrap">
        <div className="user-cell-bubble md-prose">
          {visibleContent ? (
            <>
              <div
                className={`user-cell-content${collapsible && !expanded ? " user-cell-content-collapsed" : ""}`}
                data-collapsed={collapsible && !expanded ? "true" : "false"}
              >
                {cell.content}
              </div>
              {collapsible ? (
                <button
                  type="button"
                  className="user-cell-expand-button"
                  aria-expanded={expanded}
                  onClick={() => setExpanded((value) => !value)}
                >
                  {expanded ? "收起消息" : "展开消息"}
                </button>
              ) : null}
            </>
          ) : null}
          {cell.queueState ? (
            <div className="user-cell-queue-status" data-status={cell.queueState}>
              <Clock3 size={14} />
              <span>
                {cell.queueState === "queued"
                  ? `排队中${cell.queuePosition ? ` · 第 ${cell.queuePosition} 位` : ""}`
                  : "排队已取消"}
              </span>
            </div>
          ) : null}
          {cell.steeredIntoMessageId ? (
            <div className="user-cell-queue-status" data-status="steered" title="这条消息已用于引导当前任务">
              <CornerDownLeft size={14} />
              <span>已引导当前任务</span>
            </div>
          ) : null}
          {cell.attachments && cell.attachments.length > 0 && (
            <div className={`user-cell-attachments${visibleContent ? "" : " user-cell-attachments-only"}`}>
              {cell.attachments.map((attachment, index) => {
                const previewable = Boolean(attachment.artifactId || attachment.dataUrl);
                return previewable ? (
                  <button
                    key={attachmentKey(attachment, index)}
                    type="button"
                    className="user-cell-attachment-chip user-cell-attachment-chip-button"
                    onClick={() => openFilePreview(attachment, index)}
                    title={`预览 ${attachment.name}`}
                  >
                    {fileIcon(attachment.name, { size: 13, className: "user-cell-attachment-file-icon" })}
                    <span>{attachment.name}</span>
                  </button>
                ) : (
                  <span key={attachmentKey(attachment, index)} className="user-cell-attachment-chip">
                    {fileIcon(attachment.name, { size: 13, className: "user-cell-attachment-file-icon" })}
                    <span>{attachment.name}</span>
                  </span>
                );
              })}
            </div>
          )}
        </div>
      </div>
      {!isTranscriptMode && <div className="user-cell-actions">
        <button type="button" onClick={copy} title={copied ? "已复制" : "复制消息"} aria-label={copied ? "已复制" : "复制消息"} className="cell-action-btn">
          <Copy size={14} />
        </button>
        {cell.queueState === "queued" ? (
          <button type="button" onClick={() => void cancelQueuedMessage()} disabled={cancelPending} aria-busy={cancelPending} title="取消排队消息" aria-label="取消排队消息" className="cell-action-btn">
            <X size={14} />
          </button>
        ) : (
          <button type="button" onClick={recall} title="撤回到输入框编辑" aria-label="撤回到输入框" className="cell-action-btn user-cell-edit-action">
            <RotateCcw size={14} />
            <span>编辑</span>
          </button>
        )}
      </div>}
    </div>
  );
}

function attachmentKey(
  attachment: NonNullable<UserMessageCellState["attachments"]>[number],
  index: number,
): string {
  return [
    attachment.artifactId,
    attachment.docId,
    attachment.id,
    attachment.name,
    index,
  ].filter(Boolean).join(":");
}

function compactSourceId(value: string): string {
  const id = value.trim();
  return id.length > 16 ? `${id.slice(0, 8)}...${id.slice(-5)}` : id;
}

function sourceTitle(source: NonNullable<UserMessageCellState["messageSource"]>): string {
  const details = [
    source.taskId ? `任务 ${source.taskId}` : "",
    source.runId ? `运行 ${source.runId}` : "",
  ].filter(Boolean);
  return details.length ? `定时任务 · ${details.join(" · ")}` : "定时任务";
}
