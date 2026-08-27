import { LoaderCircle, RotateCcw, TriangleAlert, X } from "lucide-react";
import { fileIcon } from "../shell/fileTreeHelpers";
import { useAppStore } from "../stores";
import type { ComposerAttachment } from "../stores/types";
import { cancelComposerUpload, retryComposerAttachment } from "./uploads";
import { openAttachmentPreview, openLocalFilePreview } from "../chat/openAttachmentPreview";

export const AttachmentStrip = () => {
  const attachments = useAppStore((s) => s.attachments);
  const removeAttachment = useAppStore((s) => s.removeAttachment);
  const remove = (attachment: ComposerAttachment) => {
    cancelComposerUpload(attachment.id);
    if (attachment.dataUrl?.startsWith("blob:") && typeof URL.revokeObjectURL === "function") {
      URL.revokeObjectURL(attachment.dataUrl);
    }
    removeAttachment(attachment.id);
  };
  if (attachments.length === 0) return null;
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        padding: "4px 0 6px",
      }}
    >
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
      role="button"
      tabIndex={0}
      title={attachmentTitle(a)}
      aria-label={`预览 ${a.name}`}
      onClick={openPreview}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openPreview();
        }
      }}
      style={{
        position: "relative",
        width: 64,
        height: 64,
        borderRadius: "var(--radius-sm, 6px)",
        border: `1px solid ${a.status === "error" ? "var(--state-danger)" : "var(--border-subtle)"}`,
        overflow: "hidden",
        cursor: "zoom-in",
      }}
      className="shrink-0"
    >
      <img
        src={a.dataUrl}
        alt={a.name}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
      {a.status === "uploading" && (
        <div
          role="status"
          aria-label={`${uploadStatusLabel(a)} ${a.name}`}
          style={{
            position: "absolute",
            inset: 0,
            background: "var(--backdrop-overlay)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 4,
            color: "var(--text-on-accent)",
            fontSize: "var(--text-xs)",
          }}
        >
          <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
          <span>{uploadStatusLabel(a)}</span>
          {a.uploadPhase !== "processing" ? (
            <span
              aria-hidden="true"
              style={{
                width: 42,
                height: 3,
                overflow: "hidden",
                borderRadius: 999,
                background: "color-mix(in srgb, currentColor 28%, transparent)",
              }}
            >
              <span
                style={{
                  display: "block",
                  width: `${uploadPercent(a)}%`,
                  height: "100%",
                  borderRadius: "inherit",
                  background: "currentColor",
                  transition: "width var(--transition-fast)",
                }}
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
          style={{
            position: "absolute",
            left: 3,
            right: 3,
            bottom: 3,
            minHeight: 16,
            padding: "1px 4px",
            borderRadius: "var(--radius-sm, 4px)",
            border: 0,
            background: "var(--backdrop-strong)",
            color: a.status === "error" ? "var(--state-danger)" : "var(--state-warning)",
            fontSize: "var(--text-3xs)",
            fontWeight: "var(--fw-bold)",
            lineHeight: "14px",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 2,
            cursor: a.status === "error" && a.localFile ? "pointer" : "default",
          }}
        >
          {a.status === "error" && a.localFile
            ? <RotateCcw size={12} aria-hidden="true" />
            : <TriangleAlert size={12} aria-hidden="true" />}
          <span>{a.status === "error" ? "重试" : "警告"}</span>
        </button>
      )}
      <button
        onClick={(event) => {
          event.stopPropagation();
          onRemove();
        }}
        aria-label={`移除 ${a.name}`}
        style={{
          position: "absolute",
          top: 2,
          right: 2,
          width: 18,
          height: 18,
          borderRadius: "50%",
          background: "var(--backdrop-strong)",
          color: "var(--text-on-accent)",
          border: 0,
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 0,
        }}
      >
        <X size={14} />
      </button>
    </div>
  );
};

