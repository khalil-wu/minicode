import { AlertTriangle, ChevronDown, ChevronUp, Copy, Download, FileText, GitBranch, Image as ImageIcon, Maximize2, Quote, RotateCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type React from "react";
import { createPortal } from "react-dom";
import type { AssistantMarkdownCellState, AssistantReplyAttachment } from "./cellTypes";
import type { ArtifactPreview, Citation, ProgressContentBlock } from "../../stores/types";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { normalizeCitationText } from "../messages/citationText";
import { BrandIcon } from "../../components/BrandIcon";
import { useAppStore } from "../../stores";
import { openWebTarget } from "../openWebTarget";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { isDesktop, openPath, revealPath } from "../../desktop/runtime";
import { useContextMenu } from "../../components/useContextMenu";
import { openArtifactPreview, openWorkspaceFilePreview } from "../openAttachmentPreview";
import { sendChatMessage } from "../sendChatMessage";
import { isWindowsLikeWorkspacePath, normalizeWorkspacePath } from "../../lib/workspace-path";
import { pushToast } from "../../overlays/ToastContainer";
import { artifactRawResourceUrlWithToken } from "../../protocol/api";
import { getWebSocket } from "../../hooks/useWebSocket";
import "./cells.css";

export function AssistantMarkdownCell({
  cell,
  isTranscriptMode = false,
}: {
  cell: AssistantMarkdownCellState;
  isTranscriptMode?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const setQuotedMessage = useAppStore((s) => s.setQuotedMessage);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const conversationId = useAppStore((s) => s.conversationId);
  const rawMarkdown = cell.markdownSource;
  const displayMarkdown = normalizeCitationText(rawMarkdown, cell.citations);
  const sources = uniqueCitationSources(rawMarkdown, cell.citations);
  const visibleSources = sourcesExpanded ? sources : sources.slice(0, 3);
  const hiddenSourceCount = Math.max(0, sources.length - visibleSources.length);
  // Attribute the reply's origin without diverging visually in this phase.
  // "reply" marks an explicit BriefTool reply; "stream" (default) is
  // final-answer text streamed after the tool work.
  const replySource = cell.source === "reply" ? "reply" : "stream";
  const visibleAttachments = useMemo(() => {
    const normalizedMarkdown = rawMarkdown.replace(/\\/g, "/");
    return (cell.attachments ?? []).filter((attachment) => {
      const normalizedPath = normalizeWorkspacePath(attachment.path);
      const caseInsensitive = isWindowsLikeWorkspacePath(workingDirectory)
        || isWindowsLikeWorkspacePath(attachment.path);
      const markdownKey = caseInsensitive ? normalizedMarkdown.toLowerCase() : normalizedMarkdown;
      const pathKey = caseInsensitive
        ? normalizedPath.toLowerCase()
        : normalizedPath;
      return !pathKey || !markdownKey.includes(pathKey);
    });
  }, [cell.attachments, rawMarkdown, workingDirectory]);
  const imageArtifacts = useMemo(
    () => (cell.artifacts ?? []).filter((artifact) => artifact.kind === "image"),
    [cell.artifacts],
  );
  const otherArtifacts = useMemo(
    () => (cell.artifacts ?? []).filter((artifact) => artifact.kind !== "image"),
    [cell.artifacts],
  );
  const visibleImageProgress = useMemo(() => {
    const progress = cell.imageProgress ?? [];
    if (imageArtifacts.length === 0) return progress;
    // The validated Artifact is the completed state. Keep only a genuine
    // failure alongside it; completed/running placeholders must be replaced
    // rather than rendered as a second image-generation row.
    return progress.filter((item) => item.status === "failed");
  }, [cell.imageProgress, imageArtifacts.length]);
  const hasPendingImage = imageArtifacts.length === 0
    && (cell.imageProgress ?? []).some((progress) => progress.status !== "failed");
  const isSettled = !cell.isStreaming && !hasPendingImage;

  const copy = useCallback(() => {
    navigator.clipboard.writeText(displayMarkdown).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  }, [displayMarkdown]);

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

    // Re-read the store after the await: the user may have switched
    // conversations while the confirm dialog was open, and sending the
    // retry against the pre-dialog conversation id would truncate and
    // re-run the wrong conversation.
    const current = useAppStore.getState();
    if (current.conversationId !== state.conversationId) return;
    const stillPresent = current.messages.findIndex((item) => item.id === cell.messageId);
    if (stillPresent < 0) return;

    const userMessage = state.messages[userIndex];
    if (userMessage && userMessage.role === "user") {
      const attachmentRefs = userMessage.attachmentRefs ?? [];
      sendChatMessage({
        displayContent: userMessage.content,
        backendContent: userMessage.content,
        conversationId: current.conversationId || undefined,
        contextRefs: userMessage.contextRefs ?? [],
        attachmentRefs,
        attachments: attachmentRefs.map((attachment) => ({
          id: attachment.id,
          kind: attachment.kind,
          file_name: attachment.name,
          media_type: attachment.mediaType,
          artifact_id: attachment.artifactId,
          doc_id: attachment.docId,
          size_bytes: attachment.sizeBytes,
          input_source: attachment.inputSource,
          source_char_count: attachment.sourceCharCount,
        })),
        retryFromMessageId: userMessage.id,
      });
    }
  }, [cell.messageId]);

  const forkConversation = useCallback(() => {
    if (!cell.messageId) return;
    const state = useAppStore.getState();
    const messageIndex = state.messages.findIndex((message) => message.id === cell.messageId);
    sendClientCommand({
      type: "context.fork",
      message_id: cell.messageId,
      ...(messageIndex >= 0 ? { message_index: messageIndex } : {}),
      create_branch: true,
      activate: true,
    });
  }, [cell.messageId]);

  return (
    <div
      className="assistant-cell-wrap"
      data-streaming={cell.isStreaming ? "true" : "false"}
      data-source={replySource}
    >
      <div className="assistant-cell-content md-prose">
        {(cell.markdownBeforeArtifacts ?? rawMarkdown) && (
          <MarkdownRenderer
            content={cell.markdownBeforeArtifacts ?? rawMarkdown}
            isStreaming={cell.isStreaming || false}
            citations={cell.citations}
          />
        )}
        {(visibleImageProgress.length > 0 || imageArtifacts.length > 0 || otherArtifacts.length > 0) && (
          <div className="assistant-cell-generated" aria-label="生成结果">
            {visibleImageProgress.map((progress) => (
              <ImageGenerationProgress
                key={progress.id}
                progress={progress}
                fallbackFailureMessage={cell.failureMessage}
                recoverable={cell.failureRecoverable}
              />
            ))}
            {imageArtifacts.map((artifact) => (
              <GeneratedArtifactCard
                key={artifact.artifactId}
                artifact={artifact}
                conversationId={conversationId || undefined}
              />
            ))}
            {otherArtifacts.map((artifact) => (
              <GeneratedArtifactCard
                key={artifact.artifactId}
                artifact={artifact}
                conversationId={conversationId || undefined}
              />
            ))}
          </div>
        )}
        {cell.markdownAfterArtifacts && (
          <MarkdownRenderer
            content={cell.markdownAfterArtifacts}
            isStreaming={cell.isStreaming || false}
            citations={cell.citations}
          />
        )}
        {visibleAttachments.length > 0 && (
          <div className="assistant-cell-attachments">
            <div className="assistant-cell-sources-title">附件</div>
            <div className="assistant-cell-attachments-list">
              {visibleAttachments.map((attachment) => (
                <AttachmentChip key={attachment.path} attachment={attachment} />
              ))}
            </div>
          </div>
        )}
      </div>
      {(sources.length > 0 || (!isTranscriptMode && isSettled)) && (
      <div className="assistant-cell-actions" data-has-sources={sources.length > 0 ? "true" : "false"}>
        {sources.length > 0 && (
          <div className="assistant-cell-source-strip" aria-label="引用来源">
            {visibleSources.map((source) => source.url ? (
                <a
                  key={source.key}
                  href={source.url}
                  rel="noreferrer"
                  title={source.title || source.url}
                  className="assistant-cell-source-chip"
                  onClick={(event) => {
                    if (openWebTarget(source.url!)) event.preventDefault();
                  }}
                >
                  <span className="assistant-cell-source-favicon" aria-hidden="true">
                    <BrandIcon
                      value={`${source.label} ${source.title || ""} ${source.url}`}
                      websiteUrl={source.url}
                      fallback="web"
                      size={14}
                    />
                  </span>
                  <span className="assistant-cell-source-label">{source.label}</span>
                </a>
              ) : (
                <span
                  key={source.key}
                  title={source.title || source.label}
                  className="assistant-cell-source-chip"
                  role="note"
                >
                  <span className="assistant-cell-source-favicon" aria-hidden="true">
                    <BrandIcon value={`${source.label} ${source.title || ""}`} fallbackIcon={<FileText size={14} />} size={14} />
                  </span>
                  <span className="assistant-cell-source-label">{source.label}</span>
                </span>
            ))}
            {hiddenSourceCount > 0 && (
              <button
                type="button"
                className="assistant-cell-source-more"
                onClick={() => setSourcesExpanded(true)}
                aria-label={`再显示 ${hiddenSourceCount} 个来源`}
              >
                <ChevronDown size={14} aria-hidden="true" />
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
                <ChevronUp size={14} aria-hidden="true" />
                收起
              </button>
            )}
          </div>
        )}
        {!isTranscriptMode && isSettled && <div className="assistant-cell-action-buttons">
          {cell.copyable && (
            <button
              type="button"
              onClick={copy}
              title={copied ? "已复制" : "复制回复"}
              aria-label={copied ? "已复制" : "复制回复"}
              className="cell-action-btn"
            >
              <Copy size={14} />
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
              <Quote size={14} />
            </button>
            <button
              type="button"
              onClick={regenerate}
              title="重新生成"
              aria-label="重新生成"
              className="cell-action-btn"
            >
              <RotateCw size={14} />
            </button>
            <button
              type="button"
              onClick={forkConversation}
              title="从此处分支"
              aria-label="从此处分支"
              className="cell-action-btn"
            >
              <GitBranch size={14} />
            </button>
            </>
          )}
        </div>}
      </div>
      )}
    </div>
  );
}

