import { Clock3, Copy, CornerDownLeft, Image as ImageIcon, LoaderCircle, RotateCcw, Trash2, X } from "lucide-react";
import { fileIcon } from "../../shell/fileTreeHelpers";
import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";
import type { UserMessageCellState } from "./cellTypes";
import { ImageLightbox } from "../../components/ImageLightbox";
import { getWebSocket } from "../../hooks/useWebSocket";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import "./cells.css";

const COLLAPSE_CHAR_THRESHOLD = 900;
const COLLAPSE_LINE_THRESHOLD = 14;

export function UserMessageCell({ cell }: { cell: UserMessageCellState }) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [preview, setPreview] = useState<{ src: string; name: string } | null>(null);
  const [pendingPreviewArtifactId, setPendingPreviewArtifactId] = useState<string | null>(null);
  const [failedPreviewArtifactId, setFailedPreviewArtifactId] = useState<string | null>(null);
  const pendingPreviewArtifactIdRef = useRef<string | null>(null);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const deleteMessage = useAppStore((s) => s.deleteMessage);

  const cancelQueuedMessage = useCallback(() => {
    const conversationId = useAppStore.getState().conversationId;
    if (!conversationId || !cell.queueMessageId) return;
    sendClientCommand({
      type: "user_message.queue.cancel",
      conversation_id: conversationId,
      message_id: cell.queueMessageId,
      user_message_id: cell.id,
    });
  }, [cell.id, cell.queueMessageId]);

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
      title: "Recall message",
      message: `Move this prompt back to the composer? This will remove ${removeCount} message${removeCount === 1 ? "" : "s"} from the current view.`,
      confirmLabel: "Recall",
      danger: removeCount > 1 || state.isStreaming,
    });
    if (!ok) return;
    const latest = useAppStore.getState();
    if (latest.isStreaming) {
      getWebSocket()?.send({
        type: "interrupt",
        conversation_id: latest.conversationId || undefined,
      });
      latest.interrupt();
    }
    recallMessage(cell.id);
    queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
  }, [cell.id, recallMessage]);

  const deleteWithConfirm = useCallback(async () => {
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "删除消息",
      message: "Remove this message from the current conversation view?",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    deleteMessage(cell.id);
  }, [cell.id, deleteMessage]);

  useEffect(() => {
    const onArtifactImagePreview = (event: Event) => {
      const detail = (event as CustomEvent<{ artifactId?: string; url?: string }>).detail;
      const pendingArtifactId = pendingPreviewArtifactIdRef.current;
      if (!pendingArtifactId || detail?.artifactId !== pendingArtifactId || !detail.url) return;
      const attachment = cell.attachments?.find((item) => item.artifactId === pendingArtifactId);
      setPreview({ src: detail.url, name: attachment?.name || "image" });
      pendingPreviewArtifactIdRef.current = null;
      setPendingPreviewArtifactId(null);
      setFailedPreviewArtifactId(null);
    };
    window.addEventListener("artifact:image-preview", onArtifactImagePreview);
    return () => window.removeEventListener("artifact:image-preview", onArtifactImagePreview);
  }, [cell.attachments]);

  useEffect(() => {
    if (!pendingPreviewArtifactId) return;
    const artifactId = pendingPreviewArtifactId;
    const timeout = window.setTimeout(() => {
      if (pendingPreviewArtifactIdRef.current !== artifactId) return;
      pendingPreviewArtifactIdRef.current = null;
      setPendingPreviewArtifactId(null);
      setFailedPreviewArtifactId(artifactId);
    }, 10_000);
    return () => window.clearTimeout(timeout);
  }, [pendingPreviewArtifactId]);

  const openImagePreview = useCallback((attachment: NonNullable<UserMessageCellState["attachments"]>[number]) => {
    if (!attachment.type.startsWith("image/")) return;
    if (attachment.dataUrl) {
      setFailedPreviewArtifactId(null);
      setPreview({ src: attachment.dataUrl, name: attachment.name });
      return;
    }
    if (attachment.artifactId) {
      setFailedPreviewArtifactId(null);
      pendingPreviewArtifactIdRef.current = attachment.artifactId;
      setPendingPreviewArtifactId(attachment.artifactId);
      getWebSocket()?.send({ type: "read_artifact", artifact_id: attachment.artifactId, purpose: "image_preview" });
    }
  }, []);

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
                  ? `Queued${cell.queuePosition ? ` · ${cell.queuePosition}` : ""}`
                  : "Queue cancelled"}
              </span>
            </div>
          ) : null}
          {cell.steeredIntoMessageId ? (
            <div className="user-cell-queue-status" data-status="steered" title="这条消息已注入当前运行中的 Agent turn">
              <CornerDownLeft size={14} />
              <span>已引导当前任务</span>
            </div>
          ) : null}
          {cell.attachments && cell.attachments.length > 0 && (
            <div className={`user-cell-attachments${visibleContent ? "" : " user-cell-attachments-only"}`}>
              {cell.attachments.map((attachment, index) => {
                const previewable = Boolean(
                  attachment.type.startsWith("image/")
                  && (attachment.dataUrl || attachment.artifactId)
                );
                return previewable ? (
                  <button
                    key={attachmentKey(attachment, index)}
                    type="button"
                    className="user-cell-attachment-chip user-cell-attachment-chip-button"
                    onClick={() => openImagePreview(attachment)}
                    title={failedPreviewArtifactId === attachment.artifactId
                      ? `Failed to load ${attachment.name}; click to retry`
                      : `Preview ${attachment.name}`}
                  >
                    {pendingPreviewArtifactId === attachment.artifactId ? (
                      <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                    ) : failedPreviewArtifactId === attachment.artifactId ? (
                      <X size={14} aria-hidden="true" />
                    ) : (
                      <ImageIcon size={14} aria-hidden="true" />
                    )}
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
      <div className="user-cell-actions">
        <button type="button" onClick={copy} title={copied ? "已复制" : "复制消息"} aria-label={copied ? "已复制" : "复制消息"} className="cell-action-btn">
          <Copy size={14} />
        </button>
        {cell.queueState === "queued" ? (
          <button type="button" onClick={cancelQueuedMessage} title="取消排队消息" aria-label="取消排队消息" className="cell-action-btn">
            <X size={14} />
          </button>
        ) : (
          <button type="button" onClick={recall} title="撤回到输入框编辑" aria-label="撤回到输入框" className="cell-action-btn user-cell-edit-action">
            <RotateCcw size={14} />
            <span>编辑</span>
          </button>
        )}
        <button type="button" onClick={deleteWithConfirm} title="删除消息" aria-label="删除消息" className="cell-action-btn">
          <Trash2 size={14} />
        </button>
      </div>
      {preview ? (
        <ImageLightbox
          src={preview.src}
          alt={preview.name}
          title={preview.name}
          onClose={() => setPreview(null)}
        />
      ) : null}
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
