import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Copy, Download, LoaderCircle, MoreHorizontal, Plus, RotateCcw, Trash2, X } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { saveDictationPreferences } from "../lib/dictation-preferences";
import type { DictationPreferences } from "../lib/workbench-preferences";
import { SelectMenu } from "../components/SelectMenu";
import { ContextMenu } from "../components/ContextMenu";
import { desktop, type DictationHotkeyState } from "../desktop/runtime";
import { fetchVoiceService, listDictationRecordings, deleteDictationRecording, downloadDictationRecording, transcribeDictationRecording, type DictationRecording, type VoiceServiceStatus } from "../protocol/voice";
import { pushToast } from "./ToastContainer";
import "./VoiceSettings.css";

export function VoiceSettings({ active = true }: { active?: boolean }) {
  const { t: tr, locale } = useI18n();
  const settings = useAppStore((state) => state.workbenchPreferences);
  const update = (patch: Partial<DictationPreferences>) => { void saveDictationPreferences(patch).catch(reason => pushToast(reason instanceof Error ? reason.message : String(reason), "error")); };
  const providerRevision = useAppStore((state) => state.currentProvider + state.currentProviderBaseUrl);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceError, setDeviceError] = useState("");
  const [service, setService] = useState<VoiceServiceStatus | null>(null);
  const [serviceError, setServiceError] = useState("");
  const [history, setHistory] = useState<DictationRecording[]>([]);
  const [historyError, setHistoryError] = useState("");
  const [revision, setRevision] = useState(0);
  const [deviceRevision, setDeviceRevision] = useState(0);
  const [serviceRevision, setServiceRevision] = useState(0);
  const [native, setNative] = useState<DictationHotkeyState | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ record: DictationRecording; anchor: HTMLButtonElement } | null>(null);
  const [dictionaryDraft, setDictionaryDraft] = useState<string[] | null>(null);
  const entryRefs = useRef<Array<HTMLInputElement | null>>([]);
  const skipBlur = useRef(false);
  const dictionary = dictionaryDraft ?? (settings.dictationDictionary.length ? settings.dictationDictionary : [""]);
  const changed = () => setRevision(value => value + 1);

  useEffect(() => { if (!active) setMenu(null); }, [active]);
  useEffect(() => {
    if (!active) return;
    const invalidate = () => setServiceRevision(value => value + 1);
    window.addEventListener("dictation:service-changed", invalidate);
    window.addEventListener("focus", invalidate);
    return () => { window.removeEventListener("dictation:service-changed", invalidate); window.removeEventListener("focus", invalidate); };
  }, [active]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setServiceError("");
    void fetchVoiceService(settings.speechProvider, settings.speechModel, controller.signal).then(value => {
      if (!controller.signal.aborted) setService(value);
    }).catch(reason => { if (!controller.signal.aborted) setServiceError(String(reason instanceof Error ? reason.message : reason)); });
    return () => controller.abort();
  }, [active, settings.speechProvider, settings.speechModel, providerRevision, serviceRevision]);
  useEffect(() => {
    if (!active) return;
    const native = desktop()?.dictation;
    if (!native) return;
    let cancelled = false;
    const stop = native.onState(value => { if (!cancelled) setNative(value); });
    void native.get().then(value => { if (!cancelled) setNative(value); }).catch(reason => {
      if (!cancelled) setNative({ available: false, holdHotkey: "", toggleHotkey: "", error: reason instanceof Error ? reason.message : String(reason) });
    });
    return () => { cancelled = true; stop(); };
  }, [active]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setHistoryError("");
    void listDictationRecordings(controller.signal).then(value => { if (!controller.signal.aborted) setHistory(value); })
      .catch(reason => { if (!controller.signal.aborted) setHistoryError(reason instanceof Error ? reason.message : String(reason)); });
    window.addEventListener("dictation:history-changed", changed);
    window.addEventListener("focus", changed);
    return () => { controller.abort(); window.removeEventListener("dictation:history-changed", changed); window.removeEventListener("focus", changed); };
  }, [active, revision]);
  useEffect(() => {
    if (!active) return;
    if (!navigator.mediaDevices?.enumerateDevices) { setDeviceError(tr("当前页面无法读取录音设备，请在桌面端打开语音设置。")); return; }
    let cancelled = false;
    const refresh = () => { void navigator.mediaDevices.enumerateDevices().then(items => {
      if (!cancelled) { setDevices(items.filter(item => item.kind === "audioinput")); setDeviceError(""); }
    }).catch(reason => { if (!cancelled) setDeviceError(reason instanceof Error ? reason.message : String(reason)); }); };
    refresh(); navigator.mediaDevices.addEventListener("devicechange", refresh);
    return () => { cancelled = true; navigator.mediaDevices.removeEventListener("devicechange", refresh); };
  }, [active, deviceRevision, tr]);

  const runAction = async (record: DictationRecording, action: "copy" | "download" | "retry" | "delete") => {
    setBusy(record.id);
    try {
      if (action === "copy") await navigator.clipboard.writeText(record.text);
      else if (action === "download") await downloadDictationRecording(record);
      else if (action === "delete") await deleteDictationRecording(record.id);
      else {
        const configured = await fetchVoiceService(settings.speechProvider, settings.speechModel);
        if (!settings.speechEnabled || !configured.can_attempt) throw new Error(configured.reason);
        await transcribeDictationRecording(record.id, configured, settings.speechModel, settings.dictationDictionary);
      }
    } catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
    finally { setBusy(null); changed(); }
  };
  const saveDictionary = async (entries: string[]) => {
    setDictionaryDraft(entries);
    try {
      await saveDictationPreferences({ dictationDictionary: entries.map(value => value.trim()).filter(Boolean) });
      setDictionaryDraft(current => current === entries ? null : current);
    } catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
  };
  const addEntry = (index: number) => {
    const next = [...dictionary.slice(0, index), "", ...dictionary.slice(index)];
    setDictionaryDraft(next); requestAnimationFrame(() => entryRefs.current[index]?.focus());
  };
  return <>
    <section className="settings-group"><h3 className="settings-group-title">{tr("常规")}</h3><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("麦克风")}</div><div className="settings-row-description">{tr("选择用于听写的输入设备。")}</div></div>
        <div className="settings-row-control voice-settings-device"><SelectMenu ariaLabel={tr("麦克风设备")} className="settings-select" align="end" value={settings.microphoneId} onValueChange={value => update({ microphoneId: value })}>
          <option value="">{tr("系统默认")}</option>
          {settings.microphoneId && !devices.some(device => device.deviceId === settings.microphoneId) && <option value={settings.microphoneId}>{tr("已选设备（当前未检测到）")}</option>}
          {devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || tr("麦克风 {v0}", { v0: index + 1 })}</option>)}
        </SelectMenu></div></div>
      {deviceError && <div className="voice-settings-observation"><p role="alert">{deviceError}</p><button type="button" className="settings-action-button" onClick={() => setDeviceRevision(value => value + 1)}>{tr("重试")}</button></div>}
    </div></section>
    <section className="settings-group"><h3 className="settings-group-title">{tr("听写")}</h3><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("听写")}</div>
        <div className="settings-row-description">{settings.speechEnabled && service?.can_attempt ? tr("录音转成草稿，发送前可以继续编辑。") : tr("在提供商设置中填写音频转录模型以使用听写。")}</div></div></div>
      {serviceError && <div className="voice-settings-observation"><p role="alert">{serviceError}</p><button type="button" className="settings-action-button" onClick={() => setServiceRevision(value => value + 1)}>{tr("重试")}</button></div>}
      {native?.available && <>
        <DictationShortcut mode="hold" active={active} state={native} onState={setNative} />
        <div className="voice-settings-advanced"><button type="button" className="settings-text-button" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>{tr("高级")}{advanced ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</button></div>
        {advanced && <DictationShortcut mode="toggle" active={active && advanced} state={native} onState={setNative} />}
      </>}
      {native?.error && <p className="voice-settings-error" role="alert">{native.error}</p>}
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("语言")}</div><div className="settings-row-description">{tr("按录音语言识别，也可以指定常用语言。")}</div></div>
        <SelectMenu ariaLabel={tr("语音语言")} className="settings-select" align="end" value={settings.speechLanguage} onValueChange={value => update({ speechLanguage: value })}><option value="">{tr("自动识别")}</option><option value="zh">{tr("中文")}</option><option value="en">English</option><option value="ja">{tr("日本語")}</option></SelectMenu></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("最近的录音")}</div><div className="settings-row-description">{tr("最近 20 段录音保存在这台设备上。")}</div></div></div>
      {historyError && <div className="voice-settings-observation"><p role="alert">{historyError}</p><button type="button" className="settings-action-button" onClick={changed}>{tr("重试")}</button></div>}
      {history.map(record => <div key={record.id} className="voice-history-row">
        <div className="voice-history-copy"><div className="voice-history-time">{new Date(record.created_at * 1000).toLocaleString(locale, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}{record.duration_ms >= 1000 ? " · " + Math.floor(record.duration_ms / 1000) + "s" : ""}</div>
          {record.text && <p>{record.text}</p>}{record.error && <p className="voice-recording-error">{record.error}</p>}</div>
        {busy === record.id && <LoaderCircle size={14} className="settings-spin" aria-label={tr("正在处理录音")} />}
        <button type="button" className="settings-icon-button" aria-label={tr("录音操作")} disabled={busy !== null} onClick={event => setMenu({ record, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button>
      </div>)}
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("听写词典")}</div><div className="settings-row-description">{tr("听写时应识别的词语或短语。")}</div></div>
        <button type="button" className="settings-action-button" onMouseDown={event => event.preventDefault()} onClick={() => addEntry(dictionary.length)}><Plus size={14} />{tr("添加条目")}</button></div>
      {dictionary.map((value, index) => <div className="voice-dictionary-entry" key={index}>
        <input ref={element => { entryRefs.current[index] = element; }} aria-label={tr("词典条目 {v0}", { v0: index + 1 })} value={value} placeholder="MiniCode"
          onChange={event => { const entries = [...dictionary]; entries[index] = event.target.value; setDictionaryDraft(entries); }}
          onBlur={() => { if (skipBlur.current) { skipBlur.current = false; return; } saveDictionary(dictionary); }}
          onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); skipBlur.current = true; addEntry(index + 1); } }} />
        <button type="button" className="settings-icon-button" aria-label={tr("移除词典条目 {v0}", { v0: index + 1 })} disabled={dictionary.length === 1 && !value}
          onMouseDown={event => event.preventDefault()} onClick={() => saveDictionary(dictionary.filter((_, entryIndex) => entryIndex !== index))}><Trash2 size={14} /></button>
      </div>)}
    </div></section>
    {menu && active && <ContextMenu anchor={menu.anchor} returnFocusTo={menu.anchor} position={{ x: menu.anchor.getBoundingClientRect().right, y: menu.anchor.getBoundingClientRect().bottom }} onClose={() => setMenu(null)} items={[
      { label: tr("复制转录文字"), icon: <Copy size={14} />, disabled: !menu.record.text, onClick: () => void runAction(menu.record, "copy") },
      { label: tr("下载录音"), icon: <Download size={14} />, onClick: () => void runAction(menu.record, "download") },
      { label: tr("重新转录"), icon: <RotateCcw size={14} />, onClick: () => void runAction(menu.record, "retry") },
      { label: tr("删除"), icon: <Trash2 size={14} />, onClick: () => void runAction(menu.record, "delete") },
    ]} />}
  </>;
}

