import { useI18n } from "../i18n";
import { X } from "lucide-react";
import { useAppStore } from "../stores";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { formatShortcut, SHORTCUT_DEFINITIONS } from "../lib/keyboard-shortcuts";

export const KeyboardShortcutsHelp = () => {
  const { t: tr } = useI18n();
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const toggleShortcutsHelp = useAppStore((s) => s.toggleShortcutsHelp);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const sendShortcut = useAppStore((s) => s.sendShortcut);
  const dialogRef = useFocusTrap(shortcutsHelpOpen);
  const shortcuts = [
    ...SHORTCUT_DEFINITIONS
      .filter((definition) => Boolean(shortcutBindings[definition.id]))
      .map((definition) => ({ keys: formatShortcut(shortcutBindings[definition.id]), label: tr(definition.label) })),
    ...(sendShortcut === "cmdIfMultiline" ? [
      { keys: "Enter", label: tr("发送单行消息") },
      { keys: "Ctrl/Cmd + Enter", label: tr("发送多行消息") },
      { keys: "Shift + Enter", label: tr("输入换行") },
    ] : [
      { keys: sendShortcut === "enter" ? "Enter" : "Ctrl/Cmd + Enter", label: tr("发送消息") },
      { keys: sendShortcut === "enter" ? "Shift + Enter" : "Enter", label: tr("输入换行") },
    ]),
    { keys: "/", label: tr("打开斜杠命令") },
    { keys: "@", label: tr("提及上下文") },
  ];

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
        aria-label={tr("快捷键")}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            toggleShortcutsHelp();
          }
        }}
        style={{
          width: "min(440px, 90vw)",
          maxHeight: "70vh",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-strong-overlay)",
          overflow: "auto",
          padding: 20,
          pointerEvents: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h2 style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)" }}>{tr("快捷键")}</h2>
          <button
            onClick={toggleShortcutsHelp}
            className="btn-ghost"
            aria-label={tr("关闭快捷键")}
            style={{ border: 0, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 4 }}
          >
            <X size={18} />
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 16px" }}>
          {shortcuts.map((s) => (
            <div key={`${s.keys}:${s.label}`} style={{ display: "contents" }}>
              <kbd className="mc-kbd" style={{ textAlign: "right" }}>
                {s.keys}
              </kbd>
              <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", padding: "3px 0" }}>
                {s.label}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
