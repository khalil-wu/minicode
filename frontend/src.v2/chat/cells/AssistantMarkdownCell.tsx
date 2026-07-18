import { ChevronDown, ChevronUp, Copy, Quote, RotateCw, Trash2 } from "lucide-react";
import { useCallback, useState } from "react";
import type React from "react";
import type { AssistantMarkdownCellState, AssistantReplyAttachment } from "./cellTypes";
import type { Citation } from "../../stores/types";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { normalizeCitationText } from "../messages/citationText";
import { sourceFaviconUrl } from "../messages/sourceFavicon";
import { ImageLightbox } from "../../components/ImageLightbox";
import { getWebSocket } from "../../hooks/useWebSocket";
import { useAppStore } from "../../stores";
import { openWebInPreview } from "../openWebInPreview";
import { previewUrlForPath } from "../../shell/fileTreeHelpers";
import "./cells.css";

function interruptBackendRunIfStreaming(): void {
  const latest = useAppStore.getState();
  if (!latest.isStreaming) return;
  getWebSocket()?.send({
    type: "interrupt",
    conversation_id: latest.conversationId || undefined,
  });
  latest.interrupt();
}

export function AssistantMarkdownCell({
  cell,
}: {
  cell: AssistantMarkdownCellState;
}) {
  const [copied, setCopied] = useState(false);
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const deleteMessage = useAppStore((s) => s.deleteMessage);
  const setQuotedMessage = useAppStore((s) => s.setQuotedMessage);
  const rawMarkdown = cell.markdownSource;
  const displayMarkdown = normalizeCitationText(rawMarkdown, cell.citations);
  const sources = uniqueCitationSources(rawMarkdown, cell.citations);
  const visibleSources = sourcesExpanded ? sources : sources.slice(0, 3);
  const hiddenSourceCount = Math.max(0, sources.length - visibleSources.length);
  // Attribute the reply's origin without diverging visually in this phase.
  // "reply" marks an explicit BriefTool reply; "stream" (default) is
  // final-answer text streamed after the tool work.
  const replySource = cell.source === "reply" ? "reply" : "stream";

  const copy = useCallback(() => {
    navigator.clipboard.writeText(displayMarkdown).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  }, [displayMarkdown]);

  const deleteWithConfirm = useCallback(async () => {
    if (!cell.messageId) return;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "删除回复",
      message: "从当前对话视图中删除这条助手回复？",
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
    deleteMessage(cell.messageId);
  }, [cell.messageId, deleteMessage]);

  const quoteReply = useCallback(() => {
    if (!displayMarkdown) return;
    setQuotedMessage({
      id: cell.messageId || cell.id,
      role: "assistant",
      content: displayMarkdown,
    });
    requestAnimationFrame(() => {
      const composerTextarea = document.querySelector("[data-composer-input]") as HTMLTextAreaElement | null;
      if (composerTextarea) {
        composerTextarea.focus();
      }
    });
  }, [cell.id, cell.messageId, displayMarkdown, setQuotedMessage]);

  const regenerate = useCallback(async () => {
    if (!cell.messageId) return;
    const state = useAppStore.getState();

    const index = state.messages.findIndex((item) => item.id === cell.messageId);
    if (index < 0) return;

    let userIndex = -1;
    for (let i = index - 1; i >= 0; i--) {
      if (state.messages[i]?.role === "user") {
        userIndex = i;
        break;
      }
    }

    if (userIndex < 0) return;

    const { showConfirm } = await import("../../overlays/DialogService");
    // Regeneration truncates from this reply to the end of the conversation —
    // tell the user how many trailing messages are also removed, not just the
    // current reply, so the data loss is clear.
    const trailingCount = Math.max(0, state.messages.length - index - 1);
    const ok = await showConfirm({
      title: "重新生成",
      message:
        trailingCount > 0
          ? `将删除当前回复及其后的 ${trailingCount} 条消息，并基于上一条提问重新生成。继续吗？`
          : "将删除当前回复并重新生成。继续吗？",
      confirmLabel: "重新生成",
      danger: false,
    });
    if (!ok) return;

    interruptBackendRunIfStreaming();

    const removeCount = state.messages.length - index;
    for (let i = 0; i < removeCount; i++) {
      const msg = state.messages[index];
      if (msg) {
        deleteMessage(msg.id);
      }
    }

    const userMessage = state.messages[userIndex];
    if (userMessage && userMessage.role === "user") {
      recallMessage(userMessage.id);
      setTimeout(() => {
        const sendBtn = document.querySelector('[data-send-button]') as HTMLButtonElement;
        if (sendBtn) sendBtn.click();
      }, 100);
    }
  }, [cell.messageId, deleteMessage, recallMessage]);

  return (
    <div
      className="assistant-cell-wrap"
      data-streaming={cell.isStreaming ? "true" : "false"}
      data-source={replySource}
    >
      <div className="assistant-cell-content md-prose">
        <MarkdownRenderer
          content={rawMarkdown}
          isStreaming={cell.isStreaming || false}
          citations={cell.citations}
        />
        {cell.attachments && cell.attachments.length > 0 && (
          <div className="assistant-cell-attachments">
            <div className="assistant-cell-sources-title">附件</div>
            <div className="assistant-cell-attachments-list">
              {cell.attachments.map((attachment) => (
                <AttachmentChip key={attachment.path} attachment={attachment} />
              ))}
            </div>
          </div>
        )}
      </div>
      <div className="assistant-cell-actions" data-has-sources={sources.length > 0 ? "true" : "false"}>
        {sources.length > 0 && (
          <div className="assistant-cell-source-strip" aria-label="引用来源">
            {visibleSources.map((source) => {
              const faviconUrl = sourceFaviconUrl(source.url);
              return (
                <a
                  key={source.url}
                  href={source.url}
                  rel="noreferrer"
                  title={source.title || source.url}
                  className="assistant-cell-source-chip"
                  onClick={(event) => {
                    if (openWebInPreview(source.url)) event.preventDefault();
                  }}
                >
                  <span className="assistant-cell-source-favicon" aria-hidden="true">
                    {faviconUrl && (
                      <img
                        src={faviconUrl}
                        alt=""
                        loading="lazy"
                        referrerPolicy="no-referrer"
                        onError={(event) => {
                          event.currentTarget.style.display = "none";
                        }}
                      />
                    )}
                  </span>
                  <span className="assistant-cell-source-label">{source.label}</span>
                </a>
              );
            })}
            {hiddenSourceCount > 0 && (
              <button
                type="button"
                className="assistant-cell-source-more"
                onClick={() => setSourcesExpanded(true)}
                aria-label={`再显示 ${hiddenSourceCount} 个来源`}
              >
                <ChevronDown size={12} aria-hidden="true" />
                再显示 {hiddenSourceCount} 个
              </button>
            )}
            {sourcesExpanded && sources.length > 3 && (
              <button
                type="button"
                className="assistant-cell-source-more"
                onClick={() => setSourcesExpanded(false)}
                aria-label="收起来源"
              >
                <ChevronUp size={12} aria-hidden="true" />
                收起
              </button>
            )}
          </div>
        )}
        <div className="assistant-cell-action-buttons">
          {cell.copyable && (
            <button
              type="button"
              onClick={copy}
              title={copied ? "已复制" : "复制回复"}
              aria-label={copied ? "已复制" : "复制回复"}
              className="cell-action-btn"
            >
              <Copy size={12} />
            </button>
          )}
          {cell.messageId && (
            <>
            <button
              type="button"
              onClick={quoteReply}
              title="引用回复"
              aria-label="引用回复"
              className="cell-action-btn"
            >
              <Quote size={12} />
            </button>
            <button
              type="button"
              onClick={regenerate}
              title="重新生成"
              aria-label="重新生成"
              className="cell-action-btn"
            >
              <RotateCw size={12} />
            </button>
            <button
              type="button"
              onClick={deleteWithConfirm}
              title="删除回复"
              aria-label="删除回复"
              className="cell-action-btn"
            >
              <Trash2 size={12} />
            </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function citationHref(citation: Citation | undefined): string {
  const candidate = String(citation?.url || citation?.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
}

function AttachmentChip({ attachment }: { attachment: AssistantReplyAttachment }) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const openAttachment = () => {
    if (attachment.isImage) {
      setPreviewOpen(true);
      return;
    }
    const store = useAppStore.getState();
    const label = attachment.path.split(/[/\\]/).filter(Boolean).pop() ?? attachment.path;
    store.openEditorFile(attachment.path, label);
    store.setRightStackTab("inspector");
  };

  const fileName = attachment.path.split(/[/\\]/).filter(Boolean).pop() || attachment.path;
  const sizeLabel = formatFileSize(attachment.size);
  const previewUrl = attachment.isImage ? previewUrlForPath(attachment.path) : "";

  return (
    <>
      <button
        type="button"
        className="assistant-cell-attachment"
        title={attachment.path}
        onClick={openAttachment}
      >
        <span className="assistant-cell-attachment-kind" aria-hidden="true">
          {attachment.isImage ? "[image]" : "[file]"}
        </span>
        <span className="assistant-cell-attachment-name">{fileName}</span>
        <span className="assistant-cell-attachment-size">({sizeLabel})</span>
      </button>
      {previewOpen && previewUrl ? (
        <ImageLightbox
          src={previewUrl}
          alt={fileName}
          title={fileName}
          onClose={() => setPreviewOpen(false)}
        />
      ) : null}
    </>
  );
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "?";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function sourceLabel(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

function displaySourceLabel(label: string | undefined, url: string): string {
  const fallback = sourceLabel(url);
  const trimmed = String(label || "").trim();
  if (!trimmed) return fallback;
  const compact = trimmed.replace(/\s+/g, "");
  if (/^[\w.-]+\.[a-z]{2,}(?:\/\S*)?$/i.test(compact)) {
    return compact.replace(/^www\./i, "");
  }
  if (/\.[a-z]\s+[a-z](?:\b|\/)/i.test(trimmed)) {
    return fallback;
  }
  return trimmed;
}

function sourceKey(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./i, "").toLowerCase();
  } catch {
    return url;
  }
}

function uniqueCitationSources(content: string, citations: AssistantMarkdownCellState["citations"] = []): Array<{ url: string; label: string; title?: string }> {
  const citedIndexes = extractInlineCitationIndexes(content);
  if (citedIndexes.size === 0) return [];
  const seen = new Set<string>();
  const sources: Array<{ url: string; label: string; title?: string }> = [];
  for (const [index, citation] of citations.entries()) {
    if (!citedIndexes.has(index + 1)) continue;
    const url = citationHref(citation);
    const key = sourceKey(url);
    if (!url || seen.has(key)) continue;
    seen.add(key);
    sources.push({
      url,
      label: displaySourceLabel(citation.label, url),
      title: citation.title,
    });
  }
  return sources;
}

function extractInlineCitationIndexes(content: string): Set<number> {
  const indexes = new Set<number>();
  for (const match of content.matchAll(/\[(\d+)\]/g)) {
    const index = Number(match[1]);
    if (Number.isFinite(index) && index > 0) indexes.add(index);
  }
  return indexes;
}
