import { Sparkles, X } from "lucide-react";
import { useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void | Promise<void>;
  menuOpen?: boolean;
  onDropFiles?: (files: File[]) => void;
  compact?: boolean;
  commandMode?: boolean;
  commandLabel?: string | null;
  onClearCommand?: () => void;
  skillTokens?: { name: string; description?: string }[];
  onRemoveSkill?: (name: string) => void;
  onRemoveLastSkill?: () => void;
  placeholder?: string;
}

const MIN_HEIGHT = 44;
const MAX_HEIGHT = 260;

export const ComposerTextarea = ({
  value,
  onChange,
  onSubmit,
  menuOpen,
  onDropFiles,
  compact = false,
  commandMode = false,
  commandLabel,
  onClearCommand,
  skillTokens = [],
  onRemoveSkill,
  onRemoveLastSkill,
  placeholder,
}: Props) => {
  const ref = useRef<HTMLTextAreaElement>(null);
  const lastValRef = useRef(value);
  const lastHeightRef = useRef(MIN_HEIGHT);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const prevVal = lastValRef.current;
    lastValRef.current = value;

    if (!value) {
      const minHeight = compact ? 36 : MIN_HEIGHT;
      el.style.height = `${minHeight}px`;
      lastHeightRef.current = minHeight;
      return;
    }

    if (value.length >= prevVal.length) {
      const sh = el.scrollHeight;
      const nextHeight = Math.min(MAX_HEIGHT, Math.max(compact ? 36 : MIN_HEIGHT, sh));
      if (nextHeight !== lastHeightRef.current) {
        el.style.height = `${nextHeight}px`;
        lastHeightRef.current = nextHeight;
      }
    } else {
      el.style.height = "auto";
      const nextHeight = Math.min(MAX_HEIGHT, Math.max(compact ? 36 : MIN_HEIGHT, el.scrollHeight));
      if (nextHeight !== lastHeightRef.current) {
        el.style.height = `${nextHeight}px`;
        lastHeightRef.current = nextHeight;
      }
    }
  }, [value]);

  const handlePaste = (e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }

    if (files.length > 0 && onDropFiles) {
      e.preventDefault();
      onDropFiles(files);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      e.preventDefault();
      e.stopPropagation();
      const files = Array.from(e.dataTransfer.files);
      onDropFiles?.(files);
    }
  };

  const textarea = (
    <textarea
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onPaste={handlePaste}
      onDrop={handleDrop}
      onKeyDown={(e) => {
        if (menuOpen && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) {
          e.preventDefault();
          return;
        }
        if (menuOpen) return;
        if ((e.key === "Backspace" || e.key === "Delete") && !value) {
          if (skillTokens.length > 0) {
            e.preventDefault();
            onRemoveLastSkill?.();
            return;
          }
          if (commandLabel) {
            e.preventDefault();
            onClearCommand?.();
            return;
          }
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          onSubmit();
        }
      }}
      placeholder={placeholder ?? "Write a message..."}
      autoFocus
      style={{
        width: commandLabel || skillTokens.length > 0 ? "auto" : "100%",
        flex: commandLabel || skillTokens.length > 0 ? "1 1 180px" : undefined,
        minHeight: compact ? 36 : MIN_HEIGHT,
        maxHeight: MAX_HEIGHT,
        overflowY: "hidden",
        resize: "none",
        background: "transparent",
        border: 0,
        outline: 0,
        color: "var(--text-primary)",
        fontFamily: "var(--font-ui)",
        fontSize: compact ? 14 : 15,
        lineHeight: compact ? 1.4 : 1.5,
        padding: commandLabel ? (compact ? "10px 14px 6px 0" : "12px 16px 10px 0") : compact ? "10px 14px 6px" : "12px 16px 10px",
        letterSpacing: 0,
        transition: "color 140ms ease",
      }}
    />
  );

  if (!commandLabel && skillTokens.length === 0) return textarea;

  return (
    <div style={commandLineStyle}>
      {commandLabel && <span style={commandPrefixStyle}>{commandLabel}</span>}
      {skillTokens.map((skill) => (
        <button
          key={skill.name}
          type="button"
          title={skill.description || skill.name}
          onClick={() => onRemoveSkill?.(skill.name)}
          style={skillTokenStyle}
        >
          <Sparkles size={12} />
          <span style={skillTokenNameStyle}>{skill.name}</span>
          <X size={12} style={{ flexShrink: 0, opacity: 0.72 }} />
        </button>
      ))}
      {textarea}
    </div>
  );
};

const commandLineStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 7,
  minWidth: 0,
  flexWrap: "wrap",
};

const commandPrefixStyle: React.CSSProperties = {
  flexShrink: 0,
  margin: "12px 0 0 14px",
  color: "var(--command-accent-strong, var(--state-info))",
  fontWeight: 800,
  fontSize: 15,
  lineHeight: 1.5,
};

const skillTokenStyle: React.CSSProperties = {
  flexShrink: 0,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  maxWidth: 190,
  height: 24,
  marginTop: 11,
  padding: "0 7px",
  border: "1px solid color-mix(in oklch, var(--command-accent, var(--state-info)) 35%, transparent)",
  borderRadius: "var(--radius-full)",
  background: "color-mix(in oklch, var(--command-accent, var(--state-info)) 9%, transparent)",
  color: "var(--command-accent-strong, var(--state-info))",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};

const skillTokenNameStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
