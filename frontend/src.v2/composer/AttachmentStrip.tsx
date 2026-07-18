import { LoaderCircle, RotateCcw, TriangleAlert, X } from "lucide-react";
import { fileIcon } from "../shell/fileTreeHelpers";
import { useCallback, useState } from "react";
import { ImageLightbox } from "../components/ImageLightbox";
import { useAppStore } from "../stores";
import type { ComposerAttachment } from "../stores/types";
import { cancelComposerUpload, retryComposerAttachment } from "./uploads";

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
  const [previewOpen, setPreviewOpen] = useState(false);
  const openPreview = useCallback(() => setPreviewOpen(true), []);
  const closePreview = useCallback(() => setPreviewOpen(false), []);

  return (
    <div
      role="button"
      tabIndex={0}
      title={attachmentTitle(a)}
      aria-label={`Preview ${a.name}`}
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
          aria-label={`Uploading ${a.name}`}
          style={{
            position: "absolute",
            inset: 0,
            background: "var(--backdrop-overlay)",
            display: "grid",
            placeItems: "center",
            color: "var(--text-on-accent)",
            fontSize: "var(--text-xs)",
          }}
        >
          <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
        </div>
      )}
      {(a.status === "error" || a.error) && (
        <div
          aria-label={`${a.name} ${a.status === "error" ? "upload failed" : "upload warning"}`}
          style={{
            position: "absolute",
            left: 3,
            right: 3,
            bottom: 3,
            minHeight: 16,
            padding: "1px 4px",
            borderRadius: "var(--radius-sm, 4px)",
            background: "var(--backdrop-strong)",
            color: a.status === "error" ? "var(--state-danger)" : "var(--state-warning)",
            fontSize: 10,
            fontWeight: 700,
            lineHeight: "14px",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            textAlign: "center",
          }}
        >
          <TriangleAlert size={12} aria-hidden="true" />
        </div>
      )}
      <button
        onClick={(event) => {
          event.stopPropagation();
          onRemove();
        }}
        aria-label={`Remove ${a.name}`}
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
        <X size={11} />
      </button>
      {previewOpen && a.dataUrl ? (
        <ImageLightbox src={a.dataUrl} alt={a.name} title={a.name} onClose={closePreview} />
      ) : null}
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
  const problem = a.error || (a.status === "error" ? "Upload failed" : "");
  const problemColor = a.status === "error" ? "var(--state-danger)" : "var(--state-warning)";

  return (
    <div
      title={attachmentTitle(a)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 4px 3px 8px",
        background: "var(--attachment-chip-bg)",
        border: `1px solid ${problem ? problemColor : "var(--border-subtle)"}`,
        borderRadius: "var(--radius-sm, 6px)",
        fontSize: "var(--text-xs)",
        color: a.status === "error" ? "var(--state-danger)" : "var(--attachment-chip-fg)",
        maxWidth: problem ? 420 : 260,
        minWidth: 0,
      }}
    >
      {a.status === "uploading" ? (
        <LoaderCircle size={14} className="animate-spin shrink-0" aria-label={`Uploading ${a.name}`} />
      ) : problem ? (
        <TriangleAlert size={14} className="shrink-0" aria-hidden="true" style={{ color: problemColor }} />
      ) : (
        fileIcon(a.name, { size: 14, className: "composer-attachment-file-icon" })
      )}
      <span className="truncate" style={{ minWidth: 0 }}>{truncateFilename(a.name)}</span>
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
          onClick={() => retryComposerAttachment(a.id)}
          aria-label={`Retry ${a.name}`}
          title="Retry upload"
          className="shrink-0"
          style={fileChipActionStyle}
        >
          <RotateCcw size={13} />
        </button>
      ) : null}
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${a.name}`}
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
        <X size={13} />
      </button>
    </div>
  );
};

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
