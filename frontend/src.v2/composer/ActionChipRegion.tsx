import { FileText, Folder, X } from "lucide-react";
import { useAppStore } from "../stores";

export const ContextChipRegion = () => {
  const actionChip = useAppStore((s) => s.actionChip);
  const mentionResults = useAppStore((s) => s.mentionResults);
  const selectedMentions = useAppStore((s) => s.selectedMentions);
  const selectedSkills = useAppStore((s) => s.selectedSkills);
  const attachments = useAppStore((s) => s.attachments);
  const setMentionResults = useAppStore((s) => s.setMentionResults);
  const addSelectedMention = useAppStore((s) => s.addSelectedMention);
  const removeSelectedMention = useAppStore((s) => s.removeSelectedMention);
  const clearSelectedMentions = useAppStore((s) => s.clearSelectedMentions);
  const clearSelectedSkills = useAppStore((s) => s.clearSelectedSkills);
  const openEditorFile = useAppStore((s) => s.openEditorFile);
  const visibleActionChip = actionChip && !actionChip.label.toLowerCase().startsWith("skill selected")
    ? actionChip
    : null;
  if (!visibleActionChip && mentionResults.length === 0 && selectedMentions.length === 0 && selectedSkills.length === 0) return null;
  return (
    <div style={tokenRowStyle}>
      {visibleActionChip && (
        <span style={tokenStyle}>
          <span style={tokenNameStyle}>{visibleActionChip.label.startsWith("/") ? visibleActionChip.label : `/${visibleActionChip.label}`}</span>
          {visibleActionChip.description && <span style={tokenDetailStyle}>{visibleActionChip.description}</span>}
        </span>
      )}
      {selectedMentions.map((item) => (
        <button
          key={item.path}
          type="button"
          title={item.path}
          onClick={() => item.kind === "file" && openEditorFile(item.path, item.name)}
          style={tokenButtonStyle}
        >
          {item.kind === "folder" ? <Folder size={12} /> : <FileText size={12} />}
          <span style={tokenNameStyle}>@{item.name}</span>
          <span
            onClick={(event) => {
              event.stopPropagation();
              removeSelectedMention(item.path);
            }}
            style={tokenRemoveStyle}
          >
            <X size={12} />
          </span>
        </button>
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
              <span style={tokenNameStyle}>@{item.name}</span>
            </button>
          ))}
          <button onClick={() => setMentionResults([])} aria-label="Clear context mentions" style={dismissStyle}>
            <X size={13} />
          </button>
        </>
      )}
      {(selectedMentions.length > 1 || selectedSkills.length > 1) && (
        <button
          type="button"
          onClick={() => {
            clearSelectedMentions();
            clearSelectedSkills();
          }}
          style={clearAllStyle}
        >
          Clear
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
  borderRadius: "var(--radius-full)",
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
  borderRadius: "var(--radius-full)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const searchTokenStyle: React.CSSProperties = {
  ...tokenButtonStyle,
  background: "var(--surface-soft)",
  borderColor: "var(--border-subtle)",
};

const tokenNameStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-mono)",
  fontWeight: 650,
};

const tokenDetailStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
};

const tokenRemoveStyle: React.CSSProperties = {
  display: "inline-flex",
  color: "var(--text-muted)",
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
