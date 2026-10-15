import { useI18n } from "../i18n";
import { useEffect, useMemo, useState } from "react";
import { Pencil, RotateCcw, Search, Trash2 } from "lucide-react";
import { findShortcutConflict, formatShortcut, shortcutFromEvent, SHORTCUT_DEFINITIONS, type ShortcutActionId } from "../lib/keyboard-shortcuts";
import { useAppStore } from "../stores";

export const ShortcutsTab = ({ searchTarget, active = true }: { searchTarget?: string; active?: boolean }) => {
  const { t: tr } = useI18n();
  const [query, setQuery] = useState("");
  const [recording, setRecording] = useState<ShortcutActionId | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { if (searchTarget) setQuery(searchTarget); }, [searchTarget]);
  useEffect(() => { if (!active) { setRecording(null); setError(""); } }, [active]);
  const bindings = useAppStore((state) => state.shortcutBindings);
  const setShortcutBinding = useAppStore((state) => state.setShortcutBinding);
  const resetShortcutBindings = useAppStore((state) => state.resetShortcutBindings);
  const normalizedQuery = query.trim().toLowerCase();
  const shortcuts = useMemo(() => (
    SHORTCUT_DEFINITIONS.map((shortcut) => ({ ...shortcut, label: tr(shortcut.label), description: tr(shortcut.description) })).filter((shortcut) => !normalizedQuery || `${shortcut.label} ${shortcut.description} ${shortcut.action} ${formatShortcut(bindings[shortcut.id])}`.toLowerCase().includes(normalizedQuery))
  ), [bindings, normalizedQuery, tr]);

  useEffect(() => {
    if (!recording || !active) return;
    const capture = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") {
        setRecording(null);
        setError("");
        return;
      }
      if ((event.key === "Backspace" || event.key === "Delete") && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
        setShortcutBinding(recording, "");
        setRecording(null);
        setError("");
        return;
      }
      const binding = shortcutFromEvent(event);
      if (!binding) return;
      if (!binding.includes("Mod") && !binding.includes("Alt") && !/^F\d+$/.test(event.key)) {
        setError(tr("快捷键需要 Ctrl/Cmd、Alt 或功能键。"));
        return;
      }
      const conflict = findShortcutConflict(bindings, recording, binding);
      if (conflict) {
        setError(tr("已用于“{v0}”。", { v0: tr(conflict.label) }));
        return;
      }
      setShortcutBinding(recording, binding);
      setRecording(null);
      setError("");
    };
    window.addEventListener("keydown", capture, true);
    return () => window.removeEventListener("keydown", capture, true);
  }, [active, bindings, recording, setShortcutBinding]);

  return (
    <section className="settings-group">
      <div className="settings-section-heading settings-section-heading-row">
        <label className="settings-shortcut-search">
          <Search aria-hidden="true" />
          <input value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder={tr("搜索快捷键")} aria-label={tr("搜索快捷键")} />
        </label>
        <button type="button" className="settings-action-button settings-shortcut-reset" onClick={resetShortcutBindings}>
          <RotateCcw aria-hidden="true" />{tr("恢复默认")}</button>
      </div>
      {error && <div className="settings-shortcut-error" role="alert">{error}</div>}
      <div className="settings-card settings-shortcuts-card">
        {shortcuts.map((shortcut) => (
          <div className="settings-shortcut-row" key={shortcut.id}>
            <span className="settings-shortcut-copy"><span className="settings-shortcut-action">{shortcut.label}</span><span className="settings-shortcut-description">{shortcut.description}</span></span>
            <button
              type="button"
              className="settings-shortcut-binding"
              data-recording={recording === shortcut.id ? "true" : "false"}
              data-shortcut-recording={active && recording === shortcut.id ? "true" : undefined}
              onClick={() => { setRecording(shortcut.id); setError(""); }}
              aria-label={tr("编辑 {v0}", { v0: shortcut.label })}
            >
              <kbd>{recording === shortcut.id ? tr("请按快捷键") : formatShortcut(bindings[shortcut.id])}</kbd>
              <Pencil aria-hidden="true" />
            </button>
            <button
              type="button"
              className="settings-shortcut-delete"
              onClick={() => setShortcutBinding(shortcut.id, "")}
              disabled={!bindings[shortcut.id]}
              title={tr("删除绑定")}
              aria-label={tr("删除 {v0} 快捷键", { v0: shortcut.label })}
            >
              <Trash2 aria-hidden="true" />
            </button>
          </div>
        ))}
        {shortcuts.length === 0 && <div className="settings-empty-inline">{tr("没有匹配的快捷键。")}</div>}
      </div>
    </section>
  );
};
