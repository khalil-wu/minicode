import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, ChevronRight, Cookie, File, FolderOpen, LockKeyhole, MoreHorizontal, Minus, Plus, RotateCcw, Settings2, X } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import { desktop, embeddedBrowserClearSiteData, embeddedBrowserGetSettings, embeddedBrowserSetSettings, onEmbeddedBrowserEvent, onEmbeddedBrowserSettingsChanged, type BrowserDownload, type BrowserSiteCategory, type BrowserSiteDecision, type BrowserSitePolicy } from "../desktop/runtime";
import { openSettings } from "../lib/settings-navigation";
import { useAppStore } from "../stores";
import type { UISlice } from "../stores/types";
import { useI18n } from "../i18n";
import { pushToast } from "../overlays/ToastContainer";
import { copyText } from "../lib/clipboard";
import { clearBrowserData } from "../lib/browser-data";
import { NumberInput } from "../components/NumberInput";
import { SelectMenu } from "../components/SelectMenu";

export type BrowserDeviceViewport = { width: number; height: number; mobile: boolean; presetId?: string };
export const DEFAULT_DEVICE_VIEWPORT: BrowserDeviceViewport = { width: 390, height: 844, mobile: false, presetId: "responsive" };
export type BrowserDeviceBounds = { x: number; y: number; width: number; height: number };
export type BrowserDeviceLayout = { fitWidth: number; fitHeight: number; scale: number; stageBounds: BrowserDeviceBounds; visualBounds: BrowserDeviceBounds };
type DeviceResizeEdge = "left" | "right" | "bottom" | "bottom-left" | "bottom-right";
type DeviceResizeDrag = { edge: DeviceResizeEdge; startWidth: number; startHeight: number; startPointerX: number; startPointerY: number };

/** Installed Source Qkn/rAn/nAn: 20px side grips and bottom gutter. */
export function browserDeviceLayout(stage: BrowserDeviceBounds, viewport: BrowserDeviceViewport): BrowserDeviceLayout {
  const fitWidth = Math.max(0, stage.width - 40), fitHeight = Math.max(0, stage.height - 20);
  const scale = Math.min(1, fitWidth / viewport.width, fitHeight / viewport.height);
  const width = Math.round(viewport.width * scale), height = Math.round(viewport.height * scale);
  return { fitWidth, fitHeight, scale, stageBounds: stage, visualBounds: {
    x: Math.round(stage.x + Math.max(20, (stage.width - width) / 2)), y: stage.y, width, height,
  } };
}

/** Installed Source iAn/sAn/F7/I7. Side dragging expands about the centre. */
export function resizeBrowserDeviceViewport(drag: DeviceResizeDrag, layout: BrowserDeviceLayout, pointerX: number, pointerY: number) {
  const widthChanges = drag.edge !== "bottom", heightChanges = drag.edge !== "left" && drag.edge !== "right";
  const direction = drag.edge === "left" || drag.edge === "bottom-left" ? -1 : 1;
  const fit = (raw: number, limit: number) => limit > 0 ? Math.min(raw, limit / layout.scale) : raw;
  return {
    width: widthChanges ? Math.min(4096, Math.max(240, Math.round(fit(drag.startWidth + (pointerX - drag.startPointerX) * direction * 2 / layout.scale, layout.fitWidth)))) : drag.startWidth,
    height: heightChanges ? Math.min(4096, Math.max(160, Math.round(fit(drag.startHeight + (pointerY - drag.startPointerY) / layout.scale, layout.fitHeight)))) : drag.startHeight,
  };
}

