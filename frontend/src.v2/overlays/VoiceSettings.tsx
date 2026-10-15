import { useI18n } from "../i18n";
import { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, LoaderCircle, Mic, RefreshCw } from "lucide-react";
import { useAppStore } from "../stores";
import { SelectMenu } from "../components/SelectMenu";
import { checkVoiceService, fetchVoiceService, type VoiceServiceStatus } from "../protocol/voice";
import "./VoiceSettings.css";

export function VoiceSettings({ active = true }: { active?: boolean }) {
  const { t: tr } = useI18n();
  const settings = useAppStore((state) => state.workbenchPreferences);
  const update = useAppStore((state) => state.setWorkbenchPreferences);
  const providerRevision = useAppStore((state) => state.currentProvider + state.currentProviderBaseUrl);
  const [draft, setDraft] = useState({ provider: settings.speechProvider, model: settings.speechModel });
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceError, setDeviceError] = useState("");
  const [service, setService] = useState<VoiceServiceStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<"save" | "check" | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [advanced, setAdvanced] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setLoading(true); setError("");
    void fetchVoiceService(settings.speechProvider, settings.speechModel, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setService(value); })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, settings.speechProvider, settings.speechModel, providerRevision, revision]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    if (!active || !navigator.mediaDevices?.enumerateDevices) return;
    let cancelled = false;
    const refresh = () => {
      void navigator.mediaDevices.enumerateDevices().then((items) => {
        if (!cancelled) { setDevices(items.filter((item) => item.kind === "audioinput")); setDeviceError(""); }
      }).catch((reason) => { if (!cancelled) setDeviceError(reason instanceof Error ? reason.message : String(reason)); });
    };
    refresh();
    navigator.mediaDevices.addEventListener("devicechange", refresh);
    return () => { cancelled = true; navigator.mediaDevices.removeEventListener("devicechange", refresh); };
  }, [active]);

  const save = async () => {
    const controller = request.current = new AbortController();
    setBusy("save"); setError("");
    try {
      const result = await fetchVoiceService(draft.provider, draft.model.trim(), controller.signal);
      if (!result.can_attempt) throw new Error(result.reason);
      update({ speechProvider: draft.provider, speechModel: draft.model.trim(), speechEnabled: true });
      setService(result); setAdvanced(false);
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  };
  const verify = async () => {
    const controller = request.current = new AbortController();
    setBusy("check"); setError("");
    try {
      const result = await checkVoiceService(settings.speechProvider, settings.speechModel, controller.signal);
      const current = useAppStore.getState();
      if (current.workbenchPreferences.speechProvider === settings.speechProvider && current.workbenchPreferences.speechModel === settings.speechModel
        && current.currentProvider + current.currentProviderBaseUrl === providerRevision) setService(result);
    }
    catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  };
  const stateLabel = loading ? tr("读取配置中") : !settings.speechEnabled ? tr("尚未启用") : !service?.can_attempt ? tr("需要配置")
    : service.last_check?.ok ? tr("上次检测通过") : service.last_check ? tr("上次检测失败") : tr("已配置 · 尚未验证");
  const checkTime = service?.last_check ? new Date(service.last_check.at * 1000).toLocaleString() : "";
  return <>
    <section className="settings-group">
      <h3 className="settings-group-title">{tr("常规")}</h3>
      <div className="settings-card">
        <div className="settings-row">
          <div className="settings-row-copy"><div className="settings-row-title">{tr("麦克风")}</div><div className="settings-row-description">{tr("选择用于听写的输入设备。")}</div></div>
          <div className="settings-row-control voice-settings-device">
            <SelectMenu ariaLabel={tr("麦克风设备")} className="settings-select" align="end" value={settings.microphoneId} onValueChange={(value) => update({ microphoneId: value })}>
              <option value="">{tr("系统默认")}</option>
              {settings.microphoneId && !devices.some((device) => device.deviceId === settings.microphoneId) && <option value={settings.microphoneId}>{tr("已选设备（当前未检测到）")}</option>}
              {devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || "麦克风 " + (index + 1)}</option>)}
            </SelectMenu>
            <button type="button" className="settings-icon-button" title={tr("刷新麦克风")} aria-label={tr("刷新麦克风")} onClick={() => {
              if (!navigator.mediaDevices?.enumerateDevices) { setDeviceError(tr("当前页面无法读取录音设备，请在桌面端打开语音设置。")); return; }
              void navigator.mediaDevices.enumerateDevices().then((items) => { setDevices(items.filter((item) => item.kind === "audioinput")); setDeviceError(""); })
                .catch((reason) => setDeviceError(reason instanceof Error ? reason.message : String(reason)));
            }}><RefreshCw size={15} /></button>
          </div>
        </div>
        <div className="settings-row">
          <div className="settings-row-copy"><div className="settings-row-title">{tr("听写")}</div><div className="settings-row-description">{tr("录音转成草稿，发送前可以继续编辑。")}</div></div>
          <div className="settings-row-control"><button type="button" className="settings-toggle" role="switch" aria-label={tr("启用听写")} aria-checked={settings.speechEnabled} data-active={settings.speechEnabled}
            onClick={() => { if (settings.speechEnabled) update({ speechEnabled: false }); else setAdvanced(true); }}><span /></button></div>
        </div>
      </div>
      {deviceError && <p className="voice-settings-error" role="alert">{deviceError}</p>}
    </section>
    <section className="settings-group">
      <h3 className="settings-group-title">{tr("听写服务")}</h3>
      <div className="settings-card">
        <div className="settings-row">
          <div className="settings-row-copy"><div className="settings-row-title">{service?.label || tr("配置听写服务")}</div>
            <div className="settings-row-description">{service?.reason || tr("使用已配置的兼容服务，转录模型由服务商提供。")}</div></div>
          <div className="settings-row-control"><span className="voice-service-status" data-state={service?.last_check?.ok ? "success" : "neutral"}>
            {loading ? <LoaderCircle className="settings-spin" size={13} /> : service?.last_check?.ok ? <Check size={13} /> : <Mic size={13} />}{stateLabel}</span></div>
        </div>
        {service?.last_check && <div className="voice-service-observation"><span>{checkTime}</span>{!service.last_check.ok && <p role="alert">{service.last_check.error}</p>}</div>}
        <div className="settings-row">
          <div className="settings-row-copy"><div className="settings-row-title">{tr("检测转录接口")}</div><div className="settings-row-description">{tr("发送 1 秒静音测试音频，不录制麦克风；可能产生服务商用量。")}</div></div>
          <div className="settings-row-control">{busy === "check" ? <button type="button" className="settings-action-button" onClick={() => { request.current?.abort(); setBusy(null); }}>{tr("取消检测")}</button> :
            <button type="button" className="settings-action-button" disabled={!settings.speechEnabled || !service?.can_attempt || Boolean(busy)} onClick={() => void verify()}>{tr("检测服务")}</button>}</div>
        </div>
        <div className="voice-service-actions"><button type="button" className="settings-action-button" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>{tr("配置服务") + " "}<ChevronRight size={14} /></button>
          <button type="button" className="settings-action-button" onClick={() => useAppStore.getState().setSettingsTab("provider")}>{tr("管理服务商")}</button>
          <button type="button" className="settings-icon-button" aria-label={tr("重新读取语音配置")} disabled={loading} onClick={() => setRevision((value) => value + 1)}><RefreshCw size={14} /></button></div>
        {advanced && <form className="voice-service-config" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label><span>{tr("服务商")}</span><SelectMenu ariaLabel={tr("听写服务商")} className="settings-select settings-select-regular" align="end" value={draft.provider} onValueChange={(value) => setDraft({ ...draft, provider: value })}>
            <option value="">{tr("默认服务商配置")}</option><option value="openai">OpenAI</option><option value="custom">{tr("自定义兼容服务")}</option><option value="anthropic">Anthropic</option>
          </SelectMenu></label>
          <label><span>{tr("转录模型")}</span><input aria-label={tr("转录模型")} autoComplete="off" value={draft.model} placeholder={tr("填写服务商支持的转录模型")} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></label>
          <p>{tr("这里配置服务商提供的听写能力。聊天模型列表不能用于确认转录支持。")}</p>
          <div><button type="submit" className="settings-action-button" disabled={!draft.model.trim() || Boolean(busy)}>{busy === "save" ? tr("检查配置…") : tr("保存并启用")}</button></div>
        </form>}
      </div>
      {error && <p className="voice-settings-error" role="alert">{error}</p>}
    </section>
    <section className="settings-group"><h3 className="settings-group-title">{tr("识别偏好")}</h3><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("语言")}</div><div className="settings-row-description">{tr("按录音语言识别，也可以指定常用语言。")}</div></div>
        <div className="settings-row-control"><SelectMenu ariaLabel={tr("语音语言")} className="settings-select" align="end" value={settings.speechLanguage} onValueChange={(value) => update({ speechLanguage: value })}><option value="">{tr("自动识别")}</option><option value="zh">{tr("中文")}</option><option value="en">English</option><option value="ja">{tr("日本語")}</option></SelectMenu></div></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("专有词汇")}</div><div className="settings-row-description">{tr("帮助识别项目名称和技术名词。")}</div></div>
        <div className="settings-row-control"><input aria-label={tr("语音专有词汇")} placeholder={tr("例如：MiniCode、TypeScript")} value={settings.speechVocabulary} onChange={(event) => update({ speechVocabulary: event.target.value })} /></div></div>
    </div></section>
  </>;
}
