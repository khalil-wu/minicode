import { useEffect, useMemo, useState } from "react";
import { AtSign, Command, Keyboard, PanelLeft, Pencil, RotateCcw, Search, Send, Slash, Terminal, Trash2 } from "lucide-react";
import { findShortcutConflict, formatShortcut, shortcutFromEvent, SHORTCUT_DEFINITIONS, type ShortcutActionId } from "../lib/keyboard-shortcuts";
import { useAppStore } from "../stores";

const shortcutIcon = (action: string) => {
  if (action.includes("Command")) return <Command />;
  if (action.includes("terminal")) return <Terminal />;
  if (action.includes("sidebar")) return <PanelLeft />;
  if (action.includes("message") || action.includes("line")) return <Send />;
  if (action.includes("mentions")) return <AtSign />;
  if (action.includes("slash")) return <Slash />;
  return <Keyboard />;
};

export const ShortcutsTab = () => {
  const [query, setQuery] = useState("");
  const [recording, setRecording] = useState<ShortcutActionId | null>(null);
  const [error, setError] = useState("");
  const bindings = useAppStore((state) => state.shortcutBindings);
  const setShortcutBinding = useAppStore((state) => state.setShortcutBinding);
  const resetShortcutBindings = useAppStore((state) => state.resetShortcutBindings);
  const normalizedQuery = query.trim().toLowerCase();
  const shortcuts = useMemo(() => (
    normalizedQuery
      ? SHORTCUT_DEFINITIONS.filter((shortcut) => `${shortcut.label} ${shortcut.action} ${formatShortcut(bindings[shortcut.id])}`.toLowerCase().includes(normalizedQuery))
      : SHORTCUT_DEFINITIONS
  ), [bindings, normalizedQuery]);

  useEffect(() => {
    if (!recording) return;
    const capture = (event: KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") {
        setRecording(null);
        setError("");
        return;
      }
      if (event.key === "Backspace" || event.key === "Delete") {
        setShortcutBinding(recording, "");
        setRecording(null);
        setError("");
        return;
      }
      const binding = shortcutFromEvent(event);
      if (!binding) return;
      if (!binding.includes("Mod") && !binding.includes("Alt") && !binding.startsWith("F")) {
        setError("快捷键需要 Ctrl/Cmd、Alt 或功能键。");
        return;
      }
      const conflict = findShortcutConflict(bindings, recording, binding);
      if (conflict) {
        setError(`已用于“${conflict.label}”。`);
        return;
      }
      setShortcutBinding(recording, binding);
      setRecording(null);
      setError("");
    };
    window.addEventListener("keydown", capture, true);
    return () => window.removeEventListener("keydown", capture, true);
  }, [bindings, recording, setShortcutBinding]);

  return (
    <section className="settings-group">
      <div className="settings-section-heading settings-section-heading-row">
        <div>
          <h3 className="settings-group-title">快捷键</h3>
          <p className="settings-section-description">当前运行时绑定。</p>
        </div>
        <label className="settings-shortcut-search">
          <Search aria-hidden="true" />
          <input value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder="搜索快捷键" aria-label="搜索快捷键" />
        </label>
        <button type="button" className="settings-action-button settings-shortcut-reset" onClick={resetShortcutBindings}>
          <RotateCcw aria-hidden="true" />恢复默认
        </button>
      </div>
      {error && <div className="settings-shortcut-error" role="alert">{error}</div>}
      <div className="settings-card settings-shortcuts-card">
        {shortcuts.map((shortcut) => (
          <div className="settings-shortcut-row" key={shortcut.id}>
            <span className="settings-shortcut-icon" aria-hidden="true">{shortcutIcon(shortcut.action)}</span>
            <span className="settings-shortcut-action">{shortcut.label}</span>
            <button
              type="button"
              className="settings-shortcut-binding"
              data-recording={recording === shortcut.id ? "true" : "false"}
              onClick={() => { setRecording(shortcut.id); setError(""); }}
              aria-label={`编辑 ${shortcut.label}`}
            >
              <kbd>{recording === shortcut.id ? "请按快捷键" : formatShortcut(bindings[shortcut.id])}</kbd>
              <Pencil aria-hidden="true" />
            </button>
            <button
              type="button"
              className="settings-shortcut-delete"
              onClick={() => setShortcutBinding(shortcut.id, "")}
              disabled={!bindings[shortcut.id]}
              title="删除绑定"
              aria-label={`删除 ${shortcut.label} 快捷键`}
            >
              <Trash2 aria-hidden="true" />
            </button>
          </div>
        ))}
        {shortcuts.length === 0 && <div className="settings-empty-inline">没有匹配的快捷键。</div>}
      </div>
    </section>
  );
};