export function BrowserDeviceResizeHandles({ viewport, layout, onChange, onResizeActiveChange }: {
  viewport: BrowserDeviceViewport; layout: BrowserDeviceLayout; onChange: (viewport: BrowserDeviceViewport) => void; onResizeActiveChange: (active: boolean) => void;
}) {
  const { t } = useI18n();
  const drag = useRef<DeviceResizeDrag | null>(null);
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const frame = useRef<number | null>(null);
  const current = useRef({ viewport, layout, onChange, onResizeActiveChange });
  current.current = { viewport, layout, onChange, onResizeActiveChange };
  const flush = () => {
    frame.current = null;
    if (!drag.current || !pointer.current) return;
    const { viewport, layout, onChange } = current.current;
    const size = resizeBrowserDeviceViewport(drag.current, layout, pointer.current.x, pointer.current.y);
    pointer.current = null;
    onChange({ ...viewport, presetId: "responsive", ...size });
  };
  useEffect(() => () => {
    if (frame.current !== null) window.cancelAnimationFrame(frame.current);
    if (drag.current) current.current.onResizeActiveChange(false);
    frame.current = null; drag.current = null; pointer.current = null;
  }, []);
  const start = (event: ReactPointerEvent<HTMLButtonElement>, edge: DeviceResizeEdge) => {
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { edge, startWidth: viewport.width, startHeight: viewport.height, startPointerX: event.clientX, startPointerY: event.clientY };
    current.current.onResizeActiveChange(true);
  };
  const move = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!drag.current) return;
    pointer.current = { x: event.clientX, y: event.clientY };
    frame.current ??= window.requestAnimationFrame(flush);
  };
  const end = () => {
    if (frame.current !== null) { window.cancelAnimationFrame(frame.current); flush(); }
    if (drag.current) current.current.onResizeActiveChange(false);
    drag.current = null; pointer.current = null;
  };
  const handles = [
    ["left", "从左边缘调整设备视口"], ["right", "从右边缘调整设备视口"], ["bottom", "从下边缘调整设备视口"],
    ["bottom-left", "从左下角调整设备视口"], ["bottom-right", "从右下角调整设备视口"],
  ] as const;
  const rect = layout.visualBounds;
  return <div className="mc-browser-device-handles" style={{ left: rect.x - layout.stageBounds.x, top: rect.y - layout.stageBounds.y, width: rect.width, height: rect.height }}>
    {handles.map(([edge, label]) => <button type="button" key={edge} tabIndex={-1} aria-label={t(label)} className={`mc-browser-device-handle mc-browser-device-handle-${edge}`}
      onPointerDown={event => start(event, edge)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onLostPointerCapture={end}>
      {edge === "bottom-left" || edge === "bottom-right"
        ? <svg className="mc-browser-device-corner-grip" aria-hidden="true" fill="none" viewBox="0 0 20 20">
          <path d="M6 11.75L11.75 6" stroke="currentColor" strokeLinecap="round" strokeWidth="2.4" />
          <path d="M7 15.5L15.5 7" stroke="currentColor" strokeLinecap="round" strokeWidth="2.4" />
        </svg>
        : <span className="mc-browser-device-grip" aria-hidden="true"><i /><i /></span>}
    </button>)}
  </div>;
}
// Installed Codex 26.1013.4894.0: app-shared mAn / options-menu-view Xr.
const DEVICE_PRESETS = [
  ["responsive", "自适应", 390, 844], ["4k", "4K", 2560, 1440], ["laptop-l", "Laptop L", 1440, 900], ["laptop", "Laptop", 1024, 768],
  ["surface-pro-7", "Surface Pro 7", 912, 1368], ["ipad-air", "iPad Air", 820, 1180], ["ipad-mini", "iPad Mini", 768, 1024],
  ["surface-duo", "Surface Duo", 540, 720], ["iphone-15-pro-max", "iPhone 15 Pro Max", 430, 932], ["pixel-8", "Pixel 8", 412, 915],
  ["iphone-15-pro", "iPhone 15 Pro", 393, 852], ["samsung-galaxy-s24-ultra", "Samsung Galaxy S24 Ultra", 384, 824], ["iphone-se", "iPhone SE", 375, 667],
] as const;

