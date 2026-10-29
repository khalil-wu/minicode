import { useI18n } from "../../i18n";
import { Clock3, Copy, RotateCcw, X } from "lucide-react";
import { fileIcon } from "../../lib/file-icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import type React from "react";
import type { UserMessageCellState } from "./cellTypes";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
} from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import { getWebSocket } from "../../hooks/useWebSocket";
import { artifactResourceUrl } from "../../lib/artifact-resource";
import { openAttachmentPreview, openLocalFilePreview } from "../openAttachmentPreview";
import { buildInterruptCommand } from "../../lib/interrupt-command";
import { pushToast } from "../../overlays/ToastContainer";
import { MessageQuote } from "../components/MessageQuote";
import { contextReferenceLabel, openContextReference } from "../contextReferenceActions";
import "./cells.css";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { DiffCommentAttachments } from "../../composer/DiffCommentAttachments";
import { restoreRetainedComposerInput } from "../../composer/retainedComposerInputs";

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
  const { t: tr } = useI18n();
  const isConnected = useAppStore((s) => s.isConnected);
  const [copied, setCopied] = useState(false);
  const [expansionPreference, setExpanded] = useState(false);
  const expanded = useTranscriptSearch() || expansionPreference;
  const [cancelPending, setCancelPending] = useState(false);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const ownerConversationId = String(conversationId || "").trim();
  const canRecall = useAppStore((s) => !ownerConversationId || s.conversationId === ownerConversationId);
  const retainedFailure = useAppStore(s => isTranscriptMode ? undefined : s.retainedComposerInputs[cell.id]?.message.failureMessage);
  const livePendingInput = useAppStore(s => Boolean(s.retainedComposerInputs[cell.id]?.unconfirmed && !s.retainedComposerInputs[cell.id]?.recovered));
  const recoveredUnconfirmedInput = useAppStore(s => Boolean(s.retainedComposerInputs[cell.id]?.unconfirmed && s.retainedComposerInputs[cell.id]?.recovered));

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
        pushToast(result.message || tr("取消排队消息失败。"), "error", 3500);
      } else if (String(result.level || "").toLowerCase() === "warning" && result.message) {
        pushToast(result.message, "warning", 3000);
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : tr("取消排队消息失败。"), "error", 3500);
    } finally {
      setCancelPending(false);
    }
  }, [cancelPending, cell.id, cell.queueMessageId, ownerConversationId, tr]);

  const copy = useCallback(() => {
    navigator.clipboard.writeText(cell.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  }, [cell.content]);

  const recall = useCallback(async () => {
    const state = useAppStore.getState();
    if (state.retainedComposerInputs[cell.id]) { restoreRetainedComposerInput(cell.id); return; }
    const requestConversationId = ownerConversationId || state.conversationId;
    if (!requestConversationId || state.conversationId !== requestConversationId) return;
    const index = state.messages.findIndex((item) => item.id === cell.id);
    const removeCount = index >= 0 ? state.messages.length - index : 1;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: tr("撤回消息"),
      message: tr("将这条输入退回编辑框？当前视图中的 {v0} 条消息将被移除。", { v0: removeCount }),
      confirmLabel: tr("撤回"),
      danger: removeCount > 1 || state.isStreaming,
    });
    if (!ok) return;
    const latest = useAppStore.getState();
    if (latest.conversationId !== requestConversationId) return;
    if (latest.isStreaming) {
      const sent = sendClientCommand(buildInterruptCommand(latest));
      if (!sent) return;
      pushToast(tr("已请求停止，请在本轮结束后再撤回。"), "info", 3000);
      return;
    }
    const recalled = await recallMessage(cell.id);
    if (recalled) queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
  }, [cell.id, ownerConversationId, recallMessage, tr, tr]);

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
  const ordinaryRefs = cell.contextRefs?.filter(ref => ref.kind !== "diff_comment") ?? [];
  const diffComments = cell.contextRefs?.flatMap(ref => ref.kind === "diff_comment" ? [ref.comment] : []) ?? [];
  const collapsible = Boolean(visibleContent) && (
    cell.content.length > COLLAPSE_CHAR_THRESHOLD
    || cell.content.split(/\r\n|\r|\n/).length > COLLAPSE_LINE_THRESHOLD
  );

  useEffect(() => {
    setExpanded(false);
  }, [cell.id, cell.content]);

  return (
    <div className="user-cell-wrap" data-message-id={cell.id} title={recoveredUnconfirmedInput ? tr("未确认发送") : retainedFailure}>
      {cell.messageSource?.kind === "scheduled_task" ? (
        <div
          className="user-cell-source"
          data-message-source="scheduled_task"
          title={tr("定时任务")}
        >
          <Clock3 size={13} aria-hidden="true" />
          <span>{tr("定时任务")}</span>
        </div>
      ) : null}
      {Boolean(ordinaryRefs.length) && (
        <div className="user-cell-attachments" aria-label={tr("消息上下文")}>
          {ordinaryRefs.map((ref, index) => (
            <button type="button" key={ref.kind + ":" + ref.path + ":" + index} className="user-cell-attachment-chip"
              onClick={() => openContextReference(ref, ownerConversationId)}
              disabled={!["file", "url", "browser_annotation"].includes(ref.kind)}
              aria-label={tr("打开引用 {v0}", { v0: contextReferenceLabel(ref) })}
              title={ref.kind === "browser_annotation" ? ref.url + "\n" + ref.note : ref.path}>
              {fileIcon(ref.kind === "skill" ? "SKILL.md" : ref.name, { size: 15, className: "user-cell-attachment-file-icon" })}
              <span>{(ref.kind === "skill" ? "$" : "@") + contextReferenceLabel(ref)}</span>
            </button>
          ))}
        </div>
      )}
      <DiffCommentAttachments comments={diffComments} />
      {cell.attachments && cell.attachments.length > 0 && (
        <div className={`user-cell-attachments${visibleContent ? "" : " user-cell-attachments-only"}`}>
          {cell.attachments.map((attachment, index) => {
            const previewable = Boolean(attachment.artifactId || attachment.dataUrl);
                const isImage = attachment.type.startsWith("image/");
            return previewable ? (
              <button
                key={attachmentKey(attachment, index)}
                type="button"
                    className={isImage ? "user-cell-attachment-image" : "user-cell-attachment-chip user-cell-attachment-chip-button"}
                aria-label={attachment.name}
                onClick={() => openFilePreview(attachment, index)}
                title={tr("预览 {v0}", { v0: attachment.name })}
              >
                    {isImage ? <UserAttachmentImage artifactId={attachment.artifactId} dataUrl={attachment.dataUrl}
                      name={attachment.name} conversationId={ownerConversationId} sessionId={getWebSocket()?.sessionId} isConnected={isConnected} />
                  : <>{fileIcon(attachment.name, { size: 18, className: "user-cell-attachment-file-icon" })}<span>{attachment.name}</span></>}
              </button>
            ) : (
              <span key={attachmentKey(attachment, index)} className="user-cell-attachment-chip">
                {fileIcon(attachment.name, { size: 15, className: "user-cell-attachment-file-icon" })}
                <span>{attachment.name}</span>
              </span>
            );
          })}
        </div>
      )}
      {cell.quotedMessage && <div className="user-cell-quote"><MessageQuote message={cell.quotedMessage} /></div>}
      {(visibleContent || cell.queueState) && <div className="edit-bubble-wrap">
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
                  {expanded ? tr("收起消息") : tr("展开消息")}
                </button>
              ) : null}
            </>
          ) : null}
          {cell.queueState ? (
            <div className="user-cell-queue-status" data-status={cell.queueState}>
              <Clock3 size={14} />
              <span>
                {cell.queueState === "queued"
                  ? tr("排队中{v0}", { v0: cell.queuePosition ? ` · 第 ${cell.queuePosition} 位` : "" })
                  : tr("排队已取消")}
              </span>
            </div>
          ) : null}
        </div>
      </div>}
      {!isTranscriptMode && <div className="user-cell-actions">
        <button type="button" onClick={copy} title={copied ? tr("已复制") : tr("复制消息")} aria-label={copied ? tr("已复制") : tr("复制消息")} className="cell-action-btn">
          <Copy size={14} />
        </button>
        {cell.queueState === "queued" ? (
          <button type="button" onClick={() => void cancelQueuedMessage()} disabled={cancelPending} aria-busy={cancelPending} title={tr("取消排队消息")} aria-label={tr("取消排队消息")} className="cell-action-btn">
            <X size={14} />
          </button>
        ) : canRecall ? (
          <button type="button" onClick={recall} disabled={livePendingInput} title={tr("撤回到输入框编辑")} aria-label={tr("撤回到输入框")} className="cell-action-btn user-cell-edit-action">
            <RotateCcw size={14} />
            <span>{tr("编辑")}</span>
          </button>
        ) : null}
      </div>}
    </div>
  );
}

function UserAttachmentImage({ artifactId, dataUrl, name, conversationId, sessionId, isConnected }: {
  artifactId?: string; dataUrl?: string; name: string; conversationId: string; sessionId?: string; isConnected: boolean;
}) {
  const imageUrl = useMemo(() => artifactResourceUrl({ artifactId, conversationId, sessionId,
    source: "attachment", originalUrl: dataUrl, isConnected,
  }), [artifactId, dataUrl, conversationId, sessionId, isConnected]);
  return imageUrl ? <img src={imageUrl} alt={name} loading="lazy" />
    : <>{fileIcon(name, { size: 18, className: "user-cell-attachment-file-icon" })}<span>{name}</span></>;
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