const truncateFilename = (name: string): string => {
  if (name.length <= 25) return name;
  const lastDotIndex = name.lastIndexOf(".");
  if (lastDotIndex === -1 || lastDotIndex === 0) {
    return `${name.slice(0, 22)}...`;
  }
  const ext = name.slice(lastDotIndex);
  const base = name.slice(0, Math.max(0, 22 - ext.length));
  return `${base}...${ext}`;
};

const FileChip = ({ attachment: a, onRemove }: { attachment: ComposerAttachment; onRemove: () => void }) => {
  const problem = a.error || (a.status === "error" ? "上传失败" : "");
  const problemColor = a.status === "error" ? "var(--state-danger)" : "var(--state-warning)";
  const canPreview = Boolean(a.artifactId || a.localFile || a.dataUrl);

  return (
    <div
      role={canPreview ? "button" : undefined}
      tabIndex={canPreview ? 0 : undefined}
      aria-label={canPreview ? `预览 ${a.name}` : undefined}
      onClick={() => {
        if (canPreview) openComposerAttachment(a);
      }}
      onKeyDown={(event) => {
        if ((event.key === "Enter" || event.key === " ") && canPreview) {
          event.preventDefault();
          event.currentTarget.click();
        }
      }}
      title={attachmentTitle(a)}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: a.status === "uploading" ? "3px 4px 5px 8px" : "3px 4px 3px 8px",
        background: "var(--attachment-chip-bg)",
        border: `1px solid ${problem ? problemColor : "var(--border-subtle)"}`,
        borderRadius: "var(--radius-sm, 6px)",
        fontSize: "var(--text-xs)",
        color: a.status === "error" ? "var(--state-danger)" : "var(--attachment-chip-fg)",
        maxWidth: problem ? 420 : 260,
        minWidth: 0,
        cursor: canPreview ? "pointer" : "default",
      }}
    >
      {a.status === "uploading" ? (
        <LoaderCircle size={14} className="animate-spin shrink-0" aria-hidden="true" />
      ) : problem ? (
        <TriangleAlert size={14} className="shrink-0" aria-hidden="true" style={{ color: problemColor }} />
      ) : (
        fileIcon(a.name, { size: 14, className: "composer-attachment-file-icon" })
      )}
      <span className="truncate" style={{ minWidth: 0 }}>{truncateFilename(a.name)}</span>
      {a.status === "uploading" ? (
        <span
          role="status"
          aria-label={`${uploadStatusLabel(a)} ${a.name}`}
          style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}
        >
          {uploadStatusLabel(a)}
        </span>
      ) : null}
      {a.inputSource === "pasted_text" && a.sourceCharCount ? (
        <span style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}>
          {a.sourceCharCount.toLocaleString()} chars
        </span>
      ) : null}
      {problem && (
        <span className="truncate" style={{ color: problemColor, minWidth: 0 }}>
          {problem}
        </span>
      )}
      {a.status === "error" && a.localFile ? (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            retryComposerAttachment(a.id);
          }}
          aria-label={`重新上传 ${a.name}`}
          title="重新上传"
          className="shrink-0"
          style={fileChipActionStyle}
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
        className="shrink-0"
        style={{
          width: 24,
          height: 24,
          background: "transparent",
          color: "var(--text-muted)",
          border: 0,
          borderRadius: "var(--radius-sm, 4px)",
          cursor: "pointer",
          padding: 0,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <X size={14} />
      </button>
      {a.status === "uploading" && a.uploadPhase !== "processing" ? (
        <span
          aria-hidden="true"
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            height: 2,
            overflow: "hidden",
            borderRadius: "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)",
            background: "var(--surface-soft)",
          }}
        >
          <span
            style={{
              display: "block",
              width: `${uploadPercent(a)}%`,
              height: "100%",
              background: "var(--accent-primary)",
              transition: "width var(--transition-fast)",
            }}
          />
        </span>
      ) : null}
    </div>
  );
};

const uploadPercent = (attachment: ComposerAttachment): number => {
  const value = Number(attachment.progress ?? 0);
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
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

const fileChipActionStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  background: "transparent",
  color: "var(--text-muted)",
  border: 0,
  borderRadius: "var(--radius-sm, 4px)",
  cursor: "pointer",
  padding: 0,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};