export function BrowserDeviceToolbar({ conversationId, tabId, viewport, onChange }: {
  conversationId: string; tabId: string; viewport: BrowserDeviceViewport; onChange: (viewport: BrowserDeviceViewport | null) => void;
}) {
  const { t } = useI18n();
  const [zoom, setZoom] = useState(100);
  useEffect(() => { void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "zoom" }).then(result => setZoom(Math.round(result.zoomFactor! * 100))).catch(error => pushToast(String(error), "error")); }, [conversationId, tabId]);
  useEffect(() => onEmbeddedBrowserEvent(event => {
    if (event.id === tabId && event.conversationId === conversationId && event.zoomFactor !== undefined) setZoom(Math.round(event.zoomFactor * 100));
  }), [conversationId, tabId]);
  const applyZoom = (percent: number) => { void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "zoom", zoomFactor: percent / 100 }).then(result => setZoom(Math.round(result.zoomFactor! * 100))).catch(error => pushToast(String(error), "error")); };
  const zoomLevels = [50, 75, 100, 125, 150, 200];
  if (!zoomLevels.includes(zoom)) zoomLevels.push(zoom);
  return <div className="mc-browser-viewport-controls" role="group" aria-label={t("设备工具栏")}>
    <label className="mc-browser-device-dimensions-label" htmlFor={`mc-browser-device-preset-${tabId}`}>{t("尺寸：")}</label>
    <select id={`mc-browser-device-preset-${tabId}`} className="mc-browser-device-preset" aria-label={t("设备视口")} value={viewport.presetId || "responsive"} onChange={event => {
      const id = event.currentTarget.value, preset = DEVICE_PRESETS.find(item => item[0] === id)!;
      onChange({ ...viewport, presetId: id, ...(id === "responsive" ? {} : { width: preset[2], height: preset[3] }) });
      applyZoom(100);
      event.currentTarget.blur();
    }}>{DEVICE_PRESETS.map(([id, label]) => <option key={id} value={id}>{id === "responsive" ? t("自适应") : label}</option>)}</select>
    <div className="mc-browser-device-dimensions">
      <NumberInput aria-label={t("视口宽度")} min={240} max={4096} value={viewport.width} onCommit={width => onChange({ ...viewport, presetId: "responsive", width })}
        onBlur={event => { const width = event.currentTarget.valueAsNumber; if (!Number.isNaN(width)) onChange({ ...viewport, presetId: "responsive", width: Math.min(4096, Math.max(240, Math.round(width))) }); }}
        onKeyDown={event => { if (event.key === "Enter") event.currentTarget.blur(); }} /><span>×</span>
      <NumberInput aria-label={t("视口高度")} min={160} max={4096} value={viewport.height} onCommit={height => onChange({ ...viewport, presetId: "responsive", height })}
        onBlur={event => { const height = event.currentTarget.valueAsNumber; if (!Number.isNaN(height)) onChange({ ...viewport, presetId: "responsive", height: Math.min(4096, Math.max(160, Math.round(height))) }); }}
        onKeyDown={event => { if (event.key === "Enter") event.currentTarget.blur(); }} />
    </div>
    <button type="button" aria-label={t("旋转视口")} onClick={() => onChange({ ...viewport, width: viewport.height, height: viewport.width })}><RotateCcw size={16} /></button>
    <select className="mc-browser-device-zoom" aria-label={t("浏览器缩放")} value={String(zoom)} onChange={event => { applyZoom(Number(event.currentTarget.value)); event.currentTarget.blur(); }}>{zoomLevels.sort((a, b) => a - b).map(value => <option key={value} value={value}>{value}%</option>)}</select>
    <button type="button" className="mc-browser-device-exit" aria-label={t("退出设备工具栏模式")} onClick={() => onChange(null)}><X size={16} /></button>
  </div>;
}

