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

export const KeyboardShortcutsHelp = () => {
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const toggleShortcutsHelp = useAppStore((s) => s.toggleShortcutsHelp);

  if (!shortcutsHelpOpen) return null;

  return (
    <div
      onClick={toggleShortcutsHelp}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
    >
      <div
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
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h2 style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>Keyboard Shortcuts</h2>
          <button
            onClick={toggleShortcutsHelp}
            style={{ background: "transparent", border: 0, color: "var(--text-muted)", fontSize: 20, cursor: "pointer" }}
          >
            x
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