function DictationShortcut({ mode, active, state, onState }: { mode: "hold" | "toggle"; active: boolean; state: DictationHotkeyState; onState: (state: DictationHotkeyState) => void }) {
  const { t: tr } = useI18n();
  const [capturing, setCapturing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bareModifiers = useRef("");
  const property = mode === "hold" ? "holdHotkey" : "toggleHotkey";
  const title = mode === "hold" ? tr("听写快捷键") : tr("单击听写快捷键");
  const save = async (value: string) => {
    const native = desktop()!.dictation!;
    setBusy(true); setCapturing(false); setError("");
    try {
      const normalized = (binding: string) => binding.replace(/Mod/g, "Ctrl").replace(/Key(?=[A-Z])/g, "").replace(/Digit(?=[0-9])/g, "");
      if (value && Object.values(useAppStore.getState().shortcutBindings).flat().some(binding => normalized(binding) === value)) throw new Error(tr("此快捷键已用于其他操作。"));
      onState(await native.configure({ [property]: value }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (!active) setCapturing(false); }, [active]);
  useEffect(() => {
    if (!capturing || !active) return;
    const native = desktop()!.dictation!;
    void native.suspend(true).catch(reason => setError(String(reason)));
    const capture = (event: KeyboardEvent) => {
      event.preventDefault(); event.stopPropagation();
      if (event.key === "Escape") { setCapturing(false); return; }
      const modifiers = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Win"].filter(Boolean) as string[];
      const bare = ["Control", "Alt", "Shift", "Meta"].includes(event.key);
      if (event.type === "keydown" && bare) { bareModifiers.current = modifiers.join("+"); return; }
      if (event.type === "keyup") { if (bare && bareModifiers.current) void save(bareModifiers.current); return; }
      if (["Backspace", "Delete"].includes(event.key) && !modifiers.length) { void save(""); return; }
      const code = event.code.replace(/^Key/, "").replace(/^Digit/, "");
      void save([...modifiers, code].join("+"));
    };
    window.addEventListener("keydown", capture, true); window.addEventListener("keyup", capture, true);
    return () => { window.removeEventListener("keydown", capture, true); window.removeEventListener("keyup", capture, true); void native.suspend(false).catch(reason => setError(String(reason))); };
  }, [active, capturing]);
  return <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{title}</div>
    <div className="settings-row-description">{mode === "hold" ? tr("按住即可听写，双击可免提听写。") : tr("按一下开始，再按一下结束。")}</div>{error && <p className="voice-settings-error" role="alert">{error}</p>}</div>
    <div className="voice-shortcut-control"><button type="button" className="settings-action-button voice-shortcut-capture" aria-label={title} disabled={busy} data-capturing={capturing} onClick={() => { bareModifiers.current = ""; setError(""); setCapturing(true); }}>{capturing ? tr("按下快捷键…") : state[property] || tr("关闭")}</button>
      {state[property] && <button type="button" className="settings-icon-button" aria-label={tr("清除快捷键")} disabled={busy} onClick={() => void save("")}><X size={13} /></button>}</div>
  </div>;
}