export function BrowserSiteInfo({ conversationId, tabId, url, onClose }: { conversationId: string; tabId: string; url: string; onClose: () => void }) {
  const { t } = useI18n();
  const root = useRef<HTMLElement>(null);
  const [page, setPage] = useState<"home" | "connection" | "cookies" | "permissions">("home");
  const [site, setSite] = useState<{ origin?: string; hostname?: string; secure?: boolean; cookieCount?: number }>();
  const [policy, setPolicy] = useState<BrowserSitePolicy>();
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const settingsRevision = useRef(0);
  useEffect(() => {
    let active = true;
    const revision = settingsRevision.current;
    void Promise.all([desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "site-info" }), desktop()!.embeddedBrowser.getSitePolicy()]).then(([info, permissions]) => {
      if (active) { setSite(info); if (settingsRevision.current === revision) setPolicy(permissions); }
    }).catch(reason => { if (active) setError(String(reason)); });
    const unsubscribe = onEmbeddedBrowserSettingsChanged(() => {
      const current = ++settingsRevision.current;
      void desktop()!.embeddedBrowser.getSitePolicy().then(next => { if (active && settingsRevision.current === current) setPolicy(next); }).catch(reason => { if (active) setError(String(reason)); });
    });
    const outside = (event: PointerEvent) => { if (!root.current!.contains(event.target as Node) && !(event.target as Element).closest('[role="listbox"]')) onClose(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { active = false; unsubscribe?.(); document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [conversationId, tabId, url]);
  const updatePermission = async (category: BrowserSiteCategory, setting: BrowserSiteDecision | "default") => {
    setSaving(true); setError("");
    const revision = settingsRevision.current;
    try { const saved = await desktop()!.embeddedBrowser.setSitePolicy({ origin: site!.origin, category, setting }); if (settingsRevision.current === revision) setPolicy(saved); }
    catch (reason) { setError(String(reason)); }
    finally { setSaving(false); }
  };
  const clearSite = async () => {
    setSaving(true); setError("");
    try { await embeddedBrowserClearSiteData(conversationId, tabId); setPolicy(await desktop()!.embeddedBrowser.getSitePolicy()); setSite(current => ({ ...current, cookieCount: 0 })); }
    catch (reason) { setError(String(reason)); }
    finally { setSaving(false); }
  };
  const title = page === "home" ? site?.hostname || new URL(url).hostname : page === "connection" ? t("连接") : page === "cookies" ? t("Cookie 和网站数据") : t("网站设置");
  return <section ref={root} role="dialog" aria-label={t("站点信息")} className="mc-browser-site-info">
    <header>{page !== "home" && <button type="button" aria-label={t("返回")} onClick={() => setPage("home")}><ArrowLeft size={16} /></button>}<strong>{title}</strong><button type="button" aria-label={t("关闭")} onClick={onClose}><X size={16} /></button></header>
    {error && <p role="alert">{error}</p>}
    {site && page === "home" && <>
      <button type="button" onClick={() => setPage("connection")}><LockKeyhole size={16} /><span>{t(site.secure ? "连接安全" : "连接不安全")}</span><ChevronRight size={16} /></button>
      <button type="button" onClick={() => setPage("cookies")}><Cookie size={16} /><span>{t("Cookie 和网站数据")}</span><ChevronRight size={16} /></button>
      <button type="button" onClick={() => setPage("permissions")}><Settings2 size={16} /><span>{t("网站设置")}</span><ChevronRight size={16} /></button>
    </>}
    {site && page === "connection" && <div className="mc-browser-site-detail"><LockKeyhole size={20} /><strong>{t(site.secure ? "连接安全" : "连接不安全")}</strong><p>{t(site.secure ? "发送到此网站的信息通过 HTTPS 加密。" : "此网站未使用 HTTPS 加密连接。")}</p><span>{site.origin}</span></div>}
    {site && page === "cookies" && <div className="mc-browser-site-detail"><span>{site.origin}</span><p>{t("使用中的 Cookie：{count}", { count: site.cookieCount || 0 })}</p><button type="button" disabled={saving} onClick={() => void clearSite()}>{t("删除网站数据")}</button></div>}
    {page === "permissions" && policy && <div className="mc-browser-site-permissions">{policy.categories.map(({ id, allowedSettings }) => {
      const labels = { camera: "摄像头", microphone: "麦克风", geolocation: "位置", notifications: "通知", "clipboard-read": "读取剪贴板", mediaKeySystem: "受保护内容 ID", sound: "声音", images: "图片", javascript: "JavaScript", usb: "USB 设备" } as const;
      const decisions = { ask: "询问", allow: "允许", block: "阻止" } as const;
      const value = policy.sites.find(item => item.origin === site?.origin)?.settings[id] ?? "default";
      return <label key={id}><span>{t(labels[id])}</span><SelectMenu ariaLabel={t(labels[id])} value={value} disabled={saving} onValueChange={setting => void updatePermission(id, setting as BrowserSiteDecision | "default")}><option value="default">{t("默认设置：{setting}", { setting: t(decisions[policy.defaults[id]]) })}</option>{allowedSettings.map(setting => <option key={setting} value={setting}>{t(decisions[setting])}</option>)}</SelectMenu></label>;
    })}</div>}
  </section>;
}

export function BrowserPageActions({ conversationId, tabId, url, position, onClose, viewportEnabled, onViewportChange }: {
  conversationId: string; tabId: string; url: string; position: { x: number; y: number };
  onClose: () => void; viewportEnabled: boolean; onViewportChange: (enabled: boolean) => void;
}) {
  const { t } = useI18n();
  const [zoom, setZoom] = useState(1);
  const zoomRevision = useRef(0);
  const [alwaysShowDeviceToolbar, setAlwaysShowDeviceToolbar] = useState(false);
  useEffect(() => {
    void embeddedBrowserGetSettings("")!.then(settings => setAlwaysShowDeviceToolbar(settings!.deviceToolbarAlwaysOnNewTabs!));
    return onEmbeddedBrowserSettingsChanged(settings => setAlwaysShowDeviceToolbar(settings.deviceToolbarAlwaysOnNewTabs));
  }, []);
  useEffect(() => {
    let active = true;
    const revision = ++zoomRevision.current;
    const unsubscribe = onEmbeddedBrowserEvent(event => {
      if (event.id !== tabId || event.conversationId !== conversationId || event.zoomFactor === undefined) return;
      ++zoomRevision.current;
      setZoom(event.zoomFactor);
    });
    if (url) void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "zoom" })
      .then(result => { if (active && zoomRevision.current === revision) setZoom(result.zoomFactor!); }).catch(error => pushToast(String(error), "error"));
    return () => { active = false; unsubscribe?.(); };
  }, [conversationId, tabId, url]);
  const command = (action: "print" | "screenshot") => {
    void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action }).catch(error => pushToast(String(error), "error"));
  };
  const applyZoom = (value: number) => {
    const revision = zoomRevision.current;
    void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "zoom", zoomFactor: value })
      .then(result => { if (zoomRevision.current === revision) setZoom(result.zoomFactor!); }).catch(error => pushToast(String(error), "error"));
  };
  const stepZoom = (direction: number) => {
    const levels = [25, 33, 50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500];
    const current = Math.round(zoom * 100);
    const next = direction > 0 ? levels.find(value => value > current) ?? 500
      : levels.filter(value => value < current).at(-1) ?? 25;
    applyZoom(next / 100);
  };
  const browserSettings = (section: UISlice["browserSettingsSection"] = "general") => {
    useAppStore.getState().setBrowserSettingsSection(section);
    openSettings("browser");
  };
  return <ContextMenu ariaLabel={t("浏览器选项")} position={position} onClose={onClose} items={[
    { label: t("在页面中查找"), disabled: !url, onClick: () => window.dispatchEvent(new CustomEvent("browser:find", { detail: tabId })) },
    { label: t("打印"), disabled: !url, onClick: () => command("print") },
    { label: "", separator: true },
    { label: t("缩放"), content: <div className="mc-browser-zoom-row"><span>{t("缩放")}</span><div>
      <button type="button" aria-label={t("缩小")} disabled={!url || zoom <= .25} onClick={() => stepZoom(-1)}><Minus size={14} /></button>
      <span>{Math.round(zoom * 100)}%</span><button type="button" aria-label={t("放大")} disabled={!url || zoom >= 5} onClick={() => stepZoom(1)}><Plus size={14} /></button>
    </div><button type="button" aria-label={t("重置")} disabled={!url || zoom === 1} onClick={() => applyZoom(1)}><RotateCcw size={14} /></button></div> },
    { label: "", separator: true },
    { label: t("设备工具栏"), submenu: [
      { label: t(viewportEnabled ? "隐藏设备工具栏" : "显示设备工具栏"), disabled: !url, onClick: () => onViewportChange(!viewportEnabled) },
      { label: t("始终在新标签页中显示"), checked: alwaysShowDeviceToolbar, checkType: "checkbox", onClick: () => {
        void embeddedBrowserSetSettings({ deviceToolbarAlwaysOnNewTabs: !alwaysShowDeviceToolbar })!.catch(error => pushToast(String(error), "error"));
      } },
    ] },
    { label: t("截取屏幕截图"), disabled: !url, onClick: () => command("screenshot") },
    { label: "", separator: true },
    { label: t("密码和自动填充"), submenu: [
      { label: t("密码管理器"), onClick: () => browserSettings("passwords") },
      { label: t("联系信息"), onClick: () => browserSettings("contacts") },
    ] },
    { label: t("下载"), onClick: () => browserSettings("downloads") },
    { label: t("历史记录"), onClick: () => browserSettings("history") },
    { label: t("清除浏览数据"), onClick: () => { void clearBrowserData().catch(error => pushToast(String(error), "error")); } },
    { label: "", separator: true },
    { label: t("浏览器设置"), onClick: () => browserSettings() },
  ]} />;
}

