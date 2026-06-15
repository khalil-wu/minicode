import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useAppStore } from "../stores";

const SHORTCUTS = [
  { keys: "Ctrl/Cmd + K", action: "Command Palette" },
  { keys: "Ctrl/Cmd + ,", action: "Settings" },
  { keys: "Ctrl/Cmd + /", action: "Keyboard Shortcuts" },
  { keys: "Ctrl/Cmd + +", action: "Increase text scale" },
  { keys: "Ctrl/Cmd + -", action: "Decrease text scale" },
  { keys: "Ctrl/Cmd + 0", action: "Reset text scale" },
  { keys: "Ctrl/Cmd + J", action: "Open terminal stack" },
  { keys: "Ctrl/Cmd + B", action: "Toggle left sidebar" },
  { keys: "Enter", action: "Send message" },
  { keys: "Shift + Enter", action: "New line in composer" },
  { keys: "/", action: "Open slash commands" },
  { keys: "@", action: "Open mentions" },
];

const getFocusable = (root: HTMLElement | null): HTMLElement[] => {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("disabled") && element.offsetParent !== null);
};

export const KeyboardShortcutsHelp = () => {
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const toggleShortcutsHelp = useAppStore((s) => s.toggleShortcutsHelp);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!shortcutsHelpOpen) return;
    const previousActive = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusFirst = () => {
      const focusable = getFocusable(dialogRef.current);
      (focusable[0] ?? dialogRef.current)?.focus();
    };
    window.setTimeout(focusFirst, 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        toggleShortcutsHelp();
      } else if (event.key === "Tab") {
        const focusable = getFocusable(dialogRef.current);
        if (focusable.length === 0) {
          event.preventDefault();
          dialogRef.current?.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      previousActive?.focus();
    };
  }, [shortcutsHelpOpen, toggleShortcutsHelp]);

  if (!shortcutsHelpOpen) return null;

  return (
    <div
      className="overlay-backdrop"
      onClick={toggleShortcutsHelp}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: "var(--z-modal)",
        pointerEvents: "auto",
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Keyboard shortcuts"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(440px, 90vw)",
          maxHeight: "70vh",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          overflow: "auto",
          padding: 20,
          pointerEvents: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h2 style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>Keyboard Shortcuts</h2>
          <button
            onClick={toggleShortcutsHelp}
            className="btn-ghost"
            style={{ border: 0, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 4 }}
          >
            <X size={18} />
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 16px" }}>
          {SHORTCUTS.map((s) => (
            <div key={s.keys} style={{ display: "contents" }}>
              <kbd
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                  background: "var(--surface-soft)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: 4,
                  padding: "3px 8px",
                  color: "var(--text-primary)",
                  textAlign: "right",
                }}
              >
                {s.keys}
              </kbd>
              <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", padding: "3px 0" }}>
                {s.action}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
