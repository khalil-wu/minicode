import { X } from "lucide-react";
import { useAppStore } from "../stores";
import type { ComposerAttachment } from "../stores/types";

export const AttachmentStrip = () => {
  const attachments = useAppStore((s) => s.attachments);
  const removeAttachment = useAppStore((s) => s.removeAttachment);
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
          <ImageChip key={a.id} attachment={a} onRemove={() => removeAttachment(a.id)} />
        ) : (
          <FileChip key={a.id} attachment={a} onRemove={() => removeAttachment(a.id)} />
        ),
      )}
    </div>
  );
};

const ImageChip = ({ attachment: a, onRemove }: { attachment: ComposerAttachment; onRemove: () => void }) => (
  <div
    title={a.name}
    style={{
      position: "relative",
      width: 64,
      height: 64,
      borderRadius: "var(--radius-sm, 6px)",
      border: `1px solid ${a.status === "error" ? "var(--state-danger)" : "var(--border-subtle)"}`,
      overflow: "hidden",
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
        ...
      </div>
    )}
    <button
      onClick={onRemove}
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
  </div>
);

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

const FileChip = ({ attachment: a, onRemove }: { attachment: ComposerAttachment; onRemove: () => void }) => (
  <div
    title={attachmentTitle(a)}
    style={{
      display: "flex",
      alignItems: "center",
      gap: 6,
      padding: "4px 8px",
      background: "var(--surface-soft)",
      border: `1px solid ${a.status === "error" || a.error ? "var(--state-warning)" : "var(--border-subtle)"}`,
      borderRadius: "var(--radius-sm, 6px)",
      fontSize: "var(--text-xs)",
      color: a.status === "error" ? "var(--state-danger)" : "var(--text-secondary)",
      maxWidth: a.error ? 420 : 260,
      minWidth: 0,
    }}
  >
    <span className="shrink-0">{truncateFilename(a.name)}</span>
    <span className="shrink-0" style={{ color: "var(--text-muted)" }}>
      {(a.size / 1024).toFixed(1)} KB
    </span>
    <span className="shrink-0" style={{ color: statusColor(a), fontWeight: 600 }}>
      {attachmentStatusLabel(a)}
    </span>
    {a.error && (
      <span className="truncate" style={{ color: a.status === "error" ? "var(--state-danger)" : "var(--state-warning)", minWidth: 0 }}>
        {a.error}
      </span>
    )}
    <button
      onClick={onRemove}
      aria-label={`Remove ${a.name}`}
      className="shrink-0"
      style={{
        background: "transparent",
        color: "var(--text-muted)",
        border: 0,
        cursor: "pointer",
        padding: 0,
        display: "inline-flex",
        alignItems: "center",
      }}
    >
      <X size={13} />
    </button>
  </div>
);

const attachmentStatusLabel = (attachment: ComposerAttachment): string => {
  if (attachment.status === "error") return "failed";
  if (attachment.status === "uploading") return "uploading";
  if (attachment.error) return "warning";
  if (attachment.indexedChunks != null && attachment.indexedChunks > 0) {
    return `${attachment.indexedChunks} chunks`;
  }
  if (attachment.docId) return "text ready";
  if (attachment.artifactId) return "stored";
  return "ready";
};

const attachmentTitle = (attachment: ComposerAttachment): string => {
  if (attachment.error) return attachment.error;
  const bits = [attachment.name];
  if (attachment.docId) bits.push(`doc: ${attachment.docId}`);
  if (attachment.artifactId) bits.push(`artifact: ${attachment.artifactId}`);
  if (attachment.indexedChunks != null) bits.push(`indexed chunks: ${attachment.indexedChunks}`);
  return bits.join("\n");
};

const statusColor = (attachment: ComposerAttachment): string => {
  if (attachment.status === "error") return "var(--state-danger)";
  if (attachment.error) return "var(--state-warning)";
  if (attachment.status === "ready") return "var(--state-success)";
  return "var(--state-warning)";
};
