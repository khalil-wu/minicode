import { useI18n } from "../i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Download, Globe2, ListFilter, MoreHorizontal, PanelRightOpen, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { browserHistoryRemove, browserHistorySearch, embeddedBrowserClearBrowsingData, embeddedBrowserGetSettings, embeddedBrowserList, embeddedBrowserSetSettings, isDesktop, onEmbeddedBrowserSettingsChanged, type BrowserHistoryVisit, type EmbeddedBrowserSettings, type EmbeddedBrowserState } from "../desktop/runtime";
import { openRightPanelFromSettings } from "../lib/settings-navigation";
import { pushToast } from "./ToastContainer";
import { useAppStore } from "../stores";
import { SelectMenu } from "../components/SelectMenu";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { showConfirm } from "./DialogService";
import { openWebInBrowser } from "../chat/openWebInBrowser";
import { ContextMenu } from "../components/ContextMenu";

export const BrowserIntegrationTab = ({ active = true }: { active?: boolean }) => {
  const { t } = useI18n();
  const { preferences, error: preferencesError, update } = useDesktopPreferences();
  const [historyOpen, setHistoryOpen] = useState(false);
  const conversationId = useAppStore((state) => state.conversationId) || "";
  const [tabs, setTabs] = useState<EmbeddedBrowserState[]>([]);
  const [downloadPolicy, setDownloadPolicy] = useState<EmbeddedBrowserSettings["downloadPolicy"]>("block");
  const [savingDownloadPolicy, setSavingDownloadPolicy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const loadSequenceRef = useRef(0);
  const policyRevisionRef = useRef(0);

  useEffect(() => {
    if (!isDesktop()) return;
    return onEmbeddedBrowserSettingsChanged((settings) => {
      policyRevisionRef.current += 1;
      setDownloadPolicy(settings.downloadPolicy);
    });
  }, []);

  const loadBrowserState = useCallback(async (showFeedback = false) => {
    const requestId = ++loadSequenceRef.current;
    const requestConversationId = conversationId;
    const policyRevision = policyRevisionRef.current;
    setLoading(true);
    setLoadError("");
    setTabs([]);
    try {
      const [items, settings] = await Promise.all([
        conversationId ? Promise.resolve(embeddedBrowserList(conversationId)) : Promise.resolve([]),
        isDesktop() ? Promise.resolve(embeddedBrowserGetSettings("")) : Promise.resolve(null),
      ]);
      if (requestId !== loadSequenceRef.current || (useAppStore.getState().conversationId || "") !== requestConversationId) return;
      setTabs(items ?? []);
      if (settings && policyRevision === policyRevisionRef.current) setDownloadPolicy(settings.downloadPolicy);
      if (showFeedback) pushToast("浏览器状态已刷新", "success");
    } catch (error) {
      if (requestId !== loadSequenceRef.current || (useAppStore.getState().conversationId || "") !== requestConversationId) return;
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      setTabs([]);
      setLoadError(message);
      if (showFeedback) pushToast(`浏览器状态刷新失败：${message}`, "error");
    } finally {
      if (requestId === loadSequenceRef.current) setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    if (active) void loadBrowserState();
    return () => { loadSequenceRef.current += 1; };
  }, [active, loadBrowserState]);

  const openBrowser = () => {
    openRightPanelFromSettings("browser");
  };

  const updateDownloadPolicy = async (policy: EmbeddedBrowserSettings["downloadPolicy"]) => {
    if (savingDownloadPolicy || policy === downloadPolicy) return;
    const previous = downloadPolicy;
    const revision = ++policyRevisionRef.current;
    setDownloadPolicy(policy);
    setSavingDownloadPolicy(true);
    try {
      const settings = await embeddedBrowserSetSettings({ downloadPolicy: policy });
      if (!settings) throw new Error("Browser settings are unavailable");
      if (policyRevisionRef.current === revision) setDownloadPolicy(settings.downloadPolicy);
      pushToast("浏览器下载策略已保存", "success");
    } catch (error) {
      if (policyRevisionRef.current === revision) setDownloadPolicy(previous);
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`浏览器下载设置保存失败：${message}`, "error");
    } finally {
      setSavingDownloadPolicy(false);
    }
  };

  const clearBrowsingData = async () => {
    if (!await showConfirm({ title: t("清除浏览数据"), message: t("清除内置浏览器的历史记录、站点数据、缓存和网站权限？"), confirmLabel: t("清除"), danger: true })) return false;
    try { await embeddedBrowserClearBrowsingData(); pushToast(t("浏览数据已清除"), "success"); return true; }
    catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); return false; }
  };

  if (historyOpen) return <BrowserHistorySettings active={active} onBack={() => setHistoryOpen(false)} onClear={clearBrowsingData} />;

  return (
    <>
      <section className="settings-group settings-browser-group">
        <h3 className="settings-group-title">{t("常规")}</h3>
        {preferencesError && <p role="alert">{preferencesError}</p>}
        <div className="settings-card">
          {preferences && <>
            {([['urlOpenTarget', t("网页 URL 和链接打开位置"), t("网页链接默认打开的位置。")], ['localUrlOpenTarget', t("本地 URL 打开位置"), t("本地开发站点默认打开的位置。")]] as const).map(([key, label, description]) => <div key={key} className="settings-row">
              <div className="settings-row-copy"><div className="settings-row-title">{label}</div><div className="settings-row-description">{description}</div></div>
              <SelectMenu ariaLabel={label} className="settings-select settings-select-regular" align="end" value={preferences[key]} onValueChange={(value) => void update({ [key]: value })}><option value="external-browser">{t("默认浏览器")}</option><option value="in-app-browser">MiniCode</option></SelectMenu>
            </div>)}
            <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("显示完整网址")}</div><div className="settings-row-description">{t("在地址栏中显示路径、查询参数和片段。")}</div></div>
              <button type="button" className="settings-toggle" role="switch" aria-label={t("显示完整网址")} aria-checked={preferences.showFullUrl} data-active={preferences.showFullUrl} onClick={() => void update({ showFullUrl: !preferences.showFullUrl })}><span /></button>
            </div>
            <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("浏览数据")}</div><div className="settings-row-description">{t("清除内置浏览器的浏览历史记录、网站数据、缓存和网站权限。")}</div></div><button type="button" className="settings-action-button" onClick={() => void clearBrowsingData()}>{t("清除浏览数据")}</button></div>
            <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("浏览历史")}</div><div className="settings-row-description">{t("查看和管理在内置浏览器中访问过的页面。")}</div></div><button type="button" className="settings-action-button" onClick={() => setHistoryOpen(true)}>{t("管理浏览历史")}</button></div>
            <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("批注截图")}</div><div className="settings-row-description">{t("截图帮助模型理解批注内容，并计入上下文用量。")}</div></div>
              <SelectMenu ariaLabel={t("批注截图")} className="settings-select settings-select-regular" align="end" value={preferences.annotationScreenshotsMode} onValueChange={(value) => void update({ annotationScreenshotsMode: value as typeof preferences.annotationScreenshotsMode })}><option value="always">{t("始终包含")}</option><option value="necessary">{t("仅拖选区域时")}</option></SelectMenu>
            </div>
          </>}
        </div>
      </section>
      <details className="settings-appearance-advanced"><summary className="settings-appearance-advanced-trigger">{t("高级")}<ChevronDown size={15} aria-hidden="true" /></summary><div className="settings-appearance-advanced-body">
      <section className="settings-group settings-browser-group">
        <h3 className="settings-group-title">{t("内置浏览器")}</h3>
        <p className="settings-section-description">{t("页面、控制台、网络和权限共用浏览器面板。")}</p>
        <div className="settings-browser-summary">
          <span className="settings-browser-summary-icon" aria-hidden="true"><Globe2 /></span>
          <div>
            <strong>{loading ? t("正在读取浏览器状态…") : tabs.length > 0 ? `${tabs.length} 个打开的页面` : t("浏览器工作区")}</strong>
            <span>{loadError ? `状态读取失败：${loadError}` : tabs.find((item) => item.active)?.title || tabs[0]?.title || t("当前没有打开的页面")}</span>
          </div>
          <div className="settings-browser-summary-actions">
          {loadError && (
            <button type="button" className="settings-action-button" disabled={loading} onClick={() => void loadBrowserState(true)}>
              <RefreshCw className={loading ? "settings-spin" : undefined} />{t("重试")}</button>
          )}
          <button type="button" className="settings-action-button" data-primary="true" onClick={openBrowser}>
            <PanelRightOpen />{t("打开浏览器")}</button>
          </div>
        </div>
      </section>
      <section className="settings-group settings-browser-group"><h3 className="settings-group-title">{t("浏览器工作区")}</h3><div className="settings-card">
          <div className="settings-row settings-browser-permission-row">
            <span className="settings-browser-row-icon" aria-hidden="true"><Download /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">{t("下载")}</div>
              <div className="settings-row-description">{t("控制内置浏览器下载文件。")}</div>
            </div>
            <div className="settings-row-control">
              <SelectMenu
                ariaLabel={t("浏览器下载策略")}
                className="settings-select settings-select-regular"
                align="end"
                value={downloadPolicy}
                disabled={!isDesktop() || savingDownloadPolicy}
                onValueChange={(value) => {
                  const policy = value as EmbeddedBrowserSettings["downloadPolicy"];
                  void updateDownloadPolicy(policy);
                }}
              >
                <option value="block">{t("阻止")}</option>
                <option value="ask">{t("每次询问")}</option>
                <option value="allow">{t("保存到下载目录")}</option>
              </SelectMenu>
            </div>
          </div>
          <div className="settings-row settings-browser-permission-row">
            <span className="settings-browser-row-icon" aria-hidden="true"><ShieldCheck /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">{t("站点权限与数据")}</div>
              <div className="settings-row-description">{t("在浏览器面板管理当前网站权限和数据。")}</div>
            </div>
            <div className="settings-row-control">
              <button type="button" className="settings-action-button" onClick={openBrowser}>{t("管理")}</button>
            </div>
          </div>
        </div></section>
      </div></details>
    </>
  );
};

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
    <button type="button" className="settings-back-button" onClick={onBack}>{t("返回浏览器设置")}</button>
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{t("浏览历史")}</h2></div></header>
    <div className="settings-search settings-browser-history-search"><input aria-label={t("搜索浏览历史")} placeholder={t("搜索浏览历史")} value={query} onChange={(event) => setQuery(event.target.value)} />
      <button type="button" className="settings-browser-history-filter" disabled={removing} aria-label={t("筛选浏览历史")} aria-haspopup="menu" aria-expanded={Boolean(menu && !menu.visit)} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setMenu({ position: { x: rect.right - 220, y: rect.bottom + 6 } }); }}><ListFilter size={16} />{source !== "all" && <span>{source === "agent" ? "Agent" : t("其他")}</span>}</button>
    </div>
    <div className="settings-browser-history-section-heading"><h3>{t("全部历史记录")}</h3><div>
      {selected.size > 0 && <button type="button" className="settings-action-button" disabled={removing} onClick={() => void removeEntries([...selected])}>{t("移除所选")}</button>}
      <button type="button" className="settings-action-button" onClick={() => void onClear().then((cleared) => { if (cleared) { setVisits([]); setSelected(new Set()); } })}>{t("清除浏览数据")}</button>
    </div></div>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">{t("正在加载浏览历史…")}</p> : visits.length === 0 ? <p>{t("没有匹配的浏览历史")}</p> : <div className="settings-card">
      {[...groups].map(([day, entries], index) => { const expanded = expandedDays?.has(day) ?? index === 0; return <div key={day} className="settings-browser-history-day">
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
    ] : (["all", "agent", "other"] as const).map((value) => ({ label: value === "agent" ? "Agent" : value === "other" ? t("其他") : t("全部"), icon: source === value ? <Check size={15} /> : undefined, onClick: () => { setSource(value); setSelected(new Set()); setExpandedDays(null); } }))} />}
  </section>;
}
