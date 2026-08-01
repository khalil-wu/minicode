import {
  ArrowLeft,
  ArrowRight,
  Bug,
  Crosshair,
  Download,
  ExternalLink,
  Globe2,
  LoaderCircle,
  MessageSquarePlus,
  Network,
  Plus,
  RefreshCw,
  Scan,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  embeddedBrowserActivate,
  embeddedBrowserClearSiteData,
  embeddedBrowserClose,
  embeddedBrowserGetSettings,
  embeddedBrowserInspect,
  embeddedBrowserList,
  embeddedBrowserNavigate,
  embeddedBrowserRunAction,
  embeddedBrowserSetSettings,
  embeddedBrowserSetBounds,
  isDesktop,
  onEmbeddedBrowserEvent,
  openExternal,
  type EmbeddedBrowserState,
  type EmbeddedBrowserSettings,
} from "../desktop/runtime";
import { assessNetworkTargetUrl } from "../lib/network-target";
import { BrandIcon } from "../components/BrandIcon";
import { useAppStore } from "../stores";
import {
  acknowledgeBrowserOpenRequest,
  subscribeBrowserOpenRequests,
} from "../chat/openWebInBrowser";
import "./BrowserPanel.css";

interface BrowserTab {
  id: string;
  title: string;
  url: string;
  draftUrl: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  faviconUrl?: string;
  error?: string;
}

type InspectorKind = "console" | "network";

interface BrowserDiagnosticItem {
  timestamp?: number;
  level?: number | string;
  message?: string;
  line?: number;
  sourceId?: string;
  url?: string;
  method?: string;
  statusCode?: number;
  resourceType?: string;
  fromCache?: boolean;
  error?: string;
}

interface PickedElement {
  selector: string;
  rect: { x: number; y: number; width: number; height: number };
  viewport: { width: number; height: number; devicePixelRatio?: number };
  text?: string;
}

const DEFAULT_BROWSER_SETTINGS: EmbeddedBrowserSettings = {
  downloadPolicy: "block",
  origin: "",
  permissions: [],
};

const sitePermissionOptions = [
  ["clipboard-read", "读取剪贴板"],
  ["media", "摄像头与麦克风"],
  ["geolocation", "位置"],
  ["notifications", "通知"],
] as const;

const diagnosticTimestamp = (timestamp?: number) => timestamp
  ? new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
  : "";

const blankTab = (id = createTabId()): BrowserTab => ({
  id,
  title: "新标签页",
  url: "",
  draftUrl: "",
  loading: false,
  canGoBack: false,
  canGoForward: false,
});

function createTabId(): string {
  return `browser_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function normalizeBrowserInput(value: string): string {
  const input = value.trim();
  if (!input) return "";
  if (/^https?:\/\//i.test(input)) return input;
  if (/^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/i.test(input)) {
    return `http://${input}`;
  }
  if (/^[\w.-]+\.[a-z]{2,}(?::\d+)?(?:\/.*)?$/i.test(input)) {
    return `https://${input}`;
  }
  return `https://www.bing.com/search?q=${encodeURIComponent(input)}`;
}

const updateTabFromEvent = (tab: BrowserTab, event: EmbeddedBrowserState): BrowserTab => ({
  ...tab,
  title: event.title || tab.title,
  url: event.url === "about:blank" ? "" : event.url || tab.url,
  draftUrl: event.url === "about:blank" ? "" : event.url || tab.draftUrl,
  loading: event.loading,
  canGoBack: event.canGoBack,
  canGoForward: event.canGoForward,
  faviconUrl: event.faviconUrl || tab.faviconUrl,
  error: event.type === "error" ? event.error || "页面加载失败。" : undefined,
});

