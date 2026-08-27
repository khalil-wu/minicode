import { Blocks, Folder, MessageSquareText, X } from "lucide-react";
import { fileIcon } from "../shell/fileTreeHelpers";
import { useAppStore } from "../stores";

export const ContextChipRegion = () => {
  const actionChip = useAppStore((s) => s.actionChip);
  const mentionResults = useAppStore((s) => s.mentionResults);
  const selectedMentions = useAppStore((s) => s.selectedMentions);
  const setMentionResults = useAppStore((s) => s.setMentionResults);
  const addSelectedMention = useAppStore((s) => s.addSelectedMention);
  const removeSelectedMention = useAppStore((s) => s.removeSelectedMention);
  const clearSelectedMentions = useAppStore((s) => s.clearSelectedMentions);
  const openEditorFile = useAppStore((s) => s.openEditorFile);
  const visibleActionChip = actionChip && !actionChip.label.toLowerCase().startsWith("skill selected")
    ? actionChip
    : null;
  if (!visibleActionChip && mentionResults.length === 0 && selectedMentions.length === 0) return null;
  return (
    <div style={tokenRowStyle}>
      {visibleActionChip && (
        <span style={tokenStyle}>
          <span style={tokenNameStyle}>{visibleActionChip.label.startsWith("/") ? visibleActionChip.label : `/${visibleActionChip.label}`}</span>
          {visibleActionChip.description && <span style={tokenDetailStyle}>{visibleActionChip.description}</span>}
        </span>
      )}
      {selectedMentions.map((item) => (
        <span key={item.path} title={item.path} style={mentionTokenStyle}>
          {item.kind === "file" ? (
            <button
              type="button"
              aria-label={`打开 ${item.name}`}
              onClick={() => openEditorFile(item.path, item.name)}
              style={mentionLabelButtonStyle}
            >
              {fileIcon(item.name || item.path || "file", { size: 12, className: "composer-context-icon-svg" })}
              <span style={mentionNameStyle}>@{item.name}</span>
            </button>
          ) : item.kind === "browser_annotation" ? (
            <span style={mentionLabelStyle}>
              <MessageSquareText size={14} />
              <span style={mentionNameStyle}>@{item.name}</span>
            </span>
          ) : item.kind === "plugin" ? (
            <span style={mentionLabelStyle}>
              <Blocks size={14} />
              <span style={mentionNameStyle}>@{item.name}</span>
            </span>
          ) : (
            <span style={mentionLabelStyle}>
              <Folder size={14} />
              <span style={mentionNameStyle}>@{item.name}</span>
            </span>
          )}
          <button
            type="button"
            aria-label={`从上下文中移除 ${item.name}`}
            onClick={() => removeSelectedMention(item.path)}
            style={tokenRemoveStyle}
          >
            <X size={14} />
          </button>
        </span>
      ))}
      {mentionResults.length > 0 && (
        <>
          {mentionResults.slice(0, 6).map((item) => (
            <button
              key={item.path}
              type="button"
              title={item.path}
              onClick={() => {
                addSelectedMention(item);
                setMentionResults([]);
                if (item.kind === "file") openEditorFile(item.path, item.name);
              }}
              style={searchTokenStyle}
            >
              <span style={mentionNameStyle}>@{item.name}</span>
            </button>
          ))}
          <button type="button" onClick={() => setMentionResults([])} aria-label="清空上下文引用" style={dismissStyle}>
            <X size={14} />
          </button>
        </>
      )}
      {selectedMentions.length > 1 && (
        <button
          type="button"
          onClick={() => {
            clearSelectedMentions();
          }}
          style={clearAllStyle}
          aria-label="清空上下文引用"
        >
          清空
        </button>
      )}
    </div>
  );
};

const tokenRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  flexWrap: "wrap",
  minWidth: 0,
  padding: "8px 12px 0",
};

const tokenStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  maxWidth: "100%",
  minHeight: 24,
  padding: "0 7px",
  background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 32%, transparent)",
  borderRadius: "var(--radius-sm, 6px)",
  fontSize: "var(--text-xs)",
  color: "var(--accent-primary)",
};

const tokenButtonStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
  maxWidth: 220,
  height: 24,
  padding: "0 7px",
  background: "color-mix(in oklch, var(--accent-primary) 10%, transparent)",
  color: "var(--accent-primary)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const searchTokenStyle: React.CSSProperties = {
  ...tokenButtonStyle,
  background: "var(--surface-soft)",
  borderColor: "var(--border-subtle)",
};

const mentionTokenStyle: React.CSSProperties = {
  ...tokenButtonStyle,
  padding: "0 3px 0 7px",
  cursor: "default",
};

const mentionLabelStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
};

const mentionLabelButtonStyle: React.CSSProperties = {
  ...mentionLabelStyle,
  padding: 0,
  border: 0,
  background: "transparent",
  color: "inherit",
  cursor: "pointer",
  font: "inherit",
};

const tokenNameStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-mono)",
  fontWeight: "var(--fw-semibold)",
};

const mentionNameStyle: React.CSSProperties = {
  ...tokenNameStyle,
  fontFamily: "var(--font-ui)",
  fontWeight: "var(--fw-semibold)",
};

const tokenDetailStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
};

const tokenRemoveStyle: React.CSSProperties = {
  width: 20,
  height: 20,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  border: 0,
  borderRadius: "50%",
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
};

const dismissStyle: React.CSSProperties = {
  background: "transparent",
  color: "var(--text-muted)",
  border: 0,
  cursor: "pointer",
  padding: "0 4px",
  display: "inline-flex",
};

const clearAllStyle: React.CSSProperties = {
  height: 24,
  padding: "0 7px",
  border: 0,
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};
