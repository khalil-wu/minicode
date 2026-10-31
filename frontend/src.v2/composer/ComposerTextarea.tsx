import { useI18n } from "../i18n";
import { X } from "../lib/icons";
import { BrandIcon } from "../components/BrandIcon";
import { useEffect, useLayoutEffect, useRef } from "react";
import { buildPastedTextFile, shouldAttachPastedText } from "./pastedText";
import { useAppStore } from "../stores";
import type { ComposerSelection } from "./inputSelection";
import { RichComposerEditor, type ComposerInputHandle } from "./RichComposerEditor";
import type { RichComposerDocument } from "./rich-composer-document";
import type { ComposerReferenceInsertion, InlineComposerReference } from "./composer-references";

interface Props {
  value: string;
  onChange: (v: string, selection?: ComposerSelection, document?: RichComposerDocument) => void;
  rich?: boolean;
  richDocument?: RichComposerDocument;
  documentKey?: string;
  references?: InlineComposerReference[];
  referenceInsertion?: ComposerReferenceInsertion;
  workspaceRoot?: string;
  onSelectionChange?: (v: string, selection: ComposerSelection) => void;
  selectionRequest?: ComposerSelection;
  onSubmit: () => void | Promise<void>;
  onAlternateSubmit?: () => void | Promise<void>;
  isResponseInProgress?: boolean;
  menuOpen?: boolean;
  onDropFiles?: (files: File[]) => void;
  minimal?: boolean;
  commandMode?: boolean;
  commandLabel?: string | null;
  onClearCommand?: () => void;
  skillTokens?: { name: string; displayName?: string; description?: string; icon?: string }[];
  onRemoveSkill?: (name: string) => void;
  onRemoveLastSkill?: () => void;
  placeholder?: string;
  onHistorySearch?: () => void;
  // ArrowUp/ArrowDown prompt-history recall (cc useArrowKeyHistory). Given the
  // current caret/direction, returns the prompt to fill, or null to do nothing.
  onRecallHistory?: (direction: "up" | "down") => string | { value: string; document?: RichComposerDocument } | null;
  // Escape while focused in the composer: interrupt the running turn (cc makes
  // Escape a global interrupt even while typing). Returns true if it acted.
  onEscape?: () => boolean;
  conversationId?: string;
  ariaLabel?: string;
}

