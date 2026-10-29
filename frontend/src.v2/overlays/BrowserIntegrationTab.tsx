import { useI18n } from "../i18n";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ChevronDown, ChevronRight, File, FolderOpen, Globe2, ListFilter, MoreHorizontal, Pause, Play, Search, Trash2, X } from "lucide-react";
import { browserHistoryRemove, browserHistorySearch, desktop, embeddedBrowserGetSettings, embeddedBrowserSetSettings, isDesktop, onEmbeddedBrowserSettingsChanged, type BrowserDownload, type BrowserHistoryVisit, type EmbeddedBrowserSettings, type ImportableBrowserProfile } from "../desktop/runtime";
import { openRightPanelFromSettings } from "../lib/settings-navigation";
import { pushToast } from "./ToastContainer";
import { useAppStore } from "../stores";
import { SelectMenu } from "../components/SelectMenu";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { openWebInBrowser } from "../chat/openWebInBrowser";
import { ContextMenu } from "../components/ContextMenu";
import { clearBrowserData } from "../lib/browser-data";
import { useFocusTrap } from "../hooks/useFocusTrap";
import "./BrowserIntegrationTab.css";
import { BrowserPersonalDataSettings } from "./BrowserPersonalDataSettings";

function BrowserControlIcon() {
  return <svg width="40" height="40" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <path fillRule="evenodd" clipRule="evenodd" d="M9.62561 8.79541L9.77112 8.82764L14.1774 10.1499C15.1066 10.4288 15.1641 11.7231 14.2633 12.0835L12.4684 12.8013L11.7506 14.5962C11.3903 15.4971 10.096 15.4395 9.81702 14.5103L8.49475 10.104C8.27544 9.37127 8.90238 8.68518 9.62561 8.79541ZM10.8014 14.1382L11.4977 12.4009L11.5416 12.3062C11.6544 12.0911 11.8405 11.9217 12.068 11.8306L13.8053 11.1343L9.51331 9.84619L10.8014 14.1382Z" fill="currentColor" />
    <path d="M9.93323 2.14111C10.6945 2.14111 11.297 2.14156 11.7819 2.18115C12.2729 2.22127 12.6895 2.30471 13.0699 2.49854C13.6861 2.81247 14.1876 3.3131 14.5016 3.9292C14.6954 4.3096 14.7788 4.72637 14.819 5.21729C14.8586 5.70219 14.858 6.30549 14.858 7.06689V7.6665C14.858 7.95634 14.6234 8.19172 14.3336 8.19189C14.0437 8.19189 13.8082 7.95645 13.8082 7.6665V7.06689C13.8082 6.28823 13.8074 5.73544 13.7721 5.30322C13.7373 4.87739 13.6718 4.61446 13.566 4.40674C13.3527 3.98808 13.012 3.6474 12.5934 3.43408C12.3856 3.32822 12.1229 3.26187 11.6969 3.22705C11.2647 3.19174 10.712 3.19189 9.93323 3.19189H6.06702C5.28839 3.19189 4.73555 3.19175 4.30334 3.22705C3.87742 3.26185 3.6146 3.32829 3.40686 3.43408C2.9882 3.6474 2.64752 3.98808 2.4342 4.40674C2.32841 4.61448 2.26197 4.8773 2.22717 5.30322C2.19187 5.73543 2.19202 6.28826 2.19202 7.06689V8.99951C2.19202 9.74646 2.19168 10.2767 2.22424 10.6919C2.25636 11.1013 2.31809 11.3543 2.41565 11.5552C2.63356 12.0038 2.99542 12.3665 3.44397 12.5845C3.64482 12.682 3.89884 12.7428 4.30823 12.7749C4.72328 12.8074 5.25301 12.8081 5.99963 12.8081C6.28958 12.8081 6.52502 13.0435 6.52502 13.3335C6.52485 13.6233 6.28947 13.8579 5.99963 13.8579C5.26963 13.8579 4.69181 13.8583 4.2262 13.8218C3.75478 13.7848 3.35407 13.7076 2.98596 13.5288C2.32548 13.208 1.79118 12.6746 1.47034 12.0142C1.29156 11.6461 1.21435 11.2454 1.17737 10.7739C1.14084 10.3082 1.14124 9.72981 1.14124 8.99951V7.06689C1.14124 6.30548 1.14166 5.70219 1.18127 5.21729C1.2214 4.72638 1.30484 4.30959 1.49866 3.9292C1.81259 3.3133 2.31342 2.81246 2.92932 2.49854C3.30972 2.30471 3.72651 2.22128 4.21741 2.18115C4.70231 2.14153 5.30561 2.14111 6.06702 2.14111H9.93323Z" fill="currentColor" />
    <path d="M5.13803 4.65356C5.51358 4.65356 5.81772 4.9577 5.81772 5.33325C5.81771 5.7088 5.51358 6.01294 5.13803 6.01294C4.76259 6.01281 4.45835 5.70872 4.45834 5.33325C4.45834 4.95778 4.76258 4.65369 5.13803 4.65356Z" fill="currentColor" />
    <path d="M10.6667 4.7994C10.9612 4.7994 11.2005 5.0387 11.2005 5.33325C11.2005 5.6278 10.9612 5.86711 10.6667 5.86711H8.00001C7.70546 5.86711 7.46616 5.6278 7.46616 5.33325C7.46616 5.0387 7.70546 4.7994 8.00001 4.7994H10.6667Z" fill="currentColor" />
  </svg>;
}