const SAFE_INLINE_IMAGE_DATA_URL = /^data:image\/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]+$/i;

function safeInlineImageUrl(artifact: ArtifactPreview): string {
  const value = String(artifact.url || "").trim();
  if (!value || value.length > 16 * 1024 * 1024) return "";
  return SAFE_INLINE_IMAGE_DATA_URL.test(value) ? value : "";
}

function ImageGenerationProgress({
  progress,
  fallbackFailureMessage,
  recoverable,
}: {
  progress: ProgressContentBlock;
  fallbackFailureMessage?: string;
  recoverable?: boolean;
}) {
  const failed = progress.status === "failed";
  const completed = progress.status === "completed";
  const awaitingImage = !failed;
  const detail = String(
    progress.detail
      || (failed ? fallbackFailureMessage : "")
      || progress.summary
      || progress.message,
  ).trim();
  return (
    <div
      className={failed
        ? "assistant-cell-image-placeholder assistant-cell-image-error"
        : "assistant-cell-image-placeholder"}
      data-running={awaitingImage ? "true" : "false"}
      data-status={progress.status}
      role={failed ? "alert" : "status"}
      aria-live="polite"
    >
      <span className="assistant-cell-image-placeholder-visual" aria-hidden="true">
        {failed
          ? <AlertTriangle size={22} />
          : <ImageIcon size={24} />}
      </span>
      <span className="assistant-cell-image-placeholder-copy">
        <strong>
          {failed
            ? "图像生成失败"
            : completed
              ? "正在载入生成结果"
              : progress.message || "正在生成图像"}
        </strong>
        {detail && detail !== progress.message && <small>{detail}</small>}
        {failed && (
          <small>{recoverable ? "服务暂时不可用，可以重试。" : "请检查图像模型与 Provider 配置后重试。"}</small>
        )}
      </span>
    </div>
  );
}