export const ComposerTextarea = ({
  value,
  onChange,
  onSelectionChange,
  selectionRequest,
  onSubmit,
  onAlternateSubmit,
  isResponseInProgress = false,
  menuOpen,
  onDropFiles,
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
  conversationId,
  ariaLabel,
  rich = false,
  richDocument,
  documentKey,
  references,
  referenceInsertion,
  workspaceRoot,
}: Props) => {
  const { t: tr } = useI18n();
  const draftsReady = useAppStore(state => state.composerDraftsReady);
  const sendShortcut = useAppStore((state) => state.sendShortcut);
  const plainTextMode = useAppStore(state => state.composerPlainTextMode);
  const ref = useRef<ComposerInputHandle | null>(null);
  const composingRef = useRef(false);
  const focusedRef = useRef(true);
  const selectionMemory = useRef<ComposerSelection>({ start: value.length, end: value.length });
  const ownerRef = useRef(documentKey);
  if (ownerRef.current !== documentKey) {
    ownerRef.current = documentKey;
    selectionMemory.current = { start: value.length, end: value.length };
  }
  const rememberSelection = (next: string, selection: ComposerSelection) => {
    selectionMemory.current = selection;
    onSelectionChange?.(next, selection);
  };
  useLayoutEffect(() => {
    if (!selectionRequest) return;
    selectionMemory.current = selectionRequest;
    ref.current!.setSelectionRange(selectionRequest.start, selectionRequest.end);
    ref.current!.focus({ preventScroll: true });
  }, [selectionRequest]);
  useLayoutEffect(() => {
    const selection = selectionMemory.current;
    ref.current!.setSelectionRange(selection.start, selection.end);
    if (focusedRef.current) ref.current!.focus({ preventScroll: true });
  }, [rich]);

  // Allow external triggers (e.g. message edit/recall) to focus the textarea
  useEffect(() => {
    const handleFocus = () => ref.current?.focus();
    const eventName = conversationId ? `composer:focus:${conversationId}` : "composer:focus";
    window.addEventListener(eventName, handleFocus);
    return () => window.removeEventListener(eventName, handleFocus);
  }, [conversationId]);

  const handlePaste = (e: React.ClipboardEvent | ClipboardEvent) => {
    const items = e.clipboardData!.items;
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
      const text = e.clipboardData!.getData("text");
      const target = ref.current!;
      const selectionStart = target.selectionStart;
      const selectionEnd = target.selectionEnd;
      if (shouldAttachPastedText(value, text, selectionStart, selectionEnd)) {
        e.preventDefault();
        onDropFiles([buildPastedTextFile(text)]);
      }
    }
  };

  const handleDrop = (e: React.DragEvent | DragEvent) => {
    if (e.dataTransfer!.files && e.dataTransfer!.files.length > 0) {
      e.preventDefault();
      e.stopPropagation();
      const files = Array.from(e.dataTransfer!.files);
      onDropFiles?.(files);
    }
  };

  const handleKeyDown = (e: KeyboardEvent | React.KeyboardEvent) => {
    if (composingRef.current || ("nativeEvent" in e ? e.nativeEvent.isComposing : e.isComposing) || e.keyCode === 229) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r") {
      e.preventDefault();
      e.stopPropagation();
      onHistorySearch?.();
      return;
    }
    if (menuOpen && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey
      && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) {
      e.preventDefault();
      return;
    }
    if (menuOpen && e.key !== "Enter") return;
    // Escape interrupts the running turn even while the composer is focused
    // (cc: Escape is a global interrupt). Only acts when the turn is live;
    // otherwise fall through so normal Escape handling still applies.
    if (e.key === "Escape" && onEscape) {
      if (onEscape()) {
        e.preventDefault();
        return;
      }
    }
    // ArrowUp/ArrowDown prompt-history recall (cc useArrowKeyHistory). Only
    // when the caret is on the first/last line so arrows still navigate
    // multi-line text; skipped during IME composition.
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && onRecallHistory) {
      const el = ref.current!;
      const caret = el.selectionStart;
      const collapsed = caret === el.selectionEnd;
      const firstNewline = value.indexOf("\n");
      const onFirstLine = firstNewline === -1 || caret <= firstNewline;
      const onLastLine = caret >= value.lastIndexOf("\n") + 1;
      const eligible = e.key === "ArrowUp" ? onFirstLine : onLastLine;
      if (collapsed && eligible) {
        const recalled = onRecallHistory(e.key === "ArrowUp" ? "up" : "down");
        if (recalled !== null) {
          e.preventDefault();
              const text = typeof recalled === "string" ? recalled : recalled.value;
              onChange(text, { start: text.length, end: text.length }, typeof recalled === "string" ? undefined : recalled.document);
          return;
        }
      }
    }
    if (e.key === "Backspace" && ref.current!.selectionStart === 0 && ref.current!.selectionEnd === 0) {
      if (!rich && skillTokens.length > 0) {
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
    const modifier = e.ctrlKey || e.metaKey;
    if (e.key === "Enter" && modifier && e.repeat && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    const alternateFollowUp = sendShortcut === "enter" ? !e.shiftKey : e.shiftKey;
    if (e.key === "Enter" && modifier && !e.altKey && alternateFollowUp && isResponseInProgress && onAlternateSubmit) {
      e.preventDefault();
      e.stopPropagation();
      onAlternateSubmit();
      return;
    }
    const submitWithEnter = e.key === "Enter" && !e.shiftKey && !e.altKey
      && (modifier || sendShortcut === "enter" || (sendShortcut === "cmdIfMultiline" && !value.includes("\n")));
    if (submitWithEnter) {
      // Ignore Enter while an IME composition is active (CJK input commits
      // with Enter); otherwise the half-composed text is sent. Mirrors the
      // Keep IME composition from being mistaken for a submit shortcut.
      e.preventDefault();
      onSubmit();
    }
  };

  const hasContextPrefix = Boolean(commandLabel || !rich && skillTokens.length > 0);
  const textarea = (
    <textarea
      disabled={!draftsReady}
      ref={node => { ref.current = node; }}
      data-composer-input
      data-enter-behavior={sendShortcut === "mod-enter" ? "cmdAlways" : sendShortcut}
      aria-label={ariaLabel ?? tr("消息输入")}
      value={value}
      onChange={(e) => { const selection = { start: e.currentTarget.selectionStart, end: e.currentTarget.selectionEnd, composing: composingRef.current }; selectionMemory.current = selection; onChange(e.currentTarget.value, selection); }}
      onSelect={(e) => rememberSelection(e.currentTarget.value, { start: e.currentTarget.selectionStart, end: e.currentTarget.selectionEnd, composing: composingRef.current })}
      onFocus={() => { focusedRef.current = true; }}
      onBlur={() => { focusedRef.current = false; }}
      onCompositionStart={(e) => { composingRef.current = true; onSelectionChange?.(e.currentTarget.value, { start: e.currentTarget.selectionStart, end: e.currentTarget.selectionEnd, composing: true }); }}
      onCompositionEnd={(e) => { composingRef.current = false; onSelectionChange?.(e.currentTarget.value, { start: e.currentTarget.selectionStart, end: e.currentTarget.selectionEnd, composing: false }); }}
      onPaste={handlePaste}
      onDrop={handleDrop}
      onKeyDown={handleKeyDown}
      placeholder={placeholder ?? tr("随心输入")}
      autoFocus={focusedRef.current}
      className="composer-textarea bg-transparent border-0 outline-0 resize-none overflow-y-auto tracking-normal transition-colors duration-[140ms]"
      style={{
        width: "auto",
        minWidth: 0,
        flex: "1 1 140px",
        color: "var(--text-primary)",
        // Share the interface and transcript's mixed-language font stack.
        fontFamily: "var(--font-prose)",
        fontSize: "var(--text-md)",
        lineHeight: "var(--leading-relaxed)",
        fontWeight: "var(--fw-regular)",
        letterSpacing: 0,
        padding: 0,
      }}
    />
  );

  return (
    <div className="composer-textarea-frame" data-command-mode={commandMode ? "true" : "false"} data-minimal={minimal}>
      {hasContextPrefix && <div className="composer-context-prefix-row">
        {commandLabel && (
          <button
            type="button"
            title={tr("清除命令")}
            aria-label={tr("清除命令")}
            onClick={onClearCommand}
            className="composer-prefix-token"
            style={commandPrefixStyle}
          >
            <span style={prefixGlyphStyle}>/</span>
            <span style={prefixNameStyle}>{commandLabel}</span>
            <X size={14} className="shrink-0 opacity-[0.62]" />
          </button>
        )}
        {!rich && skillTokens.map((skill) => (
          <span
            key={skill.name}
            className="composer-skill-token"
          >
            <BrandIcon value={skill.displayName || skill.name} iconUrl={skill.icon} inferBrand={false} fallback="skill" size={16} />
            <span className="composer-skill-name" title={skill.description || skill.displayName || skill.name}>{skill.displayName || skill.name}</span>
            <button type="button" className="composer-skill-remove"
              title={tr("移除技能 {name}", { name: skill.displayName || skill.name })}
              aria-label={tr("移除技能 {name}", { name: skill.displayName || skill.name })}
              onClick={() => { onRemoveSkill?.(skill.name); ref.current?.focus(); }}>
              <X size={13} aria-hidden="true" />
            </button>
          </span>
        ))}
      </div>}
      {rich ? <RichComposerEditor key={documentKey} ref={ref} value={value} document={richDocument} disabled={!draftsReady} plainTextMode={plainTextMode}
        references={references} referenceInsertion={referenceInsertion} workspaceRoot={workspaceRoot} conversationId={conversationId}
        placeholder={placeholder ?? tr("随心输入")} ariaLabel={ariaLabel ?? tr("消息输入")}
        enterBehavior={sendShortcut === "mod-enter" ? "cmdAlways" : sendShortcut}
        initialSelection={selectionRequest ?? richDocument?.selection ?? selectionMemory.current} initialFocus={focusedRef.current}
        onFocusChange={focused => { focusedRef.current = focused; }}
        onChange={(next, selection, document) => { selectionMemory.current = selection; onChange(next, selection, document); }}
        onSelectionChange={rememberSelection}
        onKeyDown={handleKeyDown} onPaste={handlePaste} onDrop={handleDrop}
        onCompositionChange={composing => {
          composingRef.current = composing;
          onSelectionChange?.(value, { start: ref.current!.selectionStart, end: ref.current!.selectionEnd, composing });
        }} /> : textarea}
    </div>
  );
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
  fontWeight: "var(--fw-semibold)",
};

const commandPrefixStyle: React.CSSProperties = {
  ...prefixBaseStyle,
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--accent-primary) 7%, var(--surface-page))",
  color: "var(--accent-primary)",
};

const prefixGlyphStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontWeight: "var(--fw-bold)",
};

const prefixNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
