import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight, Globe2, Search, X } from "../lib/icons";
import { desktop, type BrowserSiteCategory, type BrowserSiteDecision, type BrowserSitePolicy } from "../desktop/runtime";
import { SelectMenu } from "../components/SelectMenu";
import { useI18n } from "../i18n";

const CATEGORY_LABELS = { camera: "摄像头", microphone: "麦克风", geolocation: "位置", notifications: "通知", "clipboard-read": "剪贴板",
  mediaKeySystem: "受保护内容 ID", sound: "声音", images: "图片", javascript: "JavaScript", usb: "USB 设备" } as const;
const DECISION_LABELS = { ask: "询问", allow: "允许", block: "阻止" } as const;

export function BrowserSiteSettings({ active, breadcrumb }: { active: boolean; breadcrumb: (current: string) => ReactNode }) {
  const { t } = useI18n();
  const [policy, setPolicy] = useState<BrowserSitePolicy>();
  const [category, setCategory] = useState<BrowserSiteCategory | null>(null);
  const [origin, setOrigin] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const revision = useRef(0);
  useEffect(() => {
    if (!active) return;
    let mounted = true;
    const read = () => {
      const current = ++revision.current;
      void desktop()!.embeddedBrowser.getSitePolicy().then(next => { if (mounted && current === revision.current) setPolicy(next); }).catch(reason => { if (mounted) setError(String(reason)); });
    };
    read();
    const unsubscribe = desktop()!.embeddedBrowser.onSettingsChanged(read);
    return () => { mounted = false; unsubscribe(); };
  }, [active]);
  const commit = async (operation: () => Promise<BrowserSitePolicy>) => {
    setBusy(true); setError("");
    try { const next = await operation(); ++revision.current; setPolicy(next); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const setDecision = (id: BrowserSiteCategory, setting: BrowserSiteDecision | "default", site?: string) => void commit(() => desktop()!.embeddedBrowser.setSitePolicy({ ...(site ? { origin: site } : {}), category: id, setting }));
  const definitions = policy?.categories ?? [];
  const decisionMenu = (id: BrowserSiteCategory, site?: string) => {
    const definition = definitions.find(item => item.id === id)!;
    const value = site ? policy!.sites.find(item => item.origin === site)?.settings[id] ?? "default" : policy!.defaults[id];
    return <SelectMenu ariaLabel={t(CATEGORY_LABELS[id])} value={value} onValueChange={setting => setDecision(id, setting as BrowserSiteDecision | "default", site)} disabled={busy} className="settings-select settings-select-regular" align="end">
      {site && <option value="default">{t("默认设置：{setting}", { setting: t(DECISION_LABELS[policy!.defaults[id]]) })}</option>}
      {(site ? definition.allowedSettings : definition.defaultSettings).map(setting => <option key={setting} value={setting}>{t(DECISION_LABELS[setting])}</option>)}
    </SelectMenu>;
  };
  const visibleSites = (policy?.sites ?? []).filter(site => (!category || site.settings[category] !== undefined) && site.origin.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <section className="settings-browser-personal-data" aria-label={t("网站设置")}>
    {breadcrumb(t("网站设置"))}
    {(category || origin) && <nav className="settings-browser-breadcrumb"><button type="button" onClick={() => { setOrigin(""); setCategory(null); setQuery(""); }}>{t("网站设置")}</button><ChevronRight size={14} />{category && <><button type="button" onClick={() => { setOrigin(""); setQuery(""); }}>{t(CATEGORY_LABELS[category])}</button>{origin && <ChevronRight size={14} />}</>}{origin && <span aria-current="page">{origin}</span>}</nav>}
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{origin || (category ? t(CATEGORY_LABELS[category]) : t("网站设置"))}</h2></div></header>
    {error && <p role="alert">{error}</p>}
    {!policy ? !error && <p role="status">{t("正在加载…")}</p> : <>
      {(origin || category === "javascript" || category === "images") && <p className="settings-page-note">{t("更改 JavaScript 或图片权限将在重新加载页面后生效。")}</p>}
      {origin ? <>
        <div className="settings-card">{definitions.map(item => <div className="settings-row" key={item.id}><div className="settings-row-title">{t(CATEGORY_LABELS[item.id])}</div>{decisionMenu(item.id, origin)}</div>)}</div>
        <button type="button" className="settings-action-button" disabled={busy} onClick={() => void commit(async () => { await desktop()!.embeddedBrowser.removeSitePermissions({ origin }); return desktop()!.embeddedBrowser.getSitePolicy(); })}>{t("重置权限")}</button>
      </> : <>
        {category ? <div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("默认行为")}</div></div>{decisionMenu(category)}</div></div> : <div className="settings-card">{definitions.map(item => <button type="button" className="settings-row settings-browser-site-row" key={item.id} aria-label={`${t(CATEGORY_LABELS[item.id])} ${t(DECISION_LABELS[policy.defaults[item.id]])}`} onClick={() => { setCategory(item.id); setQuery(""); }}><span>{t(CATEGORY_LABELS[item.id])}</span><span className="settings-browser-policy-summary">{t(DECISION_LABELS[policy.defaults[item.id]])}</span><ChevronRight size={16} /></button>)}</div>}
        <label className="settings-search settings-browser-history-search"><Search size={16} /><input aria-label={t("搜索网站权限")} placeholder={t("搜索网站权限")} value={query} onChange={event => setQuery(event.target.value)} /></label>
        <h3 className="settings-group-title">{t("已保存的网站权限")}</h3>
        {visibleSites.length ? <div className="settings-card">{visibleSites.map(site => <button type="button" className="settings-row settings-browser-site-row" key={site.origin} aria-label={category ? `${site.origin} ${t(DECISION_LABELS[site.settings[category]!])}` : site.origin} onClick={() => setOrigin(site.origin)}><Globe2 size={18} /><span>{site.origin}</span>{category && <span className="settings-browser-policy-summary">{t(DECISION_LABELS[site.settings[category]!])}</span>}<ChevronRight size={16} /></button>)}</div> : <p>{t("尚未保存网站权限")}</p>}
      </>}
      {(category === "usb" || origin && policy.devices.some(device => device.origin === origin)) && <><h3 className="settings-group-title">{t("已连接的 USB 设备")}</h3><p className="settings-page-note">{t("移除授权后，此网站将无法再次访问该设备。已打开的连接会在页面关闭后结束。")}</p><div className="settings-card">{policy.devices.filter(device => !origin || device.origin === origin).map(device => <div className="settings-row settings-browser-personal-row" key={device.id}><div className="settings-row-copy"><div className="settings-row-title">{device.name}</div><div className="settings-row-description">{device.origin}</div></div><button type="button" className="settings-browser-history-actions" disabled={busy} aria-label={t("移除设备授权 {name}", { name: device.name })} onClick={() => void commit(() => desktop()!.embeddedBrowser.removeSiteDevice({ id: device.id }))}><X size={16} /></button></div>)}</div></>}
    </>}
  </section>;
}
