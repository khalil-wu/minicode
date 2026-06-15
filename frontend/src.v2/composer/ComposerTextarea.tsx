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
  const baseHeight = minimal ? 40 : compact ? 36 : 54;
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
      className="bg-transparent border-0 outline-0 resize-none overflow-y-hidden tracking-normal transition-colors duration-[140ms]"
      style={{
        width: commandLabel || skillTokens.length > 0 ? "auto" : "100%",
        flex: commandLabel || skillTokens.length > 0 ? "1 1 180px" : undefined,
        minHeight: baseHeight,
        maxHeight: MAX_HEIGHT,
        color: "var(--text-primary)",
        fontFamily: "var(--font-ui)",
        fontSize: compact ? 14 : 16,
        lineHeight: compact ? 1.4 : 1.55,
        padding: commandLabel
          ? (compact ? "10px 14px 6px 0" : "12px 16px 10px 0")
          : minimal ? "9px 16px" : compact ? "10px 14px 6px" : "13px 14px 8px",
      }}
    />
  );

  if (!commandLabel && skillTokens.length === 0) return textarea;

  return (
    <div className="flex items-start flex-wrap min-w-0" style={{ gap: 7 }}>
      {commandLabel && (
        <span className="shrink-0 mt-3 ml-[14px] font-extrabold text-[15px] leading-[1.5]" style={{ color: "var(--command-accent-strong, var(--state-info))" }}>
          {commandLabel}
        </span>
      )}
      {skillTokens.map((skill) => (
        <button
          key={skill.name}
          type="button"
          title={skill.description || skill.name}
          onClick={() => onRemoveSkill?.(skill.name)}
          className="shrink-0 inline-flex items-center max-w-[190px] h-6 mt-[11px] px-[7px] cursor-pointer font-bold"
          style={{
            gap: 5,
            border: "1px solid color-mix(in oklch, var(--command-accent, var(--state-info)) 35%, transparent)",
            borderRadius: "var(--radius-full)",
            background: "color-mix(in oklch, var(--command-accent, var(--state-info)) 9%, transparent)",
            color: "var(--command-accent-strong, var(--state-info))",
            fontSize: "var(--text-xs)",
          }}
        >
          <Sparkles size={12} />
          <span className="overflow-hidden text-ellipsis whitespace-nowrap">{skill.name}</span>
          <X size={12} className="shrink-0 opacity-[0.72]" />
        </button>
      ))}
      {textarea}
    </div>
  );
};