export const BrowserIntegrationTab = ({ active = true }: { active?: boolean }) => {
  const { t } = useI18n();
  const { preferences, error: preferencesError, update } = useDesktopPreferences();
  const section = useAppStore(state => state.browserSettingsSection);
  const setSection = useAppStore(state => state.setBrowserSettingsSection);
  const [importOpen, setImportOpen] = useState(false);
  const [browserSettings, setBrowserSettings] = useState<EmbeddedBrowserSettings>();
  const [browserError, setBrowserError] = useState("");
  const [saving, setSaving] = useState(false);
  const settingsRevision = useRef(0);
  useEffect(() => {
    if (!active || !isDesktop()) return;
    let mounted = true;
    const revision = settingsRevision.current;
    void embeddedBrowserGetSettings("")!.then(settings => { if (mounted && settingsRevision.current === revision) setBrowserSettings(settings); }).catch(error => { if (mounted) setBrowserError(String(error)); });
    const unsubscribe = onEmbeddedBrowserSettingsChanged(settings => { settingsRevision.current++; setBrowserSettings({ ...settings, origin: "", permissions: [] }); });
    return () => { mounted = false; unsubscribe?.(); };
  }, [active]);
  const updateBrowserEnabled = async () => {
    setSaving(true); setBrowserError("");
    const revision = settingsRevision.current;
    try { const saved = await embeddedBrowserSetSettings({ browserEnabled: !browserSettings!.browserEnabled }); if (settingsRevision.current === revision) setBrowserSettings(saved); }
    catch (error) { setBrowserError(String(error)); }
    finally { setSaving(false); }
  };
  const updateDownloadSettings = async (patch: { downloadDirectory?: string | null; downloadPolicy?: "ask" | "allow" }) => {
    setSaving(true); setBrowserError("");
    const revision = settingsRevision.current;
    try { const saved = await embeddedBrowserSetSettings(patch); if (settingsRevision.current === revision) setBrowserSettings(saved); }
    catch (error) { setBrowserError(String(error)); }
    finally { setSaving(false); }
  };
  const chooseDownloadDirectory = async () => {
    setSaving(true); setBrowserError("");
    try { const directory = await desktop()!.pickDirectory(); if (directory !== null) await updateDownloadSettings({ downloadDirectory: directory }); }
    catch (error) { setBrowserError(String(error)); }
    finally { setSaving(false); }
  };
  if (section === "history") return <BrowserHistorySettings active={active} onBack={() => setSection("general")} onClear={clearBrowserData} />;
  if (section === "downloads") return <BrowserDownloadHistory active={active} onBack={() => setSection("general")} />;
  if (section === "passwords" || section === "contacts" || section === "sites") return <BrowserPersonalDataSettings key={section} section={section} active={active} breadcrumb={current => <BrowserSettingsBreadcrumb current={current} onBack={() => setSection("general")} />} />;
  return <div className="settings-browser-settings">
    {preferencesError && <p role="alert">{preferencesError}</p>}
    {browserError && <p role="alert">{browserError}</p>}
    {browserSettings && <div className="settings-card settings-browser-control-card"><div className="settings-row"><div className="settings-row-copy"><BrowserControlIcon /><div><div className="settings-row-title">{t("浏览器")}</div><div className="settings-row-description">{t("让 MiniCode 控制内置浏览器")}</div></div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("让 MiniCode 控制内置浏览器")} aria-checked={browserSettings.browserEnabled} data-active={browserSettings.browserEnabled} disabled={saving} onClick={() => void updateBrowserEnabled()}><span /></button></div></div>}
    <section className="settings-group settings-browser-group">
      <div className="settings-browser-group-heading"><h3 className="settings-group-title">{t("常规")}</h3>{isDesktop() && <button type="button" className="settings-action-button" onClick={() => setImportOpen(true)}>{t("导入…")}</button>}</div>
      <div className="settings-card">
        {preferences && <>
          {([['urlOpenTarget', t("网页 URL 和链接打开位置"), undefined], ['localUrlOpenTarget', t("本地 URL 打开位置"), t("本地开发站点默认打开的位置。")]] as const).map(([key, label, description]) => <div key={key} className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{label}</div>{description && <div className="settings-row-description">{description}</div>}</div><SelectMenu ariaLabel={label} className="settings-select settings-select-regular" align="end" value={preferences[key]} onValueChange={value => void update({ [key]: value })}><option value="external-browser">{t("默认浏览器")}</option><option value="in-app-browser">MiniCode</option></SelectMenu></div>)}
          <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("显示完整网址")}</div><div className="settings-row-description">{t("在地址栏中显示路径、查询参数和片段。")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("显示完整网址")} aria-checked={preferences.showFullUrl} data-active={preferences.showFullUrl} onClick={() => void update({ showFullUrl: !preferences.showFullUrl })}><span /></button></div>
        </>}
        <BrowserDataSettings />
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("浏览历史")}</div><div className="settings-row-description">{t("查看和管理在内置浏览器中访问过的页面。")}</div></div><button type="button" className="settings-action-button" aria-label={t("管理浏览历史")} onClick={() => setSection("history")}>{t("管理")}</button></div>
        {preferences && <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("批注截图")}</div><div className="settings-row-description">{t("截图帮助模型理解批注内容，并计入上下文用量。")}</div></div><SelectMenu ariaLabel={t("批注截图")} className="settings-select settings-select-regular" align="end" value={preferences.annotationScreenshotsMode} onValueChange={value => void update({ annotationScreenshotsMode: value as typeof preferences.annotationScreenshotsMode })}><option value="always">{t("始终包含")}</option><option value="necessary">{t("仅拖选区域时")}</option></SelectMenu></div>}
      </div>
    </section>
    {isDesktop() && <section className="settings-group settings-browser-group"><h3 className="settings-group-title">{t("自动填充和密码")}</h3><div className="settings-card">
      {([["passwords", "密码管理器", "添加、删除和编辑已保存的密码"], ["contacts", "联系信息", "添加、删除和编辑已保存的地址、电话号码及电子邮箱"]] as const).map(([section, title, description]) => <div className="settings-row" key={section}><div className="settings-row-copy"><div className="settings-row-title">{t(title)}</div><div className="settings-row-description">{t(description)}</div></div><button type="button" className="settings-action-button" aria-label={t("管理{title}", { title: t(title) })} onClick={() => setSection(section)}>{t("管理")}</button></div>)}
    </div></section>}
    {isDesktop() && <section className="settings-group settings-browser-group"><h3 className="settings-group-title">{t("下载")}</h3><div className="settings-card">
      {browserSettings && <>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("位置")}</div><div className="settings-row-description">{browserSettings.downloadDirectory ? <span className="settings-path-description">{browserSettings.downloadDirectory}</span> : t("系统下载文件夹")}</div></div><div className="settings-row-control"><button type="button" className="settings-action-button" disabled={saving} onClick={() => void chooseDownloadDirectory()}>{t("更改")}</button>{browserSettings.downloadDirectory != null && <button type="button" className="settings-action-button" disabled={saving} onClick={() => void updateDownloadSettings({ downloadDirectory: null })}>{t("重置")}</button>}</div></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("询问下载文件的保存位置")}</div><div className="settings-row-description">{t("下载内置浏览器中的文件时显示保存对话框。")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("询问下载文件的保存位置")} aria-checked={browserSettings.downloadPolicy === "ask"} data-active={browserSettings.downloadPolicy === "ask"} disabled={saving || browserSettings.downloadPolicy === "block"} onClick={() => void updateDownloadSettings({ downloadPolicy: browserSettings.downloadPolicy === "ask" ? "allow" : "ask" })}><span /></button></div>
      </>}
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("下载记录")}</div><div className="settings-row-description">{t("查看和管理内置浏览器下载的文件。")}</div></div><button type="button" className="settings-action-button" aria-label={t("管理下载记录")} onClick={() => setSection("downloads")}>{t("管理")}</button></div></div></section>}
    {isDesktop() && <section className="settings-group settings-browser-group"><h3 className="settings-group-title">{t("网站权限")}</h3><div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("网站设置")}</div><div className="settings-row-description">{t("控制内置浏览器中的摄像头、麦克风及其他权限")}</div></div><button type="button" className="settings-action-button" aria-label={t("管理网站设置")} onClick={() => setSection("sites")}>{t("管理")}</button></div></div></section>}
    {active && importOpen && <BrowserProfileImportDialog onClose={() => setImportOpen(false)} />}
  </div>;
};