export const BrowserPanel = () => {
  const permissionMode = useAppStore((state) => state.permissionMode);
  const addBrowserAnnotation = useAppStore((state) => state.addBrowserAnnotation);
  const addSelectedMention = useAppStore((state) => state.addSelectedMention);
  const [tabs, setTabs] = useState<BrowserTab[]>(() => [blankTab()]);
  const [activeId, setActiveId] = useState(() => tabs[0].id);
  const [browserHydrated, setBrowserHydrated] = useState(false);
  const [annotationOpen, setAnnotationOpen] = useState(false);
  const [annotationNote, setAnnotationNote] = useState("");
  const [annotationSelector, setAnnotationSelector] = useState("");
  const [pickedElement, setPickedElement] = useState<PickedElement | null>(null);
  const [pickerMode, setPickerMode] = useState<"element" | "region" | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorKind, setInspectorKind] = useState<InspectorKind>("console");
  const [diagnostics, setDiagnostics] = useState<BrowserDiagnosticItem[]>([]);
  const [inspectorLoading, setInspectorLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [browserSettings, setBrowserSettings] = useState<EmbeddedBrowserSettings>(DEFAULT_BROWSER_SETTINGS);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const addressRef = useRef<HTMLInputElement>(null);
  const activeIdRef = useRef(activeId);
  const createdIdsRef = useRef(new Set<string>());
  const visibleIdsRef = useRef(new Set<string>());
  const activeTab = useMemo(
    () => tabs.find((tab) => tab.id === activeId) ?? tabs[0],
    [activeId, tabs],
  );

  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  useEffect(() => {
    if (!isDesktop()) {
      setBrowserHydrated(true);
      return;
    }
    let cancelled = false;
    void Promise.resolve(embeddedBrowserList()).then((targets) => {
      if (cancelled || !Array.isArray(targets) || targets.length === 0) return;
      const restoredTabs = targets.map((target) => {
        createdIdsRef.current.add(target.id);
        if (target.url && target.url !== "about:blank") visibleIdsRef.current.add(target.id);
        return updateTabFromEvent(blankTab(target.id), target);
      });
      const restoredIds = new Set(restoredTabs.map((tab) => tab.id));
      setTabs((current) => [
        ...restoredTabs,
        ...current.filter((tab) => !restoredIds.has(tab.id) && createdIdsRef.current.has(tab.id)),
      ]);
      const activeTarget = targets.find((target) => target.active) ?? targets[0];
      if (!createdIdsRef.current.has(activeIdRef.current) || activeIdRef.current === tabs[0].id) {
        activeIdRef.current = activeTarget.id;
        setActiveId(activeTarget.id);
      }
    }).finally(() => {
      if (!cancelled) setBrowserHydrated(true);
    });
    return () => { cancelled = true; };
  }, []);

  const syncBounds = useCallback(() => {
    if (!isDesktop()) return;
    const element = surfaceRef.current;
    const id = activeIdRef.current;
    if (!element || !id || !createdIdsRef.current.has(id) || !visibleIdsRef.current.has(id)) return;
    const rect = element.getBoundingClientRect();
    void embeddedBrowserSetBounds({
      id,
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
    });
  }, []);

  useEffect(() => {
    if (!isDesktop() || !browserHydrated) return;
    const element = surfaceRef.current;
    if (!element) return;
    const observer = new ResizeObserver(syncBounds);
    observer.observe(element);
    window.addEventListener("resize", syncBounds);
    document.addEventListener("scroll", syncBounds, true);
    const animationFrame = window.requestAnimationFrame(syncBounds);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", syncBounds);
      document.removeEventListener("scroll", syncBounds, true);
      window.cancelAnimationFrame(animationFrame);
    };
  }, [syncBounds]);

  useEffect(() => {
    if (!isDesktop() || !browserHydrated) return;
    if (activeId !== activeIdRef.current) return;
    if (!createdIdsRef.current.has(activeId)) {
      // Keep the initial blank tab as UI state only. Create the native view
      // when a real URL is navigated, so opening a link cannot leave an extra
      // about:blank WebContents behind.
      return;
    }
    void embeddedBrowserActivate(activeId);
    if (visibleIdsRef.current.has(activeId)) window.requestAnimationFrame(syncBounds);
  }, [activeId, browserHydrated, syncBounds]);

  const openTab = useCallback((requestedUrl = "") => {
    const reusableBlank = tabs.find((tab) => !tab.url && !visibleIdsRef.current.has(tab.id));
    if (requestedUrl && reusableBlank) {
      setTabs((current) => current.map((tab) => (
        tab.id === reusableBlank.id
          ? { ...tab, url: requestedUrl, draftUrl: requestedUrl, loading: true, error: undefined }
          : tab
      )));
      setActiveId(reusableBlank.id);
      activeIdRef.current = reusableBlank.id;
      createdIdsRef.current.add(reusableBlank.id);
      visibleIdsRef.current.add(reusableBlank.id);
      void Promise.resolve(embeddedBrowserNavigate(reusableBlank.id, requestedUrl)).finally(() => {
        syncBounds();
      });
      return;
    }
    const tab = blankTab();
    setTabs((current) => [...current, tab]);
    setActiveId(tab.id);
    if (requestedUrl) {
      window.queueMicrotask(() => {
        createdIdsRef.current.add(tab.id);
        visibleIdsRef.current.add(tab.id);
        void Promise.resolve(embeddedBrowserNavigate(tab.id, requestedUrl)).finally(() => {
          activeIdRef.current = tab.id;
          syncBounds();
        });
      });
    } else {
      window.setTimeout(() => addressRef.current?.focus(), 0);
    }
  }, [syncBounds, tabs]);

  useEffect(() => subscribeBrowserOpenRequests((request) => {
    acknowledgeBrowserOpenRequest(request.id);
    openTab(request.url);
  }), [openTab]);

  useEffect(() => {
    if (!isDesktop()) return;
    const unsubscribe = onEmbeddedBrowserEvent((event) => {
      if (event.type === "new-tab-request" && event.requestedUrl) {
        openTab(event.requestedUrl);
        return;
      }
      const knownTab = createdIdsRef.current.has(event.id);
      createdIdsRef.current.add(event.id);
      if (event.url && event.url !== "about:blank") visibleIdsRef.current.add(event.id);
      setTabs((current) => {
        const existing = current.find((tab) => tab.id === event.id);
        if (existing) {
          return current.map((tab) => tab.id === event.id ? updateTabFromEvent(tab, event) : tab);
        }
        return [...current, updateTabFromEvent(blankTab(event.id), event)];
      });
      if (!knownTab) {
        activeIdRef.current = event.id;
        setActiveId(event.id);
      }
      if (event.id === activeIdRef.current && event.url !== "about:blank") {
        window.requestAnimationFrame(syncBounds);
      }
    });
    return () => unsubscribe?.();
  }, [openTab]);

  useEffect(() => () => {
    for (const id of createdIdsRef.current) {
      void embeddedBrowserSetBounds({ id, x: 0, y: 0, width: 0, height: 0 });
    }
    visibleIdsRef.current.clear();
  }, []);

  const navigate = async (tabId: string, rawValue: string) => {
    const normalized = normalizeBrowserInput(rawValue);
    if (!normalized) return;
    const target = assessNetworkTargetUrl(normalized);
    if (target.risk === "invalid") {
      setTabs((current) => current.map((tab) => (
        tab.id === tabId ? { ...tab, error: target.reason } : tab
      )));
      return;
    }
    if (permissionMode !== "bypass" && target.requiresReview) {
      const { showConfirm } = await import("../overlays/DialogService");
      const confirmed = await showConfirm({
        title: target.risk === "local" ? "打开本地地址？" : "打开局域网地址？",
        message: `${target.host} 可能访问这台电脑或局域网中的服务。确认继续吗？`,
        confirmLabel: "继续打开",
      });
      if (!confirmed) return;
    }
    setTabs((current) => current.map((tab) => (
      tab.id === tabId
        ? { ...tab, draftUrl: target.normalizedUrl, loading: true, error: undefined }
        : tab
    )));
    createdIdsRef.current.add(tabId);
    visibleIdsRef.current.add(tabId);
    try {
      await embeddedBrowserNavigate(tabId, target.normalizedUrl);
      activeIdRef.current = tabId;
      syncBounds();
    } catch (error) {
      setTabs((current) => current.map((tab) => (
        tab.id === tabId
          ? { ...tab, loading: false, error: error instanceof Error ? error.message : "页面加载失败。" }
          : tab
      )));
    }
  };

  const closeTab = (tabId: string) => {
    const index = tabs.findIndex((tab) => tab.id === tabId);
    if (index < 0) return;
    visibleIdsRef.current.delete(tabId);
    if (createdIdsRef.current.delete(tabId)) void embeddedBrowserClose(tabId);
    if (tabs.length === 1) {
      const replacement = blankTab();
      setTabs([replacement]);
      setActiveId(replacement.id);
      return;
    }
    const nextTabs = tabs.filter((tab) => tab.id !== tabId);
    setTabs(nextTabs);
    if (activeId === tabId) {
      const next = nextTabs[Math.min(index, nextTabs.length - 1)];
      setActiveId(next.id);
    }
  };

  const saveAnnotation = () => {
    const note = annotationNote.trim();
    if (!note || !activeTab?.url) return;
    const id = `browser_note_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
    const selector = annotationSelector.trim();
    const viewport = pickedElement?.viewport;
    const rect = pickedElement?.rect;
    const xPercent = viewport && rect ? (rect.x + rect.width / 2) / viewport.width : undefined;
    const yPercent = viewport && rect ? (rect.y + rect.height / 2) / viewport.height : undefined;
    const annotation = {
      id,
      targetId: activeTab.id,
      url: activeTab.url,
      title: activeTab.title,
      selector: selector || undefined,
      xPercent,
      yPercent,
      widthPercent: viewport && rect ? rect.width / viewport.width : undefined,
      heightPercent: viewport && rect ? rect.height / viewport.height : undefined,
      viewportWidth: viewport?.width,
      viewportHeight: viewport?.height,
      note,
      createdAt: Date.now(),
    };
    addBrowserAnnotation(annotation);
    // A saved annotation is immediately available to the next agent turn;
    // the unique local path prevents annotations on the same page collapsing.
    addSelectedMention({
      kind: "browser_annotation",
      path: `browser-annotation:${id}`,
      name: "页面批注",
      url: annotation.url,
      note: annotation.note,
      selector: annotation.selector,
      targetId: annotation.targetId,
      xPercent: annotation.xPercent,
      yPercent: annotation.yPercent,
      widthPercent: annotation.widthPercent,
      heightPercent: annotation.heightPercent,
      viewportWidth: annotation.viewportWidth,
      viewportHeight: annotation.viewportHeight,
    });
    setAnnotationOpen(false);
    setAnnotationNote("");
    setAnnotationSelector("");
    setPickedElement(null);
  };

  const pickPageTarget = async (kind: "element" | "region") => {
    if (!activeTab?.url || pickerMode) return;
    setPickerMode(kind);
    try {
      const result = await embeddedBrowserInspect(activeTab.id, kind);
      const value = result?.value as PickedElement | null | undefined;
      if (!value?.rect || !value.viewport) return;
      setPickedElement(value);
      setAnnotationSelector(value.selector || "");
      if (!annotationNote.trim() && value.text) setAnnotationNote(value.text);
    } finally {
      setPickerMode(null);
    }
  };

  const updateBrowserSettings = async (payload: Parameters<typeof embeddedBrowserSetSettings>[0]) => {
    const next = await embeddedBrowserSetSettings(payload);
    if (next) setBrowserSettings(next);
  };

  const refreshInspector = useCallback(async () => {
    if (!activeTab?.url) return;
    setInspectorLoading(true);
    try {
      const result = await embeddedBrowserInspect(activeTab.id, inspectorKind);
      setDiagnostics(Array.isArray(result?.value) ? result.value as BrowserDiagnosticItem[] : []);
    } finally {
      setInspectorLoading(false);
    }
  }, [activeTab?.id, activeTab?.url, inspectorKind]);

  useEffect(() => {
    if (!inspectorOpen) return;
    void refreshInspector();
    window.requestAnimationFrame(syncBounds);
  }, [inspectorOpen, inspectorKind, refreshInspector, syncBounds]);

  useEffect(() => {
    if (!settingsOpen || !activeTab?.url) return;
    void Promise.resolve(embeddedBrowserGetSettings(activeTab.url)).then((settings) => {
      if (settings) setBrowserSettings(settings);
    });
    window.requestAnimationFrame(syncBounds);
  }, [activeTab?.url, settingsOpen, syncBounds]);

  if (!isDesktop()) {
    return (
      <div className="mc-browser-unavailable">
        <Globe2 size={24} strokeWidth={1.8} />
        <strong>内置浏览器仅在桌面版可用</strong>
        <span>请在 MiniCode 桌面应用中打开网页。</span>
      </div>
    );
  }

  return (
    <div className="mc-browser-panel">
      <div className="mc-browser-tabs" role="tablist" aria-label="浏览器标签页">
        <div className="mc-browser-tabs-scroll">
          {tabs.map((tab) => (
            <div key={tab.id} className="mc-browser-tab" data-active={tab.id === activeId ? "true" : "false"}>
              <button
                type="button"
                role="tab"
                aria-selected={tab.id === activeId}
                title={tab.title}
                onClick={() => setActiveId(tab.id)}
              >
                {tab.loading
                  ? <LoaderCircle className="mc-browser-spin" size={14} />
                  : <BrandIcon value={`${tab.title} ${tab.url}`} iconUrl={tab.faviconUrl} websiteUrl={tab.url} fallback="web" size={14} />}
                <span>{tab.title || "新标签页"}</span>
              </button>
              <button type="button" aria-label={`关闭 ${tab.title || "标签页"}`} onClick={() => closeTab(tab.id)}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
        <button type="button" className="mc-browser-new-tab" aria-label="新建标签页" title="新建标签页" onClick={() => openTab()}>
          <Plus size={16} />
        </button>
      </div>

      <div className="mc-browser-toolbar">
        <button
          type="button"
          aria-label="后退"
          title="后退"
          disabled={!activeTab.canGoBack}
          onClick={() => void embeddedBrowserRunAction(activeTab.id, "back")}
        >
          <ArrowLeft size={17} />
        </button>
        <button
          type="button"
          aria-label="前进"
          title="前进"
          disabled={!activeTab.canGoForward}
          onClick={() => void embeddedBrowserRunAction(activeTab.id, "forward")}
        >
          <ArrowRight size={17} />
        </button>
        <button
          type="button"
          aria-label={activeTab.loading ? "停止加载" : "刷新"}
          title={activeTab.loading ? "停止加载" : "刷新"}
          disabled={!activeTab.url}
          onClick={() => void embeddedBrowserRunAction(activeTab.id, activeTab.loading ? "stop" : "reload")}
        >
          {activeTab.loading ? <X size={16} /> : <RefreshCw size={16} />}
        </button>
        <form
          className="mc-browser-address"
          onSubmit={(event) => {
            event.preventDefault();
            void navigate(activeTab.id, activeTab.draftUrl);
          }}
        >
          {activeTab.url ? <ShieldCheck size={15} /> : <Search size={15} />}
          <input
            ref={addressRef}
            value={activeTab.draftUrl}
            onChange={(event) => {
              const value = event.target.value;
              setTabs((current) => current.map((tab) => tab.id === activeTab.id ? { ...tab, draftUrl: value } : tab));
            }}
            onFocus={(event) => event.currentTarget.select()}
            placeholder="输入网址或搜索内容"
            aria-label="地址栏"
            spellCheck={false}
          />
        </form>
        <button
          type="button"
          aria-label="在系统浏览器中打开"
          title="在系统浏览器中打开"
          disabled={!activeTab.url}
          onClick={() => activeTab.url && void openExternal(activeTab.url)}
        >
          <ExternalLink size={16} />
        </button>
        <button
          type="button"
          aria-label="添加页面批注"
          title="添加页面批注"
          disabled={!activeTab.url}
          onClick={() => setAnnotationOpen((current) => !current)}
        >
          <MessageSquarePlus size={16} />
        </button>
        <button
          type="button"
          aria-label="打开页面诊断"
          title="页面诊断"
          disabled={!activeTab.url}
          aria-pressed={inspectorOpen}
          onClick={() => setInspectorOpen((current) => !current)}
        >
          <Bug size={16} />
        </button>
        <button
          type="button"
          aria-label="打开站点设置"
          title="站点设置"
          disabled={!activeTab.url}
          aria-pressed={settingsOpen}
          onClick={() => setSettingsOpen((current) => !current)}
        >
          <Settings2 size={16} />
        </button>
      </div>

      {settingsOpen && activeTab.url && (
        <section className="mc-browser-settings" aria-label="站点设置">
          <div className="mc-browser-settings-heading">
            <Settings2 size={14} />
            <strong>{browserSettings.origin || activeTab.url}</strong>
            <button
              type="button"
              aria-label="清除站点数据"
              title="清除站点数据"
              onClick={() => void Promise.resolve(embeddedBrowserClearSiteData(activeTab.id)).then(() => {
                setBrowserSettings((current) => ({ ...current, permissions: [] }));
              })}
            >
              <Trash2 size={14} />
            </button>
          </div>
          <label className="mc-browser-setting-row">
            <span><Download size={14} /> 下载</span>
            <select
              aria-label="下载策略"
              value={browserSettings.downloadPolicy}
              onChange={(event) => void updateBrowserSettings({ downloadPolicy: event.target.value as EmbeddedBrowserSettings["downloadPolicy"] })}
            >
              <option value="block">阻止</option>
              <option value="ask">每次询问</option>
              <option value="allow">保存到下载目录</option>
            </select>
          </label>
          <div className="mc-browser-permissions" aria-label="站点权限">
            {sitePermissionOptions.map(([permission, label]) => (
              <label className="mc-browser-setting-row" key={permission}>
                <span>{label}</span>
                <input
                  type="checkbox"
                  checked={browserSettings.permissions.includes(permission)}
                  onChange={(event) => void updateBrowserSettings({
                    origin: browserSettings.origin || activeTab.url,
                    permission,
                    allowed: event.target.checked,
                  })}
                />
              </label>
            ))}
          </div>
        </section>
      )}

      {inspectorOpen && activeTab.url && (
        <section className="mc-browser-inspector" aria-label="页面诊断">
          <div className="mc-browser-inspector-heading">
            <div role="tablist" aria-label="诊断类别">
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "console"}
                onClick={() => setInspectorKind("console")}
              >
                <Bug size={14} /> 控制台
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "network"}
                onClick={() => setInspectorKind("network")}
              >
                <Network size={14} /> 网络
              </button>
            </div>
            <button type="button" onClick={() => void refreshInspector()} disabled={inspectorLoading}>
              <RefreshCw className={inspectorLoading ? "mc-browser-spin" : undefined} size={14} /> 刷新
            </button>
          </div>
          <div className="mc-browser-inspector-list">
            {diagnostics.length === 0 ? (
              <span className="mc-browser-inspector-empty">尚未记录{inspectorKind === "console" ? "控制台" : "网络"}事件</span>
            ) : diagnostics.slice(-50).reverse().map((item, index) => (
              <div className="mc-browser-inspector-row" key={`${item.timestamp ?? "event"}-${index}`}>
                <time>{diagnosticTimestamp(item.timestamp)}</time>
                {inspectorKind === "console" ? (
                  <span title={item.message}>{item.message || "控制台消息"}</span>
                ) : (
                  <>
                    <b>{item.statusCode || "ERR"}</b>
                    <span>{item.method || "GET"}</span>
                    <span title={item.url}>{item.url || item.error || "网络请求"}</span>
                  </>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {annotationOpen && activeTab.url && (
        <div className="mc-browser-annotation" role="region" aria-label="页面批注">
          <div className="mc-browser-annotation-heading">
            <MessageSquarePlus size={15} />
            <span>页面批注</span>
            <small>{activeTab.title || activeTab.url}</small>
          </div>
          <input
            value={annotationSelector}
            onChange={(event) => setAnnotationSelector(event.target.value)}
            placeholder="元素选择器（可选，例如 #save）"
            aria-label="元素选择器"
            spellCheck={false}
          />
          <div className="mc-browser-picker-actions">
            <button
              type="button"
              className="mc-browser-picker-button"
              disabled={pickerMode != null}
              onClick={() => void pickPageTarget("element")}
            >
              {pickerMode === "element" ? <LoaderCircle className="mc-browser-spin" size={14} /> : <Crosshair size={14} />}
              {pickerMode === "element" ? "在页面中点击目标…" : "选择元素"}
            </button>
            <button
              type="button"
              className="mc-browser-picker-button"
              disabled={pickerMode != null}
              onClick={() => void pickPageTarget("region")}
            >
              {pickerMode === "region" ? <LoaderCircle className="mc-browser-spin" size={14} /> : <Scan size={14} />}
              {pickerMode === "region" ? "在页面中拖拽区域…" : "框选区域"}
            </button>
          </div>
          {pickedElement && (
            <small className="mc-browser-picker-result">
              已选择 {Math.round(pickedElement.rect.width)} × {Math.round(pickedElement.rect.height)} px
            </small>
          )}
          <textarea
            value={annotationNote}
            onChange={(event) => setAnnotationNote(event.target.value)}
            placeholder="描述需要修复或验证的内容"
            aria-label="批注内容"
            rows={3}
            autoFocus
          />
          <div className="mc-browser-annotation-actions">
            <button type="button" onClick={() => setAnnotationOpen(false)}>取消</button>
            <button type="button" disabled={!annotationNote.trim()} onClick={saveAnnotation}>加入智能体上下文</button>
          </div>
        </div>
      )}

      {activeTab.error && (
        <div className="mc-browser-error" role="alert">
          <span>{activeTab.error}</span>
          <button type="button" onClick={() => void navigate(activeTab.id, activeTab.draftUrl)}>重试</button>
        </div>
      )}

      <div ref={surfaceRef} className="mc-browser-surface" data-empty={!activeTab.url ? "true" : "false"}>
        {!activeTab.url && (
          <div className="mc-browser-empty">
            <span className="mc-browser-empty-icon"><Globe2 size={28} strokeWidth={1.8} /></span>
            <strong>开始浏览</strong>
            <span>在地址栏输入网址或搜索内容</span>
            <button type="button" onClick={() => addressRef.current?.focus()}>
              <Search size={15} /> 输入地址
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