export function BrowserFindBar({ conversationId, tabId }: { conversationId: string; tabId: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [result, setResult] = useState({ matches: 0, activeMatchOrdinal: 0 });
  const request = useRef<number>();
  const lastResult = useRef<{ requestId: number; matches: number; activeMatchOrdinal: number }>();
  const input = useRef<HTMLInputElement>(null);
  const findNext = useRef<(forward: boolean) => void>();
  useEffect(() => {
    const openFind = (event: Event) => { if ((event as CustomEvent).detail === tabId) { setOpen(true); input.current?.focus(); } };
    window.addEventListener("browser:find", openFind);
    const unsubscribe = onEmbeddedBrowserEvent(event => {
      if (event.id !== tabId || event.conversationId !== conversationId) return;
      if (event.type === "find-request") {
        if (event.findDirection) findNext.current!(event.findDirection === "next");
        else { setOpen(true); input.current?.focus(); }
      }
      if (event.type === "print-request") void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "print" }).catch(error => pushToast(String(error), "error"));
      if (event.type === "find-result" && event.findResult) {
        lastResult.current = event.findResult;
        if (event.findResult.requestId === request.current) setResult(event.findResult);
      }
    });
    return () => { window.removeEventListener("browser:find", openFind); unsubscribe?.(); };
  }, [conversationId, tabId]);
  const find = (value: string, next = false, forward = true) => {
    if (!value) { request.current = undefined; setResult({ matches: 0, activeMatchOrdinal: 0 }); }
    // Electron starts a finding session with true; follow-up navigation uses false.
    void desktop()!.embeddedBrowser.pageCommand({ id: tabId, conversationId, action: "find", text: value, findNext: !next, forward })
      .then(reply => {
        if (!value) return;
        request.current = reply.requestId;
        if (lastResult.current?.requestId === request.current) setResult(lastResult.current!);
      }).catch(error => pushToast(String(error), "error"));
  };
  findNext.current = forward => { if (text) find(text, true, forward); else setOpen(true); };
  const close = () => { setOpen(false); find(""); };
  useEffect(() => {
    if (open) {
      input.current!.focus();
      if (text) find(text);
    }
  }, [open]);
  if (!open) return null;
  return <form className="mc-browser-find" onSubmit={event => { event.preventDefault(); find(text, true); }} onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
    <input ref={input} aria-label={t("在页面中查找")} value={text} onChange={event => { setText(event.target.value); find(event.target.value); }} />
    <span>{result.activeMatchOrdinal}/{result.matches}</span><button type="button" aria-label={t("上一个匹配项")} onClick={() => find(text, true, false)}><ArrowUp size={14} /></button>
    <button type="submit" aria-label={t("下一个匹配项")}><ArrowDown size={14} /></button><button type="button" aria-label={t("关闭")} onClick={close}><X size={14} /></button>
  </form>;
}

