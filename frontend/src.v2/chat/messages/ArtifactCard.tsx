import { Braces, Code2, FileText, GitCompare, Image, Paperclip } from "lucide-react";
import { useAppStore } from "../../stores";
import { getWebSocket } from "../../hooks/useWebSocket";

interface ArtifactPreview {
  artifactId: string;
  kind: "file" | "diff" | "image" | "json" | "code" | "text";
  summary: string;
  bytes?: number;
  mediaType?: string;
  url?: string;
}

const KIND_ICON = {
  file: FileText,
  diff: GitCompare,
  image: Image,
  json: Braces,
  code: Code2,
  text: FileText,
};

export const ArtifactCard = ({ artifact }: { artifact: ArtifactPreview }) => {
  const openArtifact = () => {
    const store = useAppStore.getState();
    store.setPreviewArtifact(null);
    store.addPanel({
      id: `artifact-${artifact.artifactId}`,
      kind: "preview",
      label: artifact.summary.slice(0, 24),
    });
    getWebSocket()?.send({ type: "read_artifact", artifact_id: artifact.artifactId });
  };

  const imageUrl = artifact.kind === "image" && artifact.url ? artifact.url : "";

  const sizeLabel = artifact.bytes
    ? artifact.bytes > 1024
      ? `${(artifact.bytes / 1024).toFixed(1)} KB`
      : `${artifact.bytes} B`
    : null;
  const Icon = KIND_ICON[artifact.kind] ?? Paperclip;

  return (
    <button
      onClick={openArtifact}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 12px",
        background: "var(--surface-soft)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 6px)",
        cursor: "pointer",
        textAlign: "left",
        width: "100%",
        color: "var(--text-primary)",
        fontSize: "var(--text-sm)",
      }}
    >
      {imageUrl && (
        <img
          src={imageUrl}
          alt={artifact.summary}
          style={{
            width: 48,
            height: 48,
            objectFit: "cover",
            borderRadius: "var(--radius-sm, 6px)",
            border: "1px solid var(--border-subtle)",
            background: "var(--surface-page)",
            flexShrink: 0,
          }}
        />
      )}
      <span style={{ flexShrink: 0, display: "inline-flex", color: "var(--text-muted)" }}>
        <Icon size={16} />
      </span>
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {artifact.summary}
      </span>
      {sizeLabel && (
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", flexShrink: 0 }}>
          {sizeLabel}
        </span>
      )}
      <span
        style={{
          fontSize: "var(--text-xs)",
          color: "var(--accent-primary)",
          background: "var(--surface-active)",
          padding: "2px 6px",
          borderRadius: 4,
          flexShrink: 0,
        }}
      >
        {artifact.kind}
      </span>
    </button>
  );
};
