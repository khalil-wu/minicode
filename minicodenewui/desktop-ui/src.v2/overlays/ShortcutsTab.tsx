import { useI18n } from "../i18n";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pencil, RotateCcw, Search, Trash2, X } from "lucide-react";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import "./ShortcutsTab.css";
import { findShortcutConflict, formatShortcut, isCodeShortcut, nativeShortcutOverrides, shortcutFromEvent, SHORTCUT_DEFINITIONS, type ShortcutActionId } from "../lib/keyboard-shortcuts";
import { useAppStore } from "../stores";
import { showConfirm } from "./DialogService";
import { SelectMenu } from "../components/SelectMenu";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { isDesktop } from "../desktop/runtime";
import type { ExternalBrowserLinkShortcut, PrimaryNumberShortcutTarget } from "../lib/shortcut-navigation";

export const ShortcutsTab = ({ searchTarget, active = true }: { searchTarget?: string; active?: boolean }) => {
  const { t: tr, locale } = useI18n();
  const { preferences, update } = useDesktopPreferences();
  const [savingPreferences, setSavingPreferences] = useState(false);
  const mac = /Mac/i.test(navigator.platform);
  const primary = mac ? "⌘" : "Ctrl+";
  const secondary = mac ? "⌃" : "Alt+";
  const copy = locale === "en-US" ? {
    number: "Number shortcuts", tabs: `${primary}1–9 for Tabs`, chats: `${primary}1–9 for Chats`,
    numberDescription: `Use ${primary}1–9 to switch ${preferences?.primaryNumberShortcutTarget === "sidebar" ? "chats" : "tabs"} and ${secondary}1–9 to switch ${preferences?.primaryNumberShortcutTarget === "sidebar" ? "tabs" : "chats"}`,
    external: "Open web link in default browser", externalDescription: "Hold the assigned keys while clicking a web link to open it in your default system browser",
    unassigned: "Unassigned", click: "Click", conflicts: "Some number shortcuts are already assigned to other actions",
  } : {
    number: "数字快捷键", tabs: `${primary}1–9 切换标签页`, chats: `${primary}1–9 选择聊天`,
    numberDescription: `使用 ${primary}1–9 切换${preferences?.primaryNumberShortcutTarget === "sidebar" ? "聊天" : "标签页"}，使用 ${secondary}1–9 切换${preferences?.primaryNumberShortcutTarget === "sidebar" ? "标签页" : "聊天"}`,
    external: "在默认浏览器中打开网页链接", externalDescription: "按住指定按键并点击网页链接，即可在系统默认浏览器中打开。",
    unassigned: "未分配", click: "单击", conflicts: "部分数字快捷键已分配给其他操作",
  };
  const savePreference = async (patch: { primaryNumberShortcutTarget?: PrimaryNumberShortcutTarget; externalBrowserLinkShortcut?: ExternalBrowserLinkShortcut }) => {
    setSavingPreferences(true); await update(patch); setSavingPreferences(false);
  };
  const [query, setQuery] = useState("");
  const [byKeystrokes, setByKeystrokes] = useState(false);
  const [searchFocused, setSearchFocused] = useState(false);
  const [recording, setRecording] = useState<{ action: ShortcutActionId; index: number } | null>(null);
  const [error, setError] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const bindings = useAppStore(state => state.shortcutBindings);
  const appMode = useAppStore(state => state.appMode);
  const setShortcutBinding = useAppStore(state => state.setShortcutBinding);
  const resetShortcutBindings = useAppStore(state => state.resetShortcutBindings);
  useEffect(() => { if (searchTarget) { setQuery(searchTarget); setByKeystrokes(false); } }, [searchTarget]);
  useEffect(() => { if (!active) { setRecording(null); setError(""); setSearchFocused(false); } }, [active]);
  const normalizedQuery = query.trim().toLowerCase();
  const showPreference = (text: string) => isDesktop() && !byKeystrokes && (!normalizedQuery || text.toLowerCase().includes(normalizedQuery));
  const shortcuts = useMemo(() => SHORTCUT_DEFINITIONS
    .filter(shortcut => appMode === "code" || !isCodeShortcut(shortcut.id))
    .map(shortcut => ({ ...shortcut, label: tr(shortcut.label), description: tr(shortcut.description) }))
    .filter(shortcut => !normalizedQuery || (byKeystrokes
      ? bindings[shortcut.id].some(binding => binding === query)
      : `${shortcut.label} ${shortcut.description} ${shortcut.action} ${formatShortcut(bindings[shortcut.id])} ${formatShortcut(bindings[shortcut.id]).replaceAll(" + ", "+")}`.toLowerCase().includes(normalizedQuery))),
  [appMode, bindings, byKeystrokes, normalizedQuery, query, tr]);

  const cancel = () => { setRecording(null); setError(""); };
  useEffect(() => {
    if (!recording || !active) return;
    const capture = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") { cancel(); return; }
      if ((event.key === "Backspace" || event.key === "Delete") && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
        setShortcutBinding(recording.action, "", recording.index); cancel(); return;
      }
      const binding = shortcutFromEvent(event);
      if (!binding) return;
      if (!binding.includes("Mod") && !binding.includes("Alt") && !/^F\d+$/.test(event.key)) {
        setError(tr("快捷键需要 Ctrl/Cmd、Alt 或功能键。")); return;
      }
      const conflict = findShortcutConflict(bindings, recording.action, binding);
      if (conflict) { setError(tr("已用于“{v0}”。", { v0: tr(conflict.label) })); return; }
      setShortcutBinding(recording.action, binding, recording.index);
      cancel();
    };
    window.addEventListener("keydown", capture, true);
    return () => window.removeEventListener("keydown", capture, true);
  }, [active, bindings, recording, setShortcutBinding, tr]);

  return <section className="settings-group settings-shortcuts">
    <div className="settings-section-heading settings-section-heading-row">
      <label className="settings-shortcut-search">
        <Search aria-hidden="true" />
        <input ref={searchRef} value={byKeystrokes ? formatShortcut(query) : query} readOnly={byKeystrokes}
          onFocus={() => setSearchFocused(true)} onBlur={() => setSearchFocused(false)}
          data-shortcut-recording={active && byKeystrokes && searchFocused ? "true" : undefined}
          onChange={event => setQuery(event.currentTarget.value)} placeholder={tr(byKeystrokes ? "请按快捷键搜索" : "搜索快捷键")}
          aria-label={tr(byKeystrokes ? "请按快捷键搜索" : "搜索快捷键")}
          onKeyDown={event => {
            if (!byKeystrokes || event.nativeEvent.isComposing) return;
            event.preventDefault(); event.stopPropagation();
            if (event.key === "Escape") { setQuery(""); setByKeystrokes(false); return; }
            if (!event.repeat) { const chord = shortcutFromEvent(event.nativeEvent); if (chord) setQuery(chord); }
          }} />
        {query && <button type="button" className="settings-shortcut-search-action" aria-label={tr("清除搜索")} onClick={() => { setQuery(""); searchRef.current!.focus(); }}><X size={14} /></button>}
        <button type="button" className="settings-shortcut-search-action" aria-label={tr("按键搜索")} title={tr("按键搜索")}
          aria-pressed={byKeystrokes} onClick={() => { cancel(); setQuery(""); setByKeystrokes(!byKeystrokes); searchRef.current!.focus(); }}><SettingsSourceIcon icon="shortcuts" size={16} /></button>
      </label>
      {(nativeShortcutOverrides(bindings).length > 0 || preferences && preferences.externalBrowserLinkShortcut !== "primary") && <button type="button" className="settings-action-button settings-shortcut-reset" onClick={async () => {
        if (await showConfirm({ title: tr("恢复默认"), message: tr("将所有快捷键恢复为默认绑定？"), confirmLabel: tr("恢复默认") })) { resetShortcutBindings(); if (isDesktop()) await savePreference({ externalBrowserLinkShortcut: "primary" }); }
      }}><RotateCcw size={14} />{tr("恢复默认")}</button>}
    </div>
    {showPreference(`${copy.number} ${copy.numberDescription} ${copy.tabs} ${copy.chats}`) && <div className="settings-card settings-shortcut-preference"><div className="settings-row">
      <div className="settings-row-copy"><div className="settings-row-title">{copy.number}</div><div className="settings-row-description">{copy.numberDescription}</div>
        {Object.values(bindings).flat().some(binding => /^(Mod|Alt)\+(Digit)?[1-9]$/.test(binding)) && <div className="settings-row-description">{copy.conflicts}</div>}</div>
      <SelectMenu ariaLabel={copy.number} className="settings-select settings-select-regular" align="end" disabled={!active || !preferences || savingPreferences}
        value={preferences?.primaryNumberShortcutTarget || "tabs"} onValueChange={value => void savePreference({ primaryNumberShortcutTarget: value as PrimaryNumberShortcutTarget })}>
        <option value="tabs">{copy.tabs}</option><option value="sidebar">{copy.chats}</option>
      </SelectMenu>
    </div></div>}
    {showPreference(`${copy.external} ${copy.externalDescription}`) && <div className="settings-card settings-shortcut-preference"><div className="settings-row">
      <div className="settings-row-copy"><div className="settings-row-title">{copy.external}</div><div className="settings-row-description">{copy.externalDescription}</div></div>
      <SelectMenu ariaLabel={copy.external} className="settings-select settings-select-regular" align="end" disabled={!active || !preferences || savingPreferences}
        value={preferences?.externalBrowserLinkShortcut || "primary"} onValueChange={value => void savePreference({ externalBrowserLinkShortcut: value as ExternalBrowserLinkShortcut })}>
        <option value="unassigned">{copy.unassigned}</option><option value="primary">{primary}{copy.click}</option><option value="alt">{mac ? "⌥" : "Alt+"}{copy.click}</option><option value="primaryShift">{primary}{mac ? "⇧" : "Shift+"}{copy.click}</option>
      </SelectMenu>
    </div></div>}
    {error && <div className="settings-shortcut-error" role="alert">{error}</div>}
    <div className="settings-card settings-shortcuts-card">
      {shortcuts.map(shortcut => <div className="settings-shortcut-row" key={shortcut.id}>
        <span className="settings-shortcut-copy"><span className="settings-shortcut-action">{shortcut.label}</span>{shortcut.description !== shortcut.label && <span className="settings-shortcut-description">{shortcut.description}</span>}</span>
        <div className="settings-shortcut-bindings">{(bindings[shortcut.id].length ? bindings[shortcut.id] : [""]).map((binding, index) => {
          const capturing = recording?.action === shortcut.id && recording.index === index;
          return <div className="settings-shortcut-slot" key={index}>
            {capturing ? <><input autoFocus readOnly className="settings-shortcut-capture" aria-label={tr("编辑 {v0}", { v0: shortcut.label })}
              data-shortcut-recording={active ? "true" : undefined} placeholder={tr("请按快捷键")} /><button type="button" className="settings-action-button" onClick={cancel}>{tr("取消")}</button></>
              : <><button type="button" className="settings-shortcut-binding" aria-label={tr("编辑 {v0}", { v0: shortcut.label })}
                onClick={() => { setRecording({ action: shortcut.id, index }); setError(""); }}><kbd data-unassigned={!binding}>{formatShortcut(binding).replaceAll(" + ", "+") || tr("未设置")}</kbd><Pencil aria-hidden="true" /></button>
              {binding && <button type="button" className="settings-shortcut-delete" title={tr("删除绑定")} aria-label={tr("删除 {v0} 快捷键", { v0: shortcut.label })}
                onClick={() => setShortcutBinding(shortcut.id, "", index)}><Trash2 aria-hidden="true" /></button>}</>}
          </div>;
        })}</div>
      </div>)}
      {shortcuts.length === 0 && <div className="settings-empty-inline">{tr("没有匹配的快捷键。")}</div>}
    </div>
  </section>;
};