function BrowserProfileImportDialog({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const [profiles, setProfiles] = useState<ImportableBrowserProfile[]>([]);
  const [profilePath, setProfilePath] = useState("");
  const [importHistory, setImportHistory] = useState(true);
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState("");
  const dialog = useFocusTrap(true);
  useEffect(() => {
    let mounted = true;
    void desktop()!.browserProfileImport.listImportableBrowserProfiles().then(items => {
      if (mounted) { setProfiles(items); setProfilePath(items[0]?.profilePath ?? ""); }
    }).catch(reason => { if (mounted) setError(String(reason)); }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, []);
  const selectedProfile = profiles.find(profile => profile.profilePath === profilePath);
  const importData = async () => {
    setImporting(true); setError("");
    try {
      const result = await desktop()!.browserProfileImport.importBrowserProfile({ source: selectedProfile!.source, profilePath,
        importHistory: true, importCookies: false, importPasswords: false, importExtensions: false });
      pushToast(t("已导入 {count} 条历史记录", { count: result.history.imported }), "success");
      onClose();
    } catch (reason) { setError(String(reason)); }
    finally { setImporting(false); }
  };
  return createPortal(<div className="settings-browser-import-backdrop" onClick={() => { if (!importing) onClose(); }}><div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="browser-profile-import-title" aria-describedby="browser-profile-import-description" tabIndex={-1} className="settings-browser-import-dialog" onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!importing) onClose(); } }}>
    <header><h2 id="browser-profile-import-title">{t("从浏览器导入")}</h2><button type="button" aria-label={t("关闭")} disabled={importing} onClick={onClose}><X size={18} /></button><p id="browser-profile-import-description">{t("选择要导入到内置浏览器的数据")}</p></header>
    {loading ? <p role="status">{t("正在查找浏览器个人资料…")}</p> : profiles.length === 0 ? !error && <p>{t("未找到可导入的浏览器个人资料")}</p> : <>
      <div className="settings-browser-import-source"><span>{t("从")}</span><SelectMenu ariaLabel={t("浏览器个人资料")} value={profilePath} onValueChange={setProfilePath} disabled={importing} className="settings-select settings-select-regular">{profiles.map(profile => <option key={profile.profilePath} value={profile.profilePath}>{profile.appName}　{profile.profileName}</option>)}</SelectMenu></div>
      <p className="settings-browser-import-note">{t("导入前，请完全关闭 Google Chrome")}</p>
      <div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("浏览记录")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("浏览记录")} aria-checked={importHistory} data-active={importHistory} disabled={importing} onClick={() => setImportHistory(current => !current)}><span /></button></div></div>
    </>}
    {error && <p role="alert">{error}</p>}
    <footer><button type="button" className="settings-action-button" disabled={importing} onClick={onClose}>{t("取消")}</button><button type="button" className="settings-action-button" data-primary="true" disabled={loading || importing || !selectedProfile || !importHistory} onClick={() => void importData()}>{importing ? t("导入中…") : t("导入")}</button></footer>
  </div></div>, document.body);
}

