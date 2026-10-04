import { LoaderCircle, RotateCcw, TriangleAlert, X } from "lucide-react";
import { fileIcon } from "../lib/file-icons";
import { useAppStore } from "../stores";
import type { ComposerAttachment } from "../stores/types";
import { cancelComposerUpload, retryComposerAttachment } from "./uploads";
import { openAttachmentPreview, openLocalFilePreview } from "../chat/openAttachmentPreview";

const NO_ATTACHMENTS: ComposerAttachment[] = [];
export const AttachmentStrip = ({ conversationId }: { conversationId?: string } = {}) => {
  const attachments = useAppStore((s) => conversationId ? s.sideChats[conversationId]?.attachments ?? NO_ATTACHMENTS : s.attachments);
  const removeAttachment = useAppStore((s) => s.removeAttachment);
  const remove = (attachment: ComposerAttachment) => {
    cancelComposerUpload(attachment.id);
    if (conversationId) {
      if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL(attachment.dataUrl);
      useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [conversationId]: {
        ...state.sideChats[conversationId], attachments: state.sideChats[conversationId].attachments!.filter((item) => item.id !== attachment.id),
      } } }));
    } else removeAttachment(attachment.id);
  };
  if (attachments.length === 0) return null;
  return (
    <div className="composer-attachment-strip">
      {attachments.map((a) =>
        a.dataUrl && a.type.startsWith("image/") ? (
          <ImageChip key={a.id} attachment={a} onRemove={() => remove(a)} />
        ) : (
          <FileChip key={a.id} attachment={a} onRemove={() => remove(a)} />
        ),
      )}
    </div>
  );
};

const ImageChip = ({ attachment: a, onRemove }: { attachment: ComposerAttachment; onRemove: () => void }) => {
  const openPreview = () => openComposerAttachment(a);

  return (
    <div
      title={attachmentTitle(a)}
      className="composer-attachment-image"
      data-status={a.status}
    >
      <button
        type="button"
        aria-label={`预览 ${a.name}`}
        onClick={openPreview}
        className="composer-attachment-image-preview"
      >
        <img
          src={a.dataUrl}
          alt={a.name}
        />
      </button>
      {a.status === "uploading" && (
        <div
          role="status"
          aria-label={`${uploadStatusLabel(a)} ${a.name}`}
          className="composer-attachment-image-upload"
        >
          <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
          <span>{uploadStatusLabel(a)}</span>
          {a.uploadPhase !== "processing" ? (
            <span
              aria-hidden="true"
              className="composer-attachment-image-progress"
            >
              <span
                style={{ width: `${uploadPercent(a)}%` }}
              />
            </span>
          ) : null}
        </div>
      )}
      {(a.status === "error" || a.error) && (
        <button
          type="button"
          aria-label={`${a.name} ${a.status === "error" ? "上传失败" : "上传警告"}`}
          title={a.error || (a.status === "error" ? "上传失败" : "上传警告")}
          onClick={(event) => {
            event.stopPropagation();
            if (a.status === "error" && a.localFile) retryComposerAttachment(a.id);
          }}
          className="composer-attachment-image-problem"
          data-retry={a.status === "error" && Boolean(a.localFile)}
        >
          {a.status === "error" && a.localFile
            ? <RotateCcw size={12} aria-hidden="true" />
            : <TriangleAlert size={12} aria-hidden="true" />}
          <span>{a.status === "error" ? "重试" : "警告"}</span>
        </button>
      )}
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onRemove();
        }}
        aria-label={`移除 ${a.name}`}
        className="composer-attachment-image-remove"
      >
        <X size={14} />
      </button>
    </div>
  );
};

const FileChip = ({ attachment: a, onRemove }: { attachment: ComposerAttachment; onRemove: () => void }) => {
  const problem = a.error || (a.status === "error" ? "上传失败" : "");
  const canPreview = Boolean(a.artifactId || a.localFile || a.dataUrl);

  return (
    <div
      title={attachmentTitle(a)}
      className="composer-attachment-file"
      data-status={a.status}
      data-problem={problem ? a.status === "error" ? "error" : "warning" : undefined}
    >
      <button
        type="button"
        disabled={!canPreview}
        aria-label={`预览 ${a.name}`}
        onClick={() => openComposerAttachment(a)}
        className="composer-attachment-file-preview"
      >
      {a.status === "uploading" ? (
        <LoaderCircle size={14} className="animate-spin shrink-0" aria-hidden="true" />
      ) : problem ? (
        <TriangleAlert size={14} className="composer-attachment-problem-icon" aria-hidden="true" />
      ) : (
        fileIcon(a.name, { size: 16, className: "composer-attachment-file-icon" })
      )}
      <span className="composer-attachment-name">{a.name}</span>
      {a.status === "uploading" ? (
        <span
          role="status"
          aria-label={`${uploadStatusLabel(a)} ${a.name}`}
          className="composer-attachment-meta"
        >
          {uploadStatusLabel(a)}
        </span>
      ) : null}
      {a.inputSource === "pasted_text" && a.sourceCharCount ? (
        <span className="composer-attachment-meta">
          {a.sourceCharCount.toLocaleString()} chars
        </span>
      ) : null}
      {problem && (
        <span className="composer-attachment-problem-text">
          {problem}
        </span>
      )}
      </button>
      {a.status === "error" && a.localFile ? (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            retryComposerAttachment(a.id);
          }}
          aria-label={`重新上传 ${a.name}`}
          title="重新上传"
          className="composer-attachment-action"
        >
          <RotateCcw size={14} />
        </button>
      ) : null}
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onRemove();
        }}
        aria-label={`移除 ${a.name}`}
        className="composer-attachment-action"
      >
        <X size={14} />
      </button>
      {a.status === "uploading" && a.uploadPhase !== "processing" ? (
        <span
          aria-hidden="true"
          className="composer-attachment-file-progress"
        >
          <span
            style={{ width: `${uploadPercent(a)}%` }}
          />
        </span>
      ) : null}
    </div>
  );
};

const uploadPercent = (attachment: ComposerAttachment): number => {
  return Math.round(attachment.progress ?? 0);
};

const openComposerAttachment = (attachment: ComposerAttachment): boolean => {
  if (attachment.status === "ready" && attachment.artifactId) {
    return openAttachmentPreview({
      artifactId: attachment.artifactId,
      name: attachment.name,
      mediaType: attachment.type,
      kind: String(attachment.attachment?.kind || (attachment.type.startsWith("image/") ? "image" : "document")),
      conversationId: attachment.conversationId,
    });
  }
  return openLocalFilePreview({
    id: attachment.id,
    name: attachment.name,
    mediaType: attachment.type,
    kind: String(attachment.attachment?.kind || (attachment.type.startsWith("image/") ? "image" : "document")),
    file: attachment.localFile,
    url: attachment.dataUrl,
    conversationId: attachment.conversationId,
  });
};

const uploadStatusLabel = (attachment: ComposerAttachment): string =>
  attachment.uploadPhase === "processing"
    ? "处理中"
    : `上传中 ${uploadPercent(attachment)}%`;

const attachmentTitle = (attachment: ComposerAttachment): string => {
  if (attachment.error) return attachment.error;
  return attachment.name || "附件";
};
