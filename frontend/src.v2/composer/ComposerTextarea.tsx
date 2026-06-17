import { Sparkles, X } from "lucide-react";
import { useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void | Promise<void>;
  menuOpen?: boolean;
  onDropFiles?: (files: File[]) => void;
  compact?: boolean;
  minimal?: boolean;
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

// Paste-to-attachment thresholds (Codex-style). Above either bound, a pasted
// text blob is converted into a `pasted-N.txt` attachment chip instead of
// flooding the textarea.
const PASTE_LINE_THRESHOLD = 10;
const PASTE_CHAR_THRESHOLD = 500;

// Session-scoped counter so pasted attachments read pasted-1.txt, pasted-2.txt…
let pastedTextCounter = 0;

const shouldDivertPaste = (text: string): boolean => {
  if (!text) return false;
  if (text.length > PASTE_CHAR_THRESHOLD) return true;
  const lineCount = text.split("\n").length;
  return lineCount > PASTE_LINE_THRESHOLD;
};

const buildPastedTextFile = (text: string): File => {
  pastedTextCounter += 1;
  return new File([text], `pasted-${pastedTextCounter}.txt`, { type: "text/plain" });
};

export const ComposerTextarea = ({
  value,
  onChange,
  onSubmit,
  menuOpen,
  onDropFiles,
  compact = false,
  minimal = false,
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
  // Single-row base height: minimal (Cowork home) is tightest, then compact
  // (code mode), then the default chat composer.
  const baseHeight = minimal ? 40 : compact ? 36 : MIN_HEIGHT;
  const lastHeightRef = useRef(baseHeight);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  // Allow external triggers (e.g. message edit/recall) to focus the textarea
  useEffect(() => {
    const handleFocus = () => ref.current?.focus();
    window.addEventListener("composer:focus", handleFocus);
    return () => window.removeEventListener("composer:focus", handleFocus);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const prevVal = lastValRef.current;
    lastValRef.current = value;

    if (!value) {
      el.style.height = `${baseHeight}px`;
      lastHeightRef.current = baseHeight;
      return;
    }

    if (value.length >= prevVal.length) {
      const sh = el.scrollHeight;
      const nextHeight = Math.min(MAX_HEIGHT, Math.max(baseHeight, sh));
      if (nextHeight !== lastHeightRef.current) {
        el.style.height = `${nextHeight}px`;
        lastHeightRef.current = nextHeight;
      }
    } else {
      el.style.height = "auto";
      const nextHeight = Math.min(MAX_HEIGHT, Math.max(baseHeight, el.scrollHeight));
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

    // File clipboard items take priority (images, dragged files, etc.).
    if (files.length > 0 && onDropFiles) {
      e.preventDefault();
      onDropFiles(files);
      return;
    }

    // Large plain-text pastes become a `pasted-N.txt` attachment chip instead
    // of flooding the textarea (Codex-style). Smaller pastes insert normally.
    if (onDropFiles) {
      const text = e.clipboardData.getData("text");
      if (shouldDivertPaste(text)) {
        e.preventDefault();
        onDropFiles([buildPastedTextFile(text)]);
      }
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

  const hasContextPrefix = Boolean(commandLabel || skillTokens.length > 0);
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
          // Ignore Enter while an IME composition is active (CJK input commits
          // with Enter); otherwise the half-composed text is sent. Mirrors the
          // isComposing guard in AskUserPrompt.
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          onSubmit();
        }
      }}
      placeholder={placeholder ?? "Write a message..."}
      autoFocus
      className="composer-textarea bg-transparent border-0 outline-0 resize-none overflow-y-hidden tracking-normal transition-colors duration-[140ms]"
      style={{
        width: "100%",
        flex: hasContextPrefix ? "0 0 auto" : undefined,
        minHeight: baseHeight,
        maxHeight: MAX_HEIGHT,
        color: "var(--text-primary)",
        fontFamily: "var(--font-ui)",
        fontSize: compact ? 14 : 15,
        lineHeight: compact ? 1.4 : 1.55,
        padding: commandLabel || skillTokens.length > 0
          ? (compact ? "5px 14px 7px" : "6px 14px 8px")
          : minimal ? "9px 16px" : compact ? "10px 14px 6px" : "8px 12px 6px",
      }}
    />
  );

  if (!hasContextPrefix) return textarea;

  return (
    <div className="composer-textarea-frame" style={textareaFrameStyle} data-command-mode={commandMode ? "true" : "false"}>
      <div className="composer-context-prefix-row" style={prefixRowStyle}>
        {commandLabel && (
          <button
            type="button"
            title="Clear command"
            onClick={onClearCommand}
            className="composer-prefix-token"
            style={commandPrefixStyle}
          >
            <span style={prefixGlyphStyle}>/</span>
            <span style={prefixNameStyle}>{commandLabel}</span>
            <X size={12} className="shrink-0 opacity-[0.62]" />
          </button>
        )}
        {skillTokens.map((skill) => (
          <button
            key={skill.name}
            type="button"
            title={skill.description || skill.name}
            onClick={() => onRemoveSkill?.(skill.name)}
            className="composer-prefix-token"
            style={skillPrefixStyle}
          >
            <Sparkles size={12} />
            <span style={prefixNameStyle}>{skill.name}</span>
            <X size={12} className="shrink-0 opacity-[0.62]" />
          </button>
        ))}
      </div>
      {textarea}
    </div>
  );
};

const textareaFrameStyle: React.CSSProperties = {
  display: "grid",
  gap: 0,
  minWidth: 0,
};

const prefixRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: 6,
  minHeight: 28,
  padding: "6px 10px 0",
};

const prefixBaseStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  maxWidth: 240,
  minHeight: 22,
  padding: "0 7px",
  borderRadius: "var(--radius-sm, 5px)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const commandPrefixStyle: React.CSSProperties = {
  ...prefixBaseStyle,
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--accent-primary) 7%, var(--surface-page))",
  color: "var(--accent-primary)",
};

const skillPrefixStyle: React.CSSProperties = {
  ...prefixBaseStyle,
  border: "1px solid color-mix(in oklch, var(--accent-primary) 24%, var(--border-subtle))",
  background: "transparent",
  color: "var(--accent-primary)",
};

const prefixGlyphStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontWeight: 800,
};

const prefixNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
