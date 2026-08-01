import { Sparkles, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { buildPastedTextFile, shouldAttachPastedText } from "./pastedText";

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
  onHistorySearch?: () => void;
  // ArrowUp/ArrowDown prompt-history recall (cc useArrowKeyHistory). Given the
  // current caret/direction, returns the prompt to fill, or null to do nothing.
  onRecallHistory?: (direction: "up" | "down") => string | null;
  // Escape while focused in the composer: interrupt the running turn (cc makes
  // Escape a global interrupt even while typing). Returns true if it acted.
  onEscape?: () => boolean;
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
  minimal = false,
  commandMode = false,
  commandLabel,
  onClearCommand,
  skillTokens = [],
  onRemoveSkill,
  onRemoveLastSkill,
  placeholder,
  onHistorySearch,
  onRecallHistory,
  onEscape,
}: Props) => {
  const ref = useRef<HTMLTextAreaElement>(null);
  const lastValRef = useRef(value);
  const baseHeight = minimal ? 40 : MIN_HEIGHT;
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

    // Keep ordinary long-form text editable. Only divert a paste when the
    // resulting draft would exceed the composer's safe editing budget.
    if (onDropFiles) {
      const text = e.clipboardData.getData("text");
      const target = e.currentTarget as HTMLTextAreaElement;
      const selectionStart = target.selectionStart ?? value.length;
      const selectionEnd = target.selectionEnd ?? selectionStart;
      if (shouldAttachPastedText(value, text, selectionStart, selectionEnd)) {
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
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r") {
          e.preventDefault();
          e.stopPropagation();
          onHistorySearch?.();
          return;
        }
        if (menuOpen && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) {
          e.preventDefault();
          return;
        }
        if (menuOpen) return;
        // Escape interrupts the running turn even while the composer is focused
        // (cc: Escape is a global interrupt). Only acts when the turn is live;
        // otherwise fall through so normal Escape handling still applies.
        if (e.key === "Escape" && onEscape) {
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          if (onEscape()) {
            e.preventDefault();
            return;
          }
        }
        // ArrowUp/ArrowDown prompt-history recall (cc useArrowKeyHistory). Only
        // when the caret is on the first/last line so arrows still navigate
        // multi-line text; skipped during IME composition.
        if ((e.key === "ArrowUp" || e.key === "ArrowDown") && onRecallHistory) {
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          const el = e.currentTarget;
          const caret = el.selectionStart ?? 0;
          const collapsed = caret === (el.selectionEnd ?? caret);
          const firstNewline = value.indexOf("\n");
          const onFirstLine = firstNewline === -1 || caret <= firstNewline;
          const onLastLine = caret >= value.lastIndexOf("\n") + 1;
          const eligible = e.key === "ArrowUp" ? onFirstLine : onLastLine;
          if (collapsed && eligible) {
            const recalled = onRecallHistory(e.key === "ArrowUp" ? "up" : "down");
            if (recalled !== null) {
              e.preventDefault();
              onChange(recalled);
              return;
            }
          }
        }
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
          // Keep IME composition from being mistaken for a submit shortcut.
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          onSubmit();
        }
      }}
      placeholder={placeholder ?? "随心输入"}
      autoFocus
      className="composer-textarea bg-transparent border-0 outline-0 resize-none overflow-y-auto tracking-normal transition-colors duration-[140ms]"
      style={{
        width: "100%",
        flex: hasContextPrefix ? "0 0 auto" : undefined,
        minHeight: baseHeight,
        maxHeight: MAX_HEIGHT,
        color: "var(--text-primary)",
        // Use one CJK-capable face for both Latin and Chinese glyphs. Switching
        // from Manrope to a fallback font only after the first Chinese
        // character made the input look as if it zoomed while typing.
        fontFamily: "var(--font-prose)",
        fontSize: "var(--text-md)",
        lineHeight: "var(--leading-relaxed)",
        fontWeight: 400,
        letterSpacing: 0,
        padding: commandLabel || skillTokens.length > 0
          ? (compact ? "7px 12px 6px" : "6px 14px 8px")
          : minimal ? "9px 16px" : "8px 12px 6px",
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
            <X size={14} className="shrink-0 opacity-[0.62]" />
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
            <Sparkles size={14} />
            <span style={prefixNameStyle}>{skill.name}</span>
            <X size={14} className="shrink-0 opacity-[0.62]" />
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
