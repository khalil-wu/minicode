import { Copy, FileText, Folder, Image, Paperclip, RotateCcw, Trash2, Wrench } from "lucide-react";
import { memo, useState } from "react";
import { getWebSocket } from "../../hooks/useWebSocket";
import { useAppStore } from "../../stores";
import type { ChatMessage, MessageContextRef } from "../../stores/types";

export const UserMessage = memo(({ message }: { message: ChatMessage }) => {
  const [copied, setCopied] = useState(false);
  const contextRefs = message.contextRefs ?? [];
  const attachmentRefs = message.attachmentRefs ?? [];
  const openEditorFile = useAppStore((s) => s.openEditorFile);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const deleteMessage = useAppStore((s) => s.deleteMessage);

  const copy = () => {
    navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  };

  const recall = async () => {
    const state = useAppStore.getState();
    const index = state.messages.findIndex((item) => item.id === message.id);
    const removeCount = index >= 0 ? state.messages.length - index : 1;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "Recall message",
      message: `Recall this prompt into the composer? This will remove ${removeCount} message${removeCount === 1 ? "" : "s"} from the current view.`,
      confirmLabel: "Recall",
      danger: removeCount > 1 || state.isStreaming,
    });
    if (!ok) return;
    if (useAppStore.getState().isStreaming) {
      useAppStore.getState().interrupt();
      getWebSocket()?.send({ type: "interrupt" });
    }
    recallMessage(message.id);
  };

  const deleteWithConfirm = async () => {
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "Delete message",
      message: "Delete this message from the current conversation view?",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    deleteMessage(message.id);
  };

  return (
    <div className="user-msg" style={{ display: "flex", justifyContent: "flex-end", maxWidth: "100%" }}>
      <div
        style={{
          maxWidth: "78%",
          display: "flex",
          flexDirection: "column",
          gap: 6,
          minWidth: 0,
          alignItems: "flex-end",
        }}
      >
        {contextRefs.length > 0 && (
          <div style={contextRowStyle}>
            {contextRefs.map((ref) => (
              <ContextChip
                key={`${ref.kind}:${ref.kind === "skill" ? ref.name : ref.path}`}
                refItem={ref}
                onOpenFile={(path, name) => openEditorFile(path, name)}
              />
            ))}
          </div>
        )}
        <div style={messageBodyRowStyle} data-testid="user-message-body-row">
          {attachmentRefs.map((attachment) => (
            <AttachmentChip key={`${attachment.artifactId || attachment.id}:${attachment.name}`} attachment={attachment} />
          ))}
          {message.content && <div style={contentStyle}>{message.content}</div>}
        </div>
        <div className="msg-actions" style={actionsStyle}>
          <button type="button" onClick={copy} title="Copy message" aria-label="Copy message" style={actionButtonStyle}>
            <Copy size={12} />
            {copied ? "Copied" : "Copy"}
          </button>
          <button type="button" onClick={recall} title="Recall this message" aria-label="Recall this message" style={actionButtonStyle}>
            <RotateCcw size={12} />
            Recall
          </button>
          <button type="button" onClick={deleteWithConfirm} title="Delete message" aria-label="Delete message" style={actionButtonStyle}>
            <Trash2 size={12} />
            Delete
          </button>
        </div>
      </div>
    </div>
  );
});

UserMessage.displayName = "UserMessage";

const ContextChip = ({
  refItem,
  onOpenFile,
}: {
  refItem: MessageContextRef;
  onOpenFile: (path: string, name: string) => void;
}) => {
  const label = refItem.kind === "skill" ? refItem.name : refItem.name || refItem.path;
  const title = refItem.kind === "skill" ? refItem.description || refItem.name : refItem.path;
  const Icon = refItem.kind === "folder" ? Folder : refItem.kind === "skill" ? Wrench : FileText;
  return (
    <button
      type="button"
      title={title}
      onClick={() => {
        if (refItem.kind === "file") onOpenFile(refItem.path, refItem.name);
      }}
      style={contextChipStyle(refItem.kind === "skill")}
    >
      <Icon size={12} />
      <span style={{ color: "var(--text-muted)" }}>{refItem.kind}</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
    </button>
  );
};

const AttachmentChip = ({ attachment }: { attachment: NonNullable<ChatMessage["attachmentRefs"]>[number] }) => {
  const Icon = attachment.kind === "image" ? Image : attachment.kind === "document" ? FileText : Paperclip;
  const sizeLabel = attachment.sizeBytes > 0 ? formatBytes(attachment.sizeBytes) : "";
  const chunkLabel = attachment.indexedChunks ? `${attachment.indexedChunks} chunks` : "";
  return (
    <span title={attachment.artifactId ? `${attachment.name}\nartifact: ${attachment.artifactId}` : attachment.name} style={attachmentChipStyle}>
      <Icon size={12} />
      <span style={{ color: "var(--text-muted)" }}>attached</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{attachment.name}</span>
      {(sizeLabel || chunkLabel) && (
        <span style={{ color: "var(--text-muted)", flexShrink: 0 }}>
          {[sizeLabel, chunkLabel].filter(Boolean).join(" · ")}
        </span>
      )}
    </span>
  );
};

const formatBytes = (bytes: number): string => {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

const contentStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-md)",
  lineHeight: 1.6,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  fontWeight: 500,
  padding: "13px 16px",
  background: "var(--surface-soft)",
  borderRadius: "14px",
  border: "1px solid transparent",
};

const contextRowStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 5,
  minWidth: 0,
  justifyContent: "flex-end",
};

const messageBodyRowStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  justifyContent: "flex-end",
  alignItems: "center",
  gap: 8,
  maxWidth: "100%",
  minWidth: 0,
};

const contextChipStyle = (skill: boolean): React.CSSProperties => ({
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  maxWidth: 260,
  padding: "0 7px",
  border: `1px solid ${skill ? "color-mix(in oklch, var(--accent-primary) 35%, var(--border-subtle))" : "var(--border-subtle)"}`,
  borderRadius: "var(--radius-sm, 4px)",
  background: skill ? "var(--accent-soft)" : "var(--surface-soft)",
  color: skill ? "var(--accent-primary)" : "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  minWidth: 0,
});

const attachmentChipStyle: React.CSSProperties = {
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  maxWidth: 300,
  padding: "0 7px",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 30%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 4px)",
  background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  minWidth: 0,
};

const actionsStyle: React.CSSProperties = {
  display: "flex",
  gap: 6,
  marginTop: 1,
  opacity: 0,
  transition: "opacity 150ms",
  alignItems: "center",
};

const actionButtonStyle: React.CSSProperties = {
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  background: "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "0 7px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};