function GeneratedArtifactCard({
  artifact,
  conversationId,
}: {
  artifact: ArtifactPreview;
  conversationId?: string;
}) {
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [lightboxUrl, setLightboxUrl] = useState("");
  const [loadedImageUrl, setLoadedImageUrl] = useState("");
  const [failedImageUrl, setFailedImageUrl] = useState("");
  const [imageReloadNonce, setImageReloadNonce] = useState(0);
  const isConnected = useAppStore((state) => state.isConnected);
  const inlineUrl = artifact.kind === "image" ? safeInlineImageUrl(artifact) : "";
  const mediaType = String(artifact.mediaType || "").split(";", 1)[0].trim().toLowerCase();
  const sessionId = isConnected ? String(getWebSocket()?.sessionId || "").trim() : "";
  const persistedImageUrl = useMemo(() => {
    if (
      artifact.kind !== "image"
      || inlineUrl
      || !conversationId
      || !sessionId
      || !/^image\/(?:png|jpeg|jpg|gif|webp)$/i.test(mediaType)
    ) {
      return "";
    }
    const value = artifactRawResourceUrlWithToken(artifact.artifactId, sessionId, conversationId);
    if (!value || imageReloadNonce === 0) return value;
    try {
      const url = new URL(value);
      url.searchParams.set("reload", String(imageReloadNonce));
      return url.toString();
    } catch {
      return `${value}${value.includes("?") ? "&" : "?"}reload=${imageReloadNonce}`;
    }
  }, [artifact.artifactId, artifact.kind, conversationId, imageReloadNonce, inlineUrl, mediaType, sessionId]);
  const imageUrl = inlineUrl || persistedImageUrl;
  const imageLoaded = Boolean(imageUrl && loadedImageUrl === imageUrl);
  const imageFailed = Boolean(imageUrl && failedImageUrl === imageUrl);

  const freshImageUrl = () => {
    if (inlineUrl) return inlineUrl;
    if (!conversationId || !sessionId) return imageUrl;
    return artifactRawResourceUrlWithToken(artifact.artifactId, sessionId, conversationId) || imageUrl;
  };

  const openImageLightbox = () => {
    const nextUrl = freshImageUrl();
    if (!nextUrl) {
      pushToast("图片内容尚未载入。", "warning", 2400);
      return;
    }
    setLightboxUrl(nextUrl);
    setLightboxOpen(true);
  };

  const openPreview = () => {
    openArtifactPreview({
      artifactId: artifact.artifactId,
      name: artifact.summary || (artifact.kind === "image" ? "生成图片" : "生成文件"),
      summary: artifact.summary,
      mediaType,
      kind: artifact.kind,
      conversationId,
    });
  };

  useEffect(() => {
    if (!lightboxOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightboxOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [lightboxOpen]);

  const copyImage = async () => {
    const sourceUrl = freshImageUrl();
    if (!sourceUrl) {
      pushToast("图片内容尚未载入，暂时无法复制。", "warning", 3200);
      return;
    }
    try {
      const ClipboardItemCtor = globalThis.ClipboardItem;
      if (!ClipboardItemCtor || typeof navigator.clipboard?.write !== "function") {
        throw new Error("Image clipboard is unavailable.");
      }
      const response = await fetch(sourceUrl);
      if (!response.ok) throw new Error(`Image request failed (${response.status}).`);
      const blob = await response.blob();
      await navigator.clipboard.write([new ClipboardItemCtor({ [blob.type || mediaType || "image/png"]: blob })]);
      pushToast("图片已复制", "success", 1800);
    } catch {
      pushToast("当前系统无法直接复制图片，请使用保存图片。", "warning", 3600);
    }
  };

  const saveImage = async () => {
    const sourceUrl = freshImageUrl();
    if (!sourceUrl) {
      pushToast("图片内容尚未载入，暂时无法保存。", "warning", 3200);
      return;
    }
    try {
      const response = await fetch(sourceUrl);
      if (!response.ok) throw new Error(`Image request failed (${response.status}).`);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = generatedImageFilename(artifact);
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch {
      pushToast("图片保存失败，请稍后重试。", "warning", 3200);
    }
  };

  if (artifact.kind === "image") {
    return (
      <div
        className="assistant-cell-image-card"
        data-artifact-id={artifact.artifactId}
      >
        {imageUrl && !imageFailed ? (
          <button
            type="button"
            className="assistant-cell-image-open"
            data-loaded={imageLoaded ? "true" : "false"}
            onClick={() => {
              if (imageLoaded) openImageLightbox();
            }}
            aria-label="查看生成图片大图"
          >
            <img
              className="assistant-cell-generated-image"
              src={imageUrl}
              alt="模型生成的图片"
              loading="lazy"
              decoding="async"
              onLoad={() => {
                setLoadedImageUrl(imageUrl);
                if (failedImageUrl === imageUrl) setFailedImageUrl("");
              }}
              onError={() => setFailedImageUrl(imageUrl)}
            />
            {!imageLoaded && (
              <span className="assistant-cell-image-load-mask" role="status" aria-label="正在载入生成图片">
                <span aria-hidden="true"><ImageIcon size={24} /></span>
                <strong>正在载入生成图片</strong>
              </span>
            )}
          </button>
        ) : (
          <button
            type="button"
            className="assistant-cell-image-unavailable"
            onClick={() => {
              if (isConnected) setImageReloadNonce(Date.now());
            }}
            disabled={!isConnected}
          >
            <ImageIcon size={24} aria-hidden="true" />
            <span>
              {isConnected
                ? imageFailed
                  ? "图片载入失败，点击重试"
                  : "正在准备生成图片"
                : "连接恢复后将自动载入图片"}
            </span>
          </button>
        )}
        {imageLoaded && (
          <span className="assistant-cell-image-actions" aria-label="图片操作">
            <button type="button" onClick={openImageLightbox} title="查看大图" aria-label="查看大图">
              <Maximize2 size={15} />
            </button>
            <button type="button" onClick={() => void copyImage()} title="复制图片" aria-label="复制图片">
              <Copy size={15} />
            </button>
            <button type="button" onClick={() => void saveImage()} title="保存图片" aria-label="保存图片">
              <Download size={15} />
            </button>
          </span>
        )}
        {lightboxOpen && lightboxUrl && createPortal(
          <div
              className="assistant-cell-image-lightbox"
              role="dialog"
              aria-modal="true"
              aria-label="生成图片大图"
              onClick={() => setLightboxOpen(false)}
            >
              <div className="assistant-cell-image-lightbox-content" onClick={(event) => event.stopPropagation()}>
                <img src={lightboxUrl} alt="模型生成的图片" />
                <button
                  type="button"
                  className="assistant-cell-image-lightbox-close"
                  onClick={() => setLightboxOpen(false)}
                  title="关闭"
                  aria-label="关闭大图"
                >
                  <X size={18} />
                </button>
              </div>
            </div>,
          document.body,
        )}
      </div>
    );
  }

  return (
    <button
      type="button"
      className="assistant-cell-artifact-card"
      onClick={openPreview}
      aria-label={`打开${artifact.summary || "生成文件"}`}
    >
      <FileText size={18} aria-hidden="true" />
      <span>
        <strong>{artifact.summary || "生成文件"}</strong>
        <small>{[mediaType, artifact.bytes != null ? formatFileSize(artifact.bytes) : ""].filter(Boolean).join(" · ")}</small>
      </span>
    </button>
  );
}

function generatedImageFilename(artifact: ArtifactPreview): string {
  const extension = artifact.mediaType === "image/jpeg"
    ? "jpg"
    : artifact.mediaType === "image/webp"
      ? "webp"
      : artifact.mediaType === "image/gif"
        ? "gif"
        : "png";
  const base = String(artifact.summary || "generated-image")
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "generated-image";
  return `${base}.${extension}`;
}

function citationHref(citation: Citation | undefined): string {
  const candidate = String(citation?.url || citation?.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
}

function AttachmentChip({ attachment }: { attachment: AssistantReplyAttachment }) {
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const conversationId = useAppStore((state) => state.conversationId);
  const openAttachment = () => {
    openWorkspaceFilePreview({
      path: attachment.path,
      name: fileName,
      mediaType: attachment.isImage ? "image/*" : undefined,
      kind: attachment.isImage ? "image" : "file",
      workspaceRoot: workingDirectory,
      conversationId: conversationId || undefined,
    });
  };

  const fileName = attachment.path.split(/[/\\]/).filter(Boolean).pop() || attachment.path;
  const sizeLabel = formatFileSize(attachment.size);
  const { onContextMenu, menu } = useContextMenu(() => [
    // OS shell actions have no meaning in browser mode; offering them there
    // produced a menu entry that did nothing at all.
    ...(isDesktop() ? [
      { label: "使用默认应用打开", onClick: () => { void openPath(attachment.path); } },
      { label: "在资源管理器中显示", onClick: () => { void revealPath(attachment.path); } },
    ] : []),
    { label: "", separator: true },
    { label: "复制路径", onClick: () => { void navigator.clipboard.writeText(attachment.path); } },
  ]);

  return (
      <span onContextMenu={onContextMenu}>
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
        {menu}
      </span>
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
  if (!/^https?:\/\//i.test(url)) return url;
  try {
    return new URL(url).hostname.replace(/^www\./i, "").toLowerCase();
  } catch {
    return url;
  }
}

function uniqueCitationSources(content: string, citations: AssistantMarkdownCellState["citations"] = []): Array<{ key: string; url?: string; label: string; title?: string }> {
  const citedIndexes = extractInlineCitationIndexes(content);
  const seen = new Set<string>();
  const sources: Array<{ key: string; url?: string; label: string; title?: string }> = [];
  for (const [index, citation] of citations.entries()) {
    if (citedIndexes.size > 0) {
      if (!citedIndexes.has(index + 1)) continue;
    } else if (!citation.providerNative) {
      continue;
    }
    const url = citationHref(citation);
    const source = String(citation.source || citation.url || "").trim();
    const key = sourceKey(source);
    if (!source || seen.has(key)) continue;
    seen.add(key);
    sources.push({
      key,
      ...(url ? { url } : {}),
      label: url
        ? displaySourceLabel(citation.label, url)
        : citation.label || citation.title || "Provider location",
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
