import { Copy, Pencil, RotateCcw, Trash2 } from "lucide-react";
import { useCallback, useState } from "react";
import type React from "react";
import type { UserMessageCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import "./cells.css";

export function UserMessageCell({ cell }: { cell: UserMessageCellState }) {
  const [copied, setCopied] = useState(false);
  const recallMessage = useAppStore((s) => s.recallMessage);
  const deleteMessage = useAppStore((s) => s.deleteMessage);

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
    if (useAppStore.getState().isStreaming) {
      useAppStore.getState().interrupt();
    }
    recallMessage(cell.id);
  }, [cell.id, recallMessage]);

  const deleteWithConfirm = useCallback(async () => {
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "Delete message",
      message: "Remove this message from the current conversation view?",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    deleteMessage(cell.id);
  }, [cell.id, deleteMessage]);

  const onEdit = useCallback(() => {
    recallMessage(cell.id);
    window.dispatchEvent(new CustomEvent("composer:focus"));
  }, [cell.id, recallMessage]);

  return (
    <div className="user-cell-wrap">
      <div className="edit-bubble-wrap" style={editBubbleWrapperStyle}>
        <button
          type="button"
          onClick={onEdit}
          title="Edit and resend"
          aria-label="Edit and resend"
          style={editButtonStyle}
        >
          <Pencil size={11} />
        </button>
        <div className="user-cell-bubble md-prose">
          <div style={userMsgContentStyle}>{cell.content}</div>
          {cell.attachments && cell.attachments.length > 0 && (
            <div style={attachmentsStyle}>
              {cell.attachments.map((attachment) => (
                <span key={attachment.name} style={attachmentChipStyle}>
                  {attachment.name}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="user-cell-actions">
        <button type="button" onClick={copy} title={copied ? "Copied" : "Copy message"} aria-label={copied ? "Copied" : "Copy message"} className="cell-action-btn">
          <Copy size={12} />
        </button>
        <button type="button" onClick={recall} title="Recall to composer" aria-label="Recall to composer" className="cell-action-btn">
          <RotateCcw size={12} />
        </button>
        <button type="button" onClick={deleteWithConfirm} title="Delete message" aria-label="Delete message" className="cell-action-btn">
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  );
}

const userMsgContentStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const attachmentsStyle: React.CSSProperties = {
  display: "flex",
  gap: 6,
  marginTop: 8,
  flexWrap: "wrap",
};

const attachmentChipStyle: React.CSSProperties = {
  padding: "2px 8px",
  borderRadius: "var(--radius-sm)",
  background: "var(--surface-soft)",
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};

const editBubbleWrapperStyle: React.CSSProperties = {
  position: "relative",
  width: "fit-content",
  maxWidth: "100%",
};

const editButtonStyle: React.CSSProperties = {
  position: "absolute",
  top: 2,
  right: 2,
  width: 24,
  height: 24,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm)",
  cursor: "pointer",
  color: "var(--text-secondary)",
  padding: 0,
  opacity: 0,
  transition: "opacity 150ms ease",
  zIndex: 1,
};