function BrowserDataSettings() {
  const { t } = useI18n();
  const [clearing, setClearing] = useState(false);
  const clear = async () => {
    setClearing(true);
    try { await clearBrowserData(); }
    catch (error) { pushToast(String(error), "error"); }
    finally { setClearing(false); }
  };
  return <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("浏览数据")}</div><div className="settings-row-description">{t("清除内置浏览器的历史记录、网站数据、缓存和下载记录。")}</div></div><button type="button" className="settings-action-button" disabled={clearing} onClick={() => void clear()}>{t("清除浏览数据")}</button></div>;
}

function BrowserSettingsBreadcrumb({ current, onBack }: { current: string; onBack: () => void }) {
  const { t } = useI18n();
  return <nav className="settings-browser-breadcrumb" aria-label={t("设置导航")}><button type="button" aria-label={t("返回浏览器设置")} onClick={onBack}><ArrowLeft size={16} /></button><button type="button" onClick={() => useAppStore.getState().setSettingsTab("general")}>{t("设置")}</button><ChevronRight size={14} aria-hidden="true" /><button type="button" onClick={onBack}>{t("浏览器")}</button><ChevronRight size={14} aria-hidden="true" /><span aria-current="page">{current}</span></nav>;
}

function BrowserHistorySettings({ active, onBack, onClear }: { active: boolean; onBack: () => void; onClear: () => Promise<boolean> }) {
  const { t, locale } = useI18n();
  const conversationId = useAppStore((state) => state.conversationId);
  const pendingOwner = useAppStore((state) => state.pendingConversationSwitchId);
  const [visits, setVisits] = useState<BrowserHistoryVisit[]>([]);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<"all" | "agent" | "other">("all");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [removing, setRemoving] = useState(false);
  const [expandedDays, setExpandedDays] = useState<Set<string> | null>(null);
  const [menu, setMenu] = useState<{ position: { x: number; y: number }; visit?: BrowserHistoryVisit } | null>(null);
  useEffect(() => {
    if (!active || pendingOwner) return;
    let mounted = true;
    setLoading(true);
    setError("");
    void browserHistorySearch({ text: query, source }).then((items) => { if (mounted) { setVisits(items); setLoading(false); } }).catch((reason) => { if (mounted) { setError(String(reason)); setLoading(false); } });
    return () => { mounted = false; };
  }, [active, query, source, conversationId, pendingOwner, revision]);
  const removeEntries = async (ids: string[]) => {
    setRemoving(true);
    try {
      await browserHistoryRemove(ids);
      setSelected(new Set());
      setRevision((current) => current + 1);
    } catch (reason) { setError(String(reason)); }
    finally { setRemoving(false); }
  };
  const groups = new Map<string, BrowserHistoryVisit[]>();
  for (const visit of visits) {
    const day = new Date(visit.visitTime).toDateString();
    const entries = groups.get(day);
    if (entries) entries.push(visit); else groups.set(day, [visit]);
  }
  const openVisit = (visit: BrowserHistoryVisit) => {
    if (openWebInBrowser(visit.url)) openRightPanelFromSettings("browser");
    else pushToast(t("请先选择一个对话，再打开内置浏览器页面。"), "error");
  };
  return <section className="settings-browser-history" aria-label={t("浏览历史")}>
    <BrowserSettingsBreadcrumb current={t("浏览历史")} onBack={onBack} />
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{t("浏览历史")}</h2></div></header>
    <div className="settings-search settings-browser-history-search"><Search size={16} aria-hidden="true" /><input aria-label={t("搜索浏览历史")} placeholder={t("搜索浏览历史")} value={query} onChange={(event) => setQuery(event.target.value)} />
      <button type="button" className="settings-browser-history-filter" disabled={removing} aria-label={t("筛选浏览历史")} aria-haspopup="menu" aria-expanded={Boolean(menu && !menu.visit)} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setMenu({ position: { x: rect.right - 220, y: rect.bottom + 6 } }); }}><ListFilter size={16} />{source !== "all" && <span>{source === "agent" ? "Agent" : t("其他")}</span>}</button>
    </div>
    <div className="settings-browser-history-section-heading"><h3>{t("全部历史记录")}</h3><div>
      {selected.size > 0 && <button type="button" className="settings-action-button" disabled={removing} onClick={() => void removeEntries([...selected])}>{t("移除所选")}</button>}
      <button type="button" className="settings-action-button" disabled={removing} onClick={() => void onClear().then((cleared) => { if (cleared) { setSelected(new Set()); setRevision(current => current + 1); } }).catch(reason => setError(String(reason)))}>{t("清除浏览数据")}</button>
    </div></div>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">{t("正在加载浏览历史…")}</p> : visits.length === 0 ? <p>{t("没有匹配的浏览历史")}</p> : <div className="settings-browser-history-days">
      {[...groups].map(([day, entries], index) => { const expanded = expandedDays?.has(day) ?? index === 0; return <div key={day} className="settings-card settings-browser-history-day">
        <button type="button" className="settings-browser-history-day-toggle" aria-expanded={expanded} aria-controls={`browser-history-day-${entries[0].id}`} onClick={() => setExpandedDays((current) => { const next = new Set(current ?? [groups.keys().next().value!]); if (next.has(day)) next.delete(day); else next.add(day); return next; })}><span>{new Date(entries[0].visitTime).toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" })}</span><ChevronDown size={14} data-expanded={expanded} /></button>
        <div id={`browser-history-day-${entries[0].id}`} hidden={!expanded}>{entries.map((visit) => <div key={visit.id} className="settings-row settings-browser-history-row">
        <div className="settings-browser-history-row-icons">
        <input type="checkbox" disabled={removing} aria-label={t("选择 {title}", { title: visit.title || visit.url })} checked={selected.has(visit.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(visit.id); else next.delete(visit.id); return next; })} />
        <Globe2 size={16} aria-hidden="true" /></div>
        <button type="button" disabled={removing} className="settings-browser-history-page" title={visit.url} onClick={() => openVisit(visit)}><span>{visit.title || new URL(visit.url).hostname}</span><span>{new URL(visit.url).hostname}</span></button>
        <div className="settings-browser-history-row-trailing">
        {visit.source === "agent" && <span className="settings-state-pill">Agent</span>}
        <time dateTime={new Date(visit.visitTime).toISOString()}>{new Date(visit.visitTime).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}</time>
        <button type="button" disabled={removing} className="settings-browser-history-actions" aria-label={t("页面操作：{title}", { title: visit.title || visit.url })} aria-haspopup="menu" aria-expanded={menu?.visit?.id === visit.id} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setMenu({ position: { x: rect.right - 220, y: rect.bottom + 6 }, visit }); }}><MoreHorizontal size={16} /></button>
        </div>
      </div>)}</div></div>; })}
    </div>}
    {menu && <ContextMenu position={menu.position} onClose={() => setMenu(null)} items={menu.visit ? [
      { label: t("打开页面"), icon: <Globe2 size={15} />, onClick: () => openVisit(menu.visit!) },
      { label: t("从历史记录移除"), icon: <Trash2 size={15} />, onClick: () => void removeEntries([menu.visit!.id]) },
    ] : (["all", "agent", "other"] as const).map((value) => ({ label: value === "agent" ? "Agent" : value === "other" ? t("其他") : t("全部"), checked: source === value, onClick: () => { setSource(value); setSelected(new Set()); setExpandedDays(null); } }))} />}
  </section>;
}