export function BrowserDownloads({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const [records, setRecords] = useState<BrowserDownload[]>([]);
  const [menu, setMenu] = useState<{ x: number; y: number; id: string } | null>(null);
  const menuRecord = menu ? records.find(record => record.id === menu.id) : undefined;
  const root = useRef<HTMLElement>(null);
  useEffect(() => {
    let active = true;
    let revision = 0;
    const unsubscribe = desktop()!.embeddedBrowser.onDownloadsChanged(records => { ++revision; setRecords(records); });
    const initialRevision = revision;
    void desktop()!.embeddedBrowser.downloads().then(records => { if (active && revision === initialRevision) setRecords(records); }).catch(error => pushToast(String(error), "error"));
    const outside = (event: PointerEvent) => { if (!root.current!.contains(event.target as Node) && !(event.target as Element).closest('[role="menu"]')) onClose(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { active = false; unsubscribe(); document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, []);
  const act = (action: "open-folder" | "open" | "reveal" | "remove", record?: BrowserDownload) => {
    void desktop()!.embeddedBrowser.downloadAction({ action, id: record?.id }).then(error => {
      if (error) pushToast(error, "error");
      else if (action === "remove") setRecords(current => current.filter(item => item.id !== record!.id));
    }).catch(error => pushToast(String(error), "error"));
  };
  return <section ref={root} role="dialog" className="mc-browser-downloads" aria-label={t("下载")}>
    <div><strong>{t("下载")}</strong><button type="button" aria-label={t("打开文件夹")} onClick={() => act("open-folder")}><FolderOpen size={16} /></button></div>
    {records.length ? records.map(record => <div key={record.id} className="mc-browser-download-row">
      <File size={20} /><button className="mc-browser-download-file" type="button" onClick={() => act("open", record)} disabled={record.state !== "completed" || !record.fileExists}>
        {record.filename}<small>{record.state === "completed" ? t("已下载") : record.state} · {record.receivedBytes} {t("字节")} · {new Date(record.startedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small>
      </button><button type="button" aria-label={t("在文件资源管理器中显示")} disabled={!record.fileExists} onClick={() => act("reveal", record)}><FolderOpen size={16} /></button>
      <button type="button" aria-label={`${record.filename} ${t("下载操作")}`} onClick={event => { const bounds = event.currentTarget.getBoundingClientRect(); setMenu({ x: bounds.right - 210, y: bounds.bottom + 4, id: record.id }); }}><MoreHorizontal size={16} /></button>
    </div>) : <p>{t("没有下载内容")}</p>}
    {menu && menuRecord && <ContextMenu position={menu} onClose={() => setMenu(null)} ariaLabel={t("下载操作")} items={[
      { label: t("打开"), disabled: menuRecord.state !== "completed" || !menuRecord.fileExists, onClick: () => act("open", menuRecord) },
      { label: t("在文件资源管理器中显示"), disabled: !menuRecord.fileExists, onClick: () => act("reveal", menuRecord) },
      { label: t("复制地址"), onClick: () => { void copyText(menuRecord.url, t("地址")); } },
      { label: t("复制路径"), onClick: () => { void copyText(menuRecord.path, t("路径")); } },
      { label: "", separator: true },
      { label: t("从列表中移除"), disabled: menuRecord.canCancel, onClick: () => act("remove", menuRecord) },
    ]} />}
  </section>;
}