function BrowserDownloadHistory({ active, onBack }: { active: boolean; onBack: () => void }) {
  const { t, locale } = useI18n();
  const [records, setRecords] = useState<BrowserDownload[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expandedDays, setExpandedDays] = useState<Set<string> | null>(null);
  const recordsRevision = useRef(0);
  useEffect(() => {
    if (!active) return;
    let mounted = true;
    const revision = recordsRevision.current;
    setLoading(true);
    void desktop()!.embeddedBrowser.downloads().then(items => {
      if (mounted && recordsRevision.current === revision) setRecords(items);
    }).catch(reason => { if (mounted) setError(String(reason)); }).finally(() => { if (mounted) setLoading(false); });
    const unsubscribe = desktop()!.embeddedBrowser.onDownloadsChanged(items => { recordsRevision.current++; setRecords(items); setLoading(false); });
    return () => { mounted = false; unsubscribe(); };
  }, [active]);
  const downloadAction = async (action: "open" | "reveal" | "remove" | "pause" | "resume" | "cancel", record: BrowserDownload) => {
    const result = await desktop()!.embeddedBrowser.downloadAction({ action, id: record.id });
    if (result) throw new Error(result);
  };
  const act = async (action: "open" | "reveal" | "remove" | "pause" | "resume" | "cancel", record: BrowserDownload) => {
    setBusy(true); setError("");
    try { await downloadAction(action, record); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const terminalRecords = records.filter(record => !record.canCancel && ["completed", "cancelled", "interrupted"].includes(record.state));
  const clearAll = async () => {
    setBusy(true); setError("");
    try { for (const record of terminalRecords) await downloadAction("remove", record); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const groups = new Map<string, BrowserDownload[]>();
  const matching = records.filter(record => `${record.filename} ${record.url}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).sort((a, b) => b.startedAt - a.startedAt);
  for (const record of matching) {
    const day = new Date(record.startedAt).toDateString();
    const entries = groups.get(day);
    if (entries) entries.push(record); else groups.set(day, [record]);
  }
  return <section className="settings-browser-history settings-browser-download-history" aria-label={t("下载记录")}>
    <BrowserSettingsBreadcrumb current={t("下载记录")} onBack={onBack} />
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{t("下载记录")}</h2></div></header>
    <label className="settings-search settings-browser-history-search"><Search size={16} aria-hidden="true" /><input aria-label={t("搜索下载记录")} placeholder={t("搜索下载记录")} value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className="settings-browser-history-section-heading"><h3>{t("全部历史记录")}</h3><button type="button" className="settings-action-button" disabled={busy || terminalRecords.length === 0} onClick={() => void clearAll()}>{t("清除全部")}</button></div>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">{t("正在加载下载记录…")}</p> : matching.length === 0 ? <p>{query ? t("没有匹配的下载记录") : t("没有下载内容")}</p> : <div className="settings-browser-history-days">{[...groups].map(([day, entries], index) => {
      const expanded = expandedDays?.has(day) ?? index === 0;
      return <div className="settings-card settings-browser-history-day" key={day}><button type="button" className="settings-browser-history-day-toggle" aria-expanded={expanded} aria-controls={`browser-download-day-${entries[0].id}`} onClick={() => setExpandedDays(current => { const next = new Set(current ?? [groups.keys().next().value!]); next.has(day) ? next.delete(day) : next.add(day); return next; })}><span>{new Date(entries[0].startedAt).toLocaleDateString(locale, { day: "numeric", month: "long", year: "numeric" })}</span><ChevronDown size={14} data-expanded={expanded} /></button><div id={`browser-download-day-${entries[0].id}`} hidden={!expanded}>{entries.map(record => <div className="settings-row settings-browser-download-row" key={record.id}>
        <File size={18} aria-hidden="true" /><button type="button" className="settings-browser-download-file" disabled={busy || record.state !== "completed" || !record.fileExists} title={record.path} onClick={() => void act("open", record)}><span style={record.state === "completed" && !record.fileExists ? { textDecoration: "line-through" } : undefined}>{record.filename}</span>{record.state === "completed" && !record.fileExists && <small>{t("已删除")}</small>}{record.state !== "completed" && <small>{record.state === "progressing" ? `${record.receivedBytes} / ${record.totalBytes} ${t("字节")}` : record.state === "paused" ? t("已暂停") : record.state === "cancelled" ? t("已取消") : record.state === "interrupted" ? t("已中断") : record.state}</small>}</button><time dateTime={new Date(record.startedAt).toISOString()}>{new Date(record.startedAt).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}</time>
        <div className="settings-browser-download-actions">{(record.canPause || record.canResume) && <button type="button" className="settings-browser-history-actions" disabled={busy} aria-label={t(record.canResume ? "继续下载" : "暂停下载")} onClick={() => void act(record.canResume ? "resume" : "pause", record)}>{record.canResume ? <Play size={16} /> : <Pause size={16} />}</button>}
        {record.fileExists && <button type="button" className="settings-browser-history-actions" disabled={busy} aria-label={t("显示下载文件 {filename}", { filename: record.filename })} onClick={() => void act("reveal", record)}><FolderOpen size={16} /></button>}
        {record.canCancel ? <button type="button" className="settings-browser-history-actions" disabled={busy} aria-label={t("取消下载")} onClick={() => void act("cancel", record)}><X size={16} /></button>
          : <button type="button" className="settings-browser-history-actions" disabled={busy || !terminalRecords.includes(record)} aria-label={t("移除下载记录 {filename}", { filename: record.filename })} onClick={() => void act("remove", record)}><X size={16} /></button>}</div>
      </div>)}</div></div>;
    })}</div>}
  </section>;
}
