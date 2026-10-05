import {
  ArrowLeft,
  ArrowRight,
  Bug,
  Crosshair,
  Download,
  ExternalLink,
  FileDiff,
  Globe2,
  LoaderCircle,
  MessageSquarePlus,
  MoreHorizontal,
  Network,
  Plus,
  RefreshCw,
  Scan,
  Search,
  Settings2,
  Smartphone,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { ContextMenu } from "../components/ContextMenu";
import { NumberInput } from "../components/NumberInput";
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
import { parseHttpUrl } from "../lib/network-target";
import { previewUrlsShareOrigin } from "../lib/preview-projection";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { BrandIcon } from "../components/BrandIcon";
import { SelectMenu } from "../components/SelectMenu";
import { useAppStore } from "../stores";
import { useTurnChanges } from "../chat/useTurnChanges";
import { PreviewServerManager } from "./PreviewServerManager";
import {
  acknowledgeBrowserRequest,
  subscribeBrowserRequests,
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

const RENDERER_OVERLAYS = '[role="menu"], [role="listbox"], [role="dialog"], [role="tooltip"]';

const navigateTabList = (event: KeyboardEvent<HTMLDivElement>) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (event.key === "ArrowLeft" ? -1 : 1) + buttons.length) % buttons.length;
  buttons[next].focus(); buttons[next].click();
};

interface BrowserDiagnosticItem {
  durationMs?: number;
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
  source?: { path: string; line: number; column?: number };
  selector: string;
  rect: { x: number; y: number; width: number; height: number };
  viewport: { width: number; height: number; devicePixelRatio?: number };
  text?: string;
}

interface AnnotationDraft {
  elements?: PickedElement[];
  open: boolean;
  note: string;
  selector: string;
  pickedElement: PickedElement | null;
}

const EMPTY_ANNOTATION_DRAFT: AnnotationDraft = { open: false, note: "", selector: "", pickedElement: null };

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

const diagnosticSource = (item: BrowserDiagnosticItem) => {
  const source = item.sourceId ?? "";
  const match = /^(?:https?:\/\/[^/]+)\/(@fs\/)?(src\/[^?#]+|[^?#]+\.[cm]?[jt]sx?)(?:[?#].*)?$/.exec(source);
  if (!match || (!match[1] && !match[2].startsWith("src/"))) return null;
  return { path: decodeURIComponent(match[2]), line: item.line ?? 1 };
};

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
  faviconUrl: event.faviconUrl ?? tab.faviconUrl,
  error: event.type === "error" ? event.error || "页面加载失败。" : undefined,
});

export const BrowserPanel = () => {
  const conversationId = useAppStore((state) => state.conversationId) || "";
  const addBrowserAnnotation = useAppStore((state) => state.addBrowserAnnotation);
  const addSelectedMention = useAppStore((state) => state.addSelectedMention);
  const { summary: turnChanges, openReview } = useTurnChanges();
  const [tabs, setRenderedTabs] = useState<BrowserTab[]>(() => [blankTab()]);
  const [activeId, setActiveId] = useState(() => tabs[0].id);
  const [hydratedConversationId, setHydratedConversationId] = useState<string | null>(null);
  const browserHydrated = hydratedConversationId === conversationId;
  const [annotationDrafts, setAnnotationDrafts] = useState<Record<string, AnnotationDraft>>({});
  const [pickerMode, setPickerMode] = useState<"element" | "region" | null>(null);
  const [moreMenuPosition, setMoreMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const [inspectorPage, setInspectorPage] = useState<string | null>(null);
  const [inspectorKind, setInspectorKind] = useState<InspectorKind>("console");
  const [diagnostics, setDiagnostics] = useState<BrowserDiagnosticItem[]>([]);
  const [diagnosticQuery, setDiagnosticQuery] = useState("");
  const [diagnosticFilter, setDiagnosticFilter] = useState("all");
  const [viewport, setViewport] = useState<{ width: number; height: number; mobile: boolean } | null>(null);
  const viewportRef = useRef(viewport);
  viewportRef.current = viewport;
  const [inspectorLoading, setInspectorLoading] = useState(false);
  const [inspectorError, setInspectorError] = useState("");
  const [settingsPage, setSettingsPage] = useState<string | null>(null);
  const [browserSettings, setBrowserSettings] = useState<EmbeddedBrowserSettings>(DEFAULT_BROWSER_SETTINGS);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const overlaysRef = useRef(new Set<HTMLElement>());
  const addressRef = useRef<HTMLInputElement>(null);
  const annotationNoteRef = useRef<HTMLTextAreaElement>(null);
  const activeIdRef = useRef(activeId);
  const tabsRef = useRef(tabs);
  const setTabs = useCallback((update: BrowserTab[] | ((current: BrowserTab[]) => BrowserTab[])) => {
    const nextTabs = typeof update === "function" ? update(tabsRef.current) : update;
    tabsRef.current = nextTabs;
    setRenderedTabs(nextTabs);
  }, []);
  const createdIdsRef = useRef(new Set<string>());
  const visibleIdsRef = useRef(new Set<string>());
  const ownerRef = useRef(conversationId);
  const ownerGenerationRef = useRef(0);
  const pageGenerationRef = useRef(0);
  const inspectorRequestRef = useRef(0);
  const settingsRevisionRef = useRef(0);
  const navigationRequestsRef = useRef(new Map<string, number>());
  const activeTab = useMemo(
    () => tabs.find((tab) => tab.id === activeId) ?? tabs[0],
    [activeId, tabs],
  );
  const pageKey = JSON.stringify([conversationId, activeId, activeTab.url]);
  const annotationDraft = annotationDrafts[pageKey] ?? EMPTY_ANNOTATION_DRAFT;
  const { open: annotationOpen, note: annotationNote, selector: annotationSelector, pickedElement } = annotationDraft;
  const pickedElements = annotationDraft.elements ?? (pickedElement ? [pickedElement] : []);
  const visibleDiagnostics = diagnostics.filter((item) => {
    const isError = inspectorKind === "console" ? item.level === 3 || item.level === "error" : Boolean(item.error) || (item.statusCode ?? 0) >= 400;
    return (diagnosticFilter !== "errors" || isError)
      && (diagnosticFilter !== "warnings" || item.level === 2 || item.level === "warning")
      && (diagnosticFilter !== "fetch" || ["xhr", "fetch"].includes(item.resourceType ?? ""))
      && JSON.stringify(item).toLocaleLowerCase().includes(diagnosticQuery.toLocaleLowerCase());
  });
  const updateAnnotationDraft = (patch: Partial<AnnotationDraft>) => {
    setAnnotationDrafts((drafts) => ({
      ...drafts,
      [pageKey]: { ...(drafts[pageKey] ?? EMPTY_ANNOTATION_DRAFT), ...patch },
    }));
  };
  const inspectorOpen = inspectorPage === pageKey;
  const settingsOpen = settingsPage === pageKey;

  useLayoutEffect(() => {
    pageGenerationRef.current += 1;
    inspectorRequestRef.current += 1;
    setPickerMode(null);
    setMoreMenuPosition(null);
    setInspectorPage(null);
    setInspectorLoading(false);
    setInspectorError("");
    setDiagnostics([]);
    setSettingsPage(null);
    setBrowserSettings(DEFAULT_BROWSER_SETTINGS);
    setSettingsLoading(false);
    setSettingsSaving(false);
    settingsRevisionRef.current += 1;
  }, [conversationId, activeId, activeTab.url]);

  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  useEffect(() => {
    const previousOwner = ownerRef.current;
    if (isDesktop() && previousOwner && previousOwner !== conversationId) {
      for (const id of createdIdsRef.current) {
        void embeddedBrowserSetBounds({
          id,
          conversationId: previousOwner,
          x: 0,
          y: 0,
          width: 0,
          height: 0,
        });
      }
    }
    ownerRef.current = conversationId;
    const generation = ++ownerGenerationRef.current;
    const initialTab = blankTab();
    createdIdsRef.current.clear();
    visibleIdsRef.current.clear();
    navigationRequestsRef.current.clear();
    activeIdRef.current = initialTab.id;
    setTabs([initialTab]);
    setActiveId(initialTab.id);
    setHydratedConversationId(null);
    setInspectorPage(null);
    setSettingsPage(null);
    setPickerMode(null);
    setInspectorLoading(false);
    setDiagnostics([]);
    if (!isDesktop()) {
      setHydratedConversationId(conversationId);
      return;
    }
    if (!conversationId) {
      setHydratedConversationId(conversationId);
      return;
    }
    let cancelled = false;
    void Promise.resolve(embeddedBrowserList(conversationId)).then((targets) => {
      if (
        cancelled
        || ownerGenerationRef.current !== generation
        || ownerRef.current !== conversationId
        || !Array.isArray(targets)
        || targets.length === 0
      ) return;
      const restoredTabs = targets.map((target) => {
        if (!createdIdsRef.current.has(target.id) && target.url && target.url !== "about:blank") {
          visibleIdsRef.current.add(target.id);
        }
        createdIdsRef.current.add(target.id);
        return updateTabFromEvent(blankTab(target.id), target);
      });
      const restoredIds = new Set(restoredTabs.map((tab) => tab.id));
      setTabs((current) => {
        const liveTabs = new Map(current.map((tab) => [tab.id, tab]));
        return [
          ...restoredTabs.map((tab) => liveTabs.get(tab.id) ?? tab),
          ...current.filter((tab) => !restoredIds.has(tab.id) && createdIdsRef.current.has(tab.id)),
        ];
      });
      const activeTarget = targets.find((target) => target.active) ?? targets[0];
      if (!createdIdsRef.current.has(activeIdRef.current) || activeIdRef.current === initialTab.id) {
        activeIdRef.current = activeTarget.id;
        setActiveId(activeTarget.id);
      }
    }).catch((error) => {
      if (cancelled || ownerGenerationRef.current !== generation || ownerRef.current !== conversationId) return;
      setTabs((current) => current.map((tab) => tab.id === activeIdRef.current
        ? { ...tab, error: error instanceof Error ? error.message : "无法恢复浏览器标签页。" }
        : tab));
    }).finally(() => {
      if (!cancelled && ownerGenerationRef.current === generation) setHydratedConversationId(conversationId);
    });
    return () => { cancelled = true; };
  }, [conversationId]);

  const syncBounds = useCallback(() => {
    if (!isDesktop()) return;
    const element = surfaceRef.current;
    const id = activeIdRef.current;
    const owner = ownerRef.current;
    if (!owner || !element || !id || !createdIdsRef.current.has(id) || !visibleIdsRef.current.has(id)) return;
    const rect = element.getBoundingClientRect();
    // WebContentsView sits above the renderer, regardless of CSS z-index.
    // Yield its surface to overlapping menus and modal backdrops; retain the
    // same tab and navigation state when the renderer overlay closes.
    const obscured = Array.from(overlaysRef.current).some((overlay) => {
      if (overlay.contains(element)) return false;
      const bounds = overlay.getBoundingClientRect();
      return bounds.width > 0 && bounds.height > 0 && getComputedStyle(overlay).visibility !== "hidden"
        && (overlay.getAttribute("aria-modal") === "true"
          || (bounds.left < rect.right && bounds.right > rect.left && bounds.top < rect.bottom && bounds.bottom > rect.top));
    });
    void embeddedBrowserSetBounds({
      id,
      conversationId: owner,
      x: rect.left,
      y: rect.top,
      width: obscured ? 0 : rect.width,
      height: obscured ? 0 : rect.height,
      viewport: viewportRef.current,
    });
  }, []);
  useEffect(() => { syncBounds(); }, [viewport, syncBounds]);

  const reconcileNativeTab = useCallback(async (tabId: string): Promise<boolean> => {
    const owner = ownerRef.current;
    const generation = ownerGenerationRef.current;
    const navigationRequest = navigationRequestsRef.current.get(tabId);
    if (!owner) return false;
    const targets = await embeddedBrowserList(owner);
    if (ownerRef.current !== owner || ownerGenerationRef.current !== generation
      || navigationRequestsRef.current.get(tabId) !== navigationRequest
      || !tabsRef.current.some((tab) => tab.id === tabId)) return false;
    if (!Array.isArray(targets)) return false;
    const target = targets.find((item) => item.id === tabId);
    if (!target) {
      createdIdsRef.current.delete(tabId);
      visibleIdsRef.current.delete(tabId);
      return false;
    }
    createdIdsRef.current.add(tabId);
    if (target.url && target.url !== "about:blank") visibleIdsRef.current.add(tabId);
    else visibleIdsRef.current.delete(tabId);
    setTabs((current) => current.map((tab) => tab.id === tabId ? updateTabFromEvent(tab, target) : tab));
    return true;
  }, []);

  const performNativeNavigation = useCallback(async (tabId: string, url: string): Promise<boolean> => {
    const owner = ownerRef.current;
    const generation = ownerGenerationRef.current;
    if (!owner) return false;
    const request = (navigationRequestsRef.current.get(tabId) ?? 0) + 1;
    navigationRequestsRef.current.set(tabId, request);
    const isCurrent = () => ownerRef.current === owner
      && ownerGenerationRef.current === generation
      && navigationRequestsRef.current.get(tabId) === request;
    try {
      const state = await embeddedBrowserNavigate(owner, tabId, url);
      if (!isCurrent()) return false;
      if (
        !state
        || state.id !== tabId
        || state.conversationId !== owner
      ) throw new Error("Desktop browser did not confirm the navigation.");
      createdIdsRef.current.add(tabId);
      visibleIdsRef.current.add(tabId);
      setTabs((current) => current.map((tab) => (
        tab.id === tabId ? updateTabFromEvent(tab, state) : tab
      )));
      if (activeIdRef.current === tabId) {
        await embeddedBrowserActivate(owner, tabId);
      } else {
        if (createdIdsRef.current.has(activeIdRef.current)) {
          await embeddedBrowserActivate(owner, activeIdRef.current);
        } else {
          await embeddedBrowserSetBounds({ id: tabId, conversationId: owner, x: 0, y: 0, width: 0, height: 0 });
        }
      }
      if (!isCurrent()) return false;
      syncBounds();
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      await reconcileNativeTab(tabId).catch(() => false);
      if (!isCurrent()) return false;
      setTabs((current) => current.map((tab) => (
        tab.id === tabId
          ? { ...tab, loading: false, error: error instanceof Error ? error.message : "页面加载失败。" }
          : tab
      )));
      return false;
    }
  }, [reconcileNativeTab, syncBounds]);

  useEffect(() => {
    if (!isDesktop() || !browserHydrated) return;
    const element = surfaceRef.current;
    if (!element) return;
    const observer = new ResizeObserver(syncBounds);
    observer.observe(element);
    const trackOverlays = (node: Node, added: boolean) => {
      if (!(node instanceof HTMLElement)) return false;
      const overlays = [...node.querySelectorAll<HTMLElement>(RENDERER_OVERLAYS)];
      if (node.matches(RENDERER_OVERLAYS)) overlays.push(node);
      for (const overlay of overlays) {
        if (added) {
          overlaysRef.current.add(overlay);
          observer.observe(overlay);
        } else {
          overlaysRef.current.delete(overlay);
          observer.unobserve(overlay);
        }
      }
      return overlays.length > 0;
    };
    trackOverlays(document.body, true);
    const overlayObserver = new MutationObserver((mutations) => {
      let changed = false;
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) changed = trackOverlays(node, true) || changed;
        for (const node of mutation.removedNodes) changed = trackOverlays(node, false) || changed;
      }
      if (changed) syncBounds();
    });
    // Only overlay mount/unmount triggers IPC. Streaming text updates do not
    // scan the document or resize the native browser.
    overlayObserver.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", syncBounds);
    document.addEventListener("scroll", syncBounds, true);
    const animationFrame = window.requestAnimationFrame(syncBounds);
    return () => {
      observer.disconnect();
      overlayObserver.disconnect();
      overlaysRef.current.clear();
      window.removeEventListener("resize", syncBounds);
      document.removeEventListener("scroll", syncBounds, true);
      window.cancelAnimationFrame(animationFrame);
    };
  }, [browserHydrated, syncBounds]);

  useEffect(() => {
    if (!isDesktop() || !browserHydrated) return;
    if (activeId !== activeIdRef.current) return;
    if (!createdIdsRef.current.has(activeId)) {
      // Keep the initial blank tab as UI state only. Create the native view
      // when a real URL is navigated, so opening a link cannot leave an extra
      // about:blank WebContents behind.
      return;
    }
    const owner = ownerRef.current;
    const generation = ownerGenerationRef.current;
    if (!owner) return;
    const navigationRequest = navigationRequestsRef.current.get(activeId);
    const isCurrent = () => ownerRef.current === owner && ownerGenerationRef.current === generation
      && activeIdRef.current === activeId && navigationRequestsRef.current.get(activeId) === navigationRequest;
    void Promise.resolve(embeddedBrowserActivate(owner, activeId)).then(async (activated) => {
      if (!isCurrent()) return;
      if (!activated) await reconcileNativeTab(activeId);
    }).catch((error) => {
      if (!isCurrent()) return;
      setTabs((current) => current.map((tab) => tab.id === activeId
        ? { ...tab, error: error instanceof Error ? error.message : "无法激活浏览器标签页。" }
        : tab));
    });
    if (visibleIdsRef.current.has(activeId)) window.requestAnimationFrame(syncBounds);
  }, [activeId, browserHydrated, reconcileNativeTab, syncBounds]);

  const openTab = useCallback((requestedUrl = "") => {
    const reusableBlank = requestedUrl
      ? tabsRef.current.find((tab) => !tab.url && !visibleIdsRef.current.has(tab.id))
      : undefined;
    const tab = requestedUrl
      ? { ...(reusableBlank ?? blankTab()), url: requestedUrl, draftUrl: requestedUrl, loading: true, error: undefined }
      : blankTab();
    const nextTabs = reusableBlank
      ? tabsRef.current.map((current) => current.id === tab.id ? tab : current)
      : [...tabsRef.current, tab];
    setTabs(nextTabs);
    activeIdRef.current = tab.id;
    setActiveId(tab.id);
    if (requestedUrl) {
      void performNativeNavigation(tab.id, requestedUrl);
    } else {
      window.setTimeout(() => addressRef.current?.focus(), 0);
    }
  }, [performNativeNavigation]);

  useEffect(() => {
    if (!isDesktop()) return;
    const unsubscribe = onEmbeddedBrowserEvent((event) => {
      if (event.conversationId !== ownerRef.current) return;
      if (event.type === "new-tab-request" && event.requestedUrl) {
        openTab(event.requestedUrl);
        return;
      }
      const knownTab = createdIdsRef.current.has(event.id);
      createdIdsRef.current.add(event.id);
      if (event.url && event.url !== "about:blank") visibleIdsRef.current.add(event.id);
      else if (event.url === "about:blank") visibleIdsRef.current.delete(event.id);
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
    ownerGenerationRef.current += 1;
    pageGenerationRef.current += 1;
    inspectorRequestRef.current += 1;
    const owner = ownerRef.current;
    for (const id of createdIdsRef.current) {
      if (owner) {
        void embeddedBrowserSetBounds({ id, conversationId: owner, x: 0, y: 0, width: 0, height: 0 });
      }
    }
    visibleIdsRef.current.clear();
  }, []);

  const navigate = async (tabId: string, rawValue: string) => {
    const normalized = normalizeBrowserInput(rawValue);
    if (!normalized) return;
    const target = parseHttpUrl(normalized);
    if (!target) {
      setTabs((current) => current.map((tab) => (
        tab.id === tabId ? { ...tab, error: "仅支持不包含登录凭据的 HTTP(S) 地址。" } : tab
      )));
      return;
    }
    // The Electron main process owns private-network approval so every entry
    // path (address bar, tool card, popup, redirect, or agent control bridge)
    // crosses the same security boundary exactly once.
    setTabs((current) => current.map((tab) => (
      tab.id === tabId
        ? { ...tab, draftUrl: target.toString(), loading: true, error: undefined }
        : tab
    )));
    await performNativeNavigation(tabId, target.toString());
  };

  const runNavigationAction = useCallback(async (
    tabId: string,
    action: "back" | "forward" | "reload" | "stop" | "focus",
  ) => {
    const owner = ownerRef.current;
    const generation = ownerGenerationRef.current;
    if (!owner) return;
    const request = (navigationRequestsRef.current.get(tabId) ?? 0) + 1;
    navigationRequestsRef.current.set(tabId, request);
    const isCurrent = () => ownerRef.current === owner
      && ownerGenerationRef.current === generation
      && navigationRequestsRef.current.get(tabId) === request;
    try {
      const accepted = await embeddedBrowserRunAction(owner, tabId, action);
      if (!isCurrent()) return;
      if (accepted) return;
      await reconcileNativeTab(tabId);
      if (!isCurrent()) return;
      setTabs((current) => current.map((tab) => tab.id === tabId
        ? { ...tab, loading: false, error: `浏览器未接受${action === "reload" ? "刷新" : action === "stop" ? "停止" : action === "back" ? "后退" : "前进"}操作。` }
        : tab));
    } catch (error) {
      if (!isCurrent()) return;
      await reconcileNativeTab(tabId).catch(() => false);
      if (!isCurrent()) return;
      setTabs((current) => current.map((tab) => tab.id === tabId
        ? { ...tab, loading: false, error: error instanceof Error ? error.message : "浏览器操作失败。" }
        : tab));
    }
  }, [reconcileNativeTab]);

  useEffect(() => {
    if (!isDesktop() || !browserHydrated) return;
    return subscribeBrowserRequests((request) => {
      if (request.conversationId !== ownerRef.current) return;
      acknowledgeBrowserRequest(request.id);
      if (request.kind === "open") {
        openTab(request.url);
        return;
      }
      if (request.kind === "resume") {
        const tab = tabsRef.current.find((candidate) => candidate.id === request.targetId);
        if (tab) {
          activeIdRef.current = tab.id;
          setActiveId(tab.id);
          if (tab.url !== request.url) void performNativeNavigation(tab.id, request.url);
        } else {
          openTab(request.url);
        }
        return;
      }
      if (request.workspaceRoot !== normalizeWorkspaceRoot(useAppStore.getState().workingDirectory)) return;
      for (const tab of tabsRef.current) {
        if (createdIdsRef.current.has(tab.id) && previewUrlsShareOrigin(tab.url, request.url)) {
          void runNavigationAction(tab.id, "reload");
        }
      }
    });
  }, [browserHydrated, conversationId, openTab, performNativeNavigation, runNavigationAction]);

  const closeTab = async (tabId: string) => {
    const index = tabs.findIndex((tab) => tab.id === tabId);
    if (index < 0) return;
    const owner = ownerRef.current;
    const generation = ownerGenerationRef.current;
    if (!owner) return;
    if (createdIdsRef.current.has(tabId) || navigationRequestsRef.current.has(tabId)) {
      try {
        const closed = await embeddedBrowserClose(owner, tabId);
        if (ownerRef.current !== owner || ownerGenerationRef.current !== generation) return;
        if (!closed) {
          await reconcileNativeTab(tabId);
          if (ownerRef.current !== owner || ownerGenerationRef.current !== generation) return;
          setTabs((current) => current.map((tab) => (
            tab.id === tabId ? { ...tab, error: "Desktop browser rejected the close request." } : tab
          )));
          return;
        }
      } catch (error) {
        if (ownerRef.current !== owner || ownerGenerationRef.current !== generation) return;
        await reconcileNativeTab(tabId).catch(() => false);
        if (ownerRef.current !== owner || ownerGenerationRef.current !== generation) return;
        setTabs((current) => current.map((tab) => (
          tab.id === tabId
            ? { ...tab, error: error instanceof Error ? error.message : "关闭标签页失败。" }
            : tab
        )));
        return;
      }
    }
    visibleIdsRef.current.delete(tabId);
    createdIdsRef.current.delete(tabId);
    navigationRequestsRef.current.delete(tabId);
    const liveTabs = tabsRef.current.filter((tab) => tab.id !== tabId);
    if (liveTabs.length === 0) {
      const replacement = blankTab();
      setTabs([replacement]);
      setActiveId(replacement.id);
      return;
    }
    const liveIndex = tabsRef.current.findIndex((tab) => tab.id === tabId);
    setTabs(liveTabs);
    if (activeIdRef.current === tabId) {
      const next = liveTabs[Math.min(Math.max(liveIndex, 0), liveTabs.length - 1)];
      setActiveId(next.id);
    }
  };

  const saveAnnotation = () => {
    const note = annotationNote.trim();
    if (!note || !activeTab?.url) return;
    for (const selectedElement of (pickedElements.length ? pickedElements : [null])) {
    const id = `browser_note_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
    const selector = selectedElement?.selector || annotationSelector.trim();
    const viewport = selectedElement?.viewport;
    const rect = selectedElement?.rect;
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
    }
    updateAnnotationDraft({ ...EMPTY_ANNOTATION_DRAFT, elements: [] });
    const store = useAppStore.getState();
    const chatPanel = store.panelSlots.find((slot) => slot.kind === "chat");
    if (chatPanel) store.focusPanel(chatPanel.id);
    else store.addPanel({ id: "main-chat", kind: "chat", label: "Chat" });
    window.dispatchEvent(new Event("composer:focus"));
    const workspaceRoot = store.workingDirectory;
    requestAnimationFrame(() => {
      const current = useAppStore.getState();
      if (current.conversationId !== conversationId || current.workingDirectory !== workspaceRoot) return;
      document.querySelector<HTMLTextAreaElement>("[data-composer-input]")?.focus();
    });
  };

  const reviewPageChanges = () => {
    openReview();
    const store = useAppStore.getState();
    store.setDiffReviewState({
      ...store.diffReview!,
      previewReturnTarget: {
        conversationId,
        tab: "browser",
        url: activeTab.url,
        targetId: activeTab.id,
      },
    });
  };

  const pickPageTarget = async (kind: "element" | "region") => {
    if (!activeTab?.url || pickerMode) return;
    const owner = ownerRef.current;
    const generation = pageGenerationRef.current;
    const tabId = activeTab.id;
    setPickerMode(kind);
    updateAnnotationDraft({ open: true });
    setInspectorPage(null);
    setSettingsPage(null);
    try {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      const result = await embeddedBrowserInspect(owner, tabId, kind);
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      if (!result?.ok) throw new Error(result?.error || "页面目标选取失败。");
      const value = result?.value as PickedElement | null | undefined;
      if (value === null) return;
      if (!value?.rect || !value.viewport) throw new Error("页面没有返回可用的选取结果。");
      updateAnnotationDraft({
        pickedElement: value,
        elements: [...pickedElements.filter((item) => !value.selector || item.selector !== value.selector), value],
        selector: value.selector || "",
      });
    } catch (error) {
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      setTabs((current) => current.map((tab) => tab.id === tabId
        ? { ...tab, error: error instanceof Error ? error.message : "页面目标选取失败。" }
        : tab));
    } finally {
      if (pageGenerationRef.current === generation && activeIdRef.current === tabId) {
        setPickerMode(null);
        requestAnimationFrame(() => {
          if (pageGenerationRef.current === generation && activeIdRef.current === tabId) annotationNoteRef.current?.focus();
        });
      }
    }
  };

  const updateBrowserSettings = async (payload: Parameters<typeof embeddedBrowserSetSettings>[0]) => {
    const generation = pageGenerationRef.current;
    const tabId = activeTab.id;
    const url = activeTab.url;
    settingsRevisionRef.current += 1;
    setSettingsSaving(true);
    try {
      const next = await embeddedBrowserSetSettings(payload);
      if (
        pageGenerationRef.current !== generation
        || activeIdRef.current !== tabId
      ) return;
      if (!next) throw new Error("桌面浏览器未确认站点设置更新。");
      settingsRevisionRef.current += 1;
      setBrowserSettings(next);
    } catch (error) {
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      setTabs((current) => current.map((tab) => tab.id === tabId && tab.url === url
        ? { ...tab, error: error instanceof Error ? error.message : "站点设置更新失败。" }
        : tab));
    } finally {
      if (pageGenerationRef.current === generation && activeIdRef.current === tabId) setSettingsSaving(false);
    }
  };

  const clearActiveSiteData = async () => {
    const owner = ownerRef.current;
    const generation = pageGenerationRef.current;
    const tabId = activeTab.id;
    const url = activeTab.url;
    if (!owner || !url) return;
    settingsRevisionRef.current += 1;
    setSettingsSaving(true);
    try {
      const cleared = await embeddedBrowserClearSiteData(owner, tabId);
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      if (!cleared) throw new Error("桌面浏览器未确认清除站点数据。");
      const settings = await embeddedBrowserGetSettings(url);
      if (
        pageGenerationRef.current === generation
        && activeIdRef.current === tabId
        && settings
      ) { settingsRevisionRef.current += 1; setBrowserSettings(settings); }
    } catch (error) {
      if (pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      setTabs((current) => current.map((tab) => tab.id === tabId && tab.url === url
        ? { ...tab, error: error instanceof Error ? error.message : "清除站点数据失败。" }
        : tab));
    } finally {
      if (pageGenerationRef.current === generation && activeIdRef.current === tabId) setSettingsSaving(false);
    }
  };

  const refreshInspector = useCallback(async () => {
    if (!activeTab?.url) return;
    const owner = ownerRef.current;
    const request = ++inspectorRequestRef.current;
    const tabId = activeTab.id;
    setInspectorLoading(true);
    setInspectorError("");
    setDiagnostics([]);
    try {
      const result = await embeddedBrowserInspect(owner, tabId, inspectorKind);
      if (inspectorRequestRef.current !== request) return;
      if (!result?.ok) throw new Error(result?.error || "页面诊断读取失败。");
      setDiagnostics(result.value as BrowserDiagnosticItem[]);
    } catch (error) {
      if (inspectorRequestRef.current !== request) return;
      setInspectorError(error instanceof Error ? error.message : "页面诊断读取失败。");
    } finally {
      if (inspectorRequestRef.current === request) {
        setInspectorLoading(false);
      }
    }
  }, [activeTab?.id, activeTab?.url, inspectorKind]);

  useEffect(() => {
    if (!inspectorOpen) return;
    void refreshInspector();
    window.requestAnimationFrame(syncBounds);
    return () => { inspectorRequestRef.current += 1; };
  }, [inspectorOpen, inspectorKind, refreshInspector, syncBounds]);

  useEffect(() => {
    if (!settingsOpen || !activeTab?.url) return;
    let cancelled = false;
    const generation = pageGenerationRef.current;
    const tabId = activeTab.id;
    const url = activeTab.url;
    const revision = settingsRevisionRef.current;
    setSettingsLoading(true);
    void Promise.resolve(embeddedBrowserGetSettings(url)).then((settings) => {
      if (
        !cancelled
        && pageGenerationRef.current === generation
        && activeIdRef.current === tabId
        && settings
        && settingsRevisionRef.current === revision
      ) setBrowserSettings(settings);
    }).catch((error) => {
      if (cancelled || pageGenerationRef.current !== generation || activeIdRef.current !== tabId) return;
      setTabs((current) => current.map((tab) => tab.id === tabId && tab.url === url
        ? { ...tab, error: error instanceof Error ? error.message : "无法读取站点设置。" }
        : tab));
    }).finally(() => { if (!cancelled && pageGenerationRef.current === generation && activeIdRef.current === tabId) setSettingsLoading(false); });
    window.requestAnimationFrame(syncBounds);
    return () => { cancelled = true; };
  }, [activeTab.id, activeTab.url, settingsOpen, syncBounds]);

  if (!isDesktop()) {
    return (
      <div className="mc-browser-panel"><div className="mc-browser-toolbar"><PreviewServerManager /></div><div className="mc-browser-unavailable">
        <Globe2 size={24} strokeWidth={1.8} />
        <strong>内置浏览器仅在桌面版可用</strong>
        <span>请在 MiniCode 桌面应用中打开网页。</span>
      </div></div>
    );
  }

  if (!conversationId) {
    return (
      <div className="mc-browser-panel">
        <div className="mc-browser-toolbar"><PreviewServerManager /></div>
        <div className="mc-browser-surface" data-empty="true">
          <div className="mc-browser-empty" role="status" tabIndex={0}>
            <Globe2 size={24} strokeWidth={1.8} aria-hidden="true" />
            <strong>请先选择会话，再使用浏览器</strong>
            <span>从侧边栏选择一个会话，或创建新会话后打开浏览器。</span>
          </div>
        </div>
      </div>
    );
  }

  if (!browserHydrated) {
    return (
      <div className="mc-browser-panel">
        <div className="mc-browser-toolbar"><PreviewServerManager /></div>
        <div className="mc-browser-surface">
          <div className="mc-browser-empty" role="status">
            <LoaderCircle className="mc-browser-spin" size={24} />
            <strong>正在恢复浏览器标签页…</strong>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mc-browser-panel">
      <div className="mc-browser-tabs" role="tablist" aria-label="浏览器标签页" onKeyDown={navigateTabList}>
        <div className="mc-browser-tabs-scroll">
          {tabs.map((tab) => (
            <div key={tab.id} className="mc-browser-tab" data-active={tab.id === activeId ? "true" : "false"}>
              <button
                type="button"
                role="tab"
                aria-selected={tab.id === activeId}
                tabIndex={tab.id === activeId ? 0 : -1}
                title={tab.title}
                onClick={() => setActiveId(tab.id)}
              >
                {tab.loading
                  ? <LoaderCircle className="mc-browser-spin" size={14} />
                  : <BrandIcon value={`${tab.title} ${tab.url}`} iconUrl={tab.faviconUrl} websiteUrl={tab.url} fallback="web" size={14} />}
                <span>{tab.title || "新标签页"}</span>
              </button>
              <button type="button" aria-label={`关闭 ${tab.title || "标签页"}`} onClick={() => void closeTab(tab.id)}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
        <button type="button" className="mc-browser-new-tab" aria-label="新建标签页" title="新建标签页" onClick={() => openTab()}>
          <Plus size={16} />
        </button>
        <PreviewServerManager />
      </div>

      <div className="mc-browser-toolbar">
        <button
          type="button"
          aria-label="后退"
          title="后退"
          disabled={!activeTab.canGoBack}
          onClick={() => void runNavigationAction(activeTab.id, "back")}
        >
          <ArrowLeft size={16} />
        </button>
        <button
          type="button"
          aria-label="前进"
          title="前进"
          disabled={!activeTab.canGoForward}
          onClick={() => void runNavigationAction(activeTab.id, "forward")}
        >
          <ArrowRight size={16} />
        </button>
        <button
          type="button"
          aria-label={activeTab.loading ? "停止加载" : "刷新"}
          title={activeTab.loading ? "停止加载" : "刷新"}
          disabled={!activeTab.url}
          onClick={() => void runNavigationAction(activeTab.id, activeTab.loading ? "stop" : "reload")}
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
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
            }}
            spellCheck={false}
          />
        </form>
        <label className="mc-browser-device"><Smartphone size={15} /><select aria-label="预览设备"
          value={viewport ? (viewport.width === 390 ? "phone" : viewport.width === 768 ? "tablet" : viewport.width === 1440 ? "desktop" : "custom") : "auto"}
          onChange={(event) => setViewport(event.target.value === "auto" ? null : {
            phone: { width: 390, height: 844, mobile: true }, tablet: { width: 768, height: 1024, mobile: true },
            desktop: { width: 1440, height: 900, mobile: false }, custom: { width: 1024, height: 768, mobile: false },
          }[event.target.value]!)}>
          <option value="auto">自适应</option><option value="phone">手机</option><option value="tablet">平板</option><option value="desktop">桌面</option><option value="custom">自定义</option>
        </select></label>
        <button
          type="button"
          className="mc-browser-select-target"
          aria-label="选择元素"
          title={pickerMode === "element" ? "在页面中点击目标，按 Esc 退出" : "选择页面元素并描述修改"}
          disabled={!activeTab.url || pickerMode != null}
          onClick={() => void pickPageTarget("element")}
        >
          {pickerMode === "element" ? <LoaderCircle className="mc-browser-spin" size={15} /> : <Crosshair size={15} />}
          <span className="mc-browser-select-label">{pickerMode === "element" ? "点击目标…" : "选择元素"}</span>
        </button>
        <button
          type="button"
          aria-label="添加页面批注"
          title="添加页面批注"
          disabled={!activeTab.url}
          onClick={() => {
            updateAnnotationDraft({ open: !annotationOpen });
            setInspectorPage(null);
            setSettingsPage(null);
          }}
        >
          <MessageSquarePlus size={16} />
        </button>
        {activeTab.url && turnChanges && (
          <button
            type="button"
            aria-label={`审阅本轮 ${turnChanges.files.length} 个文件更改`}
            title={`审阅本轮更改 · ${turnChanges.files.length} 个文件 · +${turnChanges.additions} -${turnChanges.deletions}`}
            onClick={reviewPageChanges}
          >
            <FileDiff size={16} />
          </button>
        )}
        <button type="button" aria-label="更多浏览器操作" title="更多浏览器操作" aria-haspopup="menu" aria-expanded={moreMenuPosition != null} onClick={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setMoreMenuPosition(moreMenuPosition ? null : { x: bounds.right - 190, y: bounds.bottom + 4 });
        }}><MoreHorizontal size={16} /></button>
      </div>

      {moreMenuPosition && <ContextMenu position={moreMenuPosition} onClose={() => setMoreMenuPosition(null)} items={[
        { label: "在系统浏览器中打开", icon: <ExternalLink size={14} />, disabled: !activeTab.url, onClick: () => void openExternal(activeTab.url) },
        { label: "打开页面诊断", icon: <Bug size={14} />, disabled: !activeTab.url, onClick: () => { setInspectorPage(inspectorOpen ? null : pageKey); updateAnnotationDraft({ open: false }); setSettingsPage(null); } },
        { label: "打开站点设置", icon: <Settings2 size={14} />, disabled: !activeTab.url, onClick: () => { setSettingsPage(settingsOpen ? null : pageKey); updateAnnotationDraft({ open: false }); setInspectorPage(null); } },
      ]} />}

      {settingsOpen && activeTab.url && (
        <section className="mc-browser-settings" aria-label="站点设置">
          <div className="mc-browser-settings-heading">
            <Settings2 size={14} />
            <strong>{browserSettings.origin || activeTab.url}</strong>
            <button
              type="button"
              aria-label="清除站点数据"
              title="清除站点数据"
              disabled={settingsLoading || settingsSaving}
              onClick={() => void clearActiveSiteData()}
            >
              <Trash2 size={14} />
            </button>
          </div>
          <div className="mc-browser-setting-row">
            <span><Download size={14} /> 下载</span>
            <SelectMenu
              ariaLabel="下载策略"
              value={browserSettings.downloadPolicy}
              disabled={settingsLoading || settingsSaving}
              onValueChange={(value) => void updateBrowserSettings({ downloadPolicy: value as EmbeddedBrowserSettings["downloadPolicy"] })}
            >
              <option value="block">阻止</option>
              <option value="ask">每次询问</option>
              <option value="allow">保存到下载目录</option>
            </SelectMenu>
          </div>
          <div className="mc-browser-permissions" aria-label="站点权限">
            {sitePermissionOptions.map(([permission, label]) => (
              <label className="mc-browser-setting-row" key={permission}>
                <span>{label}</span>
                <input
                  type="checkbox"
                  disabled={settingsLoading || settingsSaving}
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
            <div role="tablist" aria-label="诊断类别" onKeyDown={navigateTabList}>
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "console"}
                tabIndex={inspectorKind === "console" ? 0 : -1}
                onClick={() => setInspectorKind("console")}
              >
                <Bug size={14} /> 控制台
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "network"}
                tabIndex={inspectorKind === "network" ? 0 : -1}
                onClick={() => setInspectorKind("network")}
              >
                <Network size={14} /> 网络
              </button>
            </div>
            <button type="button" onClick={() => void refreshInspector()} disabled={inspectorLoading}>
              <RefreshCw className={inspectorLoading ? "mc-browser-spin" : undefined} size={14} /> 刷新
            </button>
          </div>
          <div className="mc-browser-diagnostic-filters">
            <input aria-label="搜索页面诊断" placeholder="搜索消息或 URL" value={diagnosticQuery} onChange={(event) => setDiagnosticQuery(event.target.value)} />
            <select aria-label="筛选页面诊断" value={diagnosticFilter} onChange={(event) => setDiagnosticFilter(event.target.value)}>
              <option value="all">全部</option><option value="errors">错误</option>
              {inspectorKind === "console" ? <option value="warnings">警告</option> : <option value="fetch">Fetch / XHR</option>}
            </select><small>{visibleDiagnostics.length} / {diagnostics.length}</small>
          </div>
          <div className="mc-browser-inspector-list">
            {inspectorError ? (
              <span role="alert" className="mc-browser-inspector-empty">{inspectorError}</span>
            ) : inspectorLoading ? (
              <span className="mc-browser-inspector-empty">正在读取页面诊断…</span>
            ) : visibleDiagnostics.length === 0 ? (
              <span className="mc-browser-inspector-empty">尚未记录{inspectorKind === "console" ? "控制台" : "网络"}事件</span>
            ) : visibleDiagnostics.slice().reverse().map((item, index) => (
              <div className="mc-browser-inspector-row" key={`${item.timestamp ?? "event"}-${index}`}>
                <time>{diagnosticTimestamp(item.timestamp)}</time>
                {inspectorKind === "console" ? (
                  <span title={item.message}>{item.message || "控制台消息"}</span>
                ) : (
                  <>
                    <b>{item.statusCode || "ERR"}</b>
                    <span>{item.method || "GET"}</span>
                    <span title={item.url}>{item.url || item.error || "网络请求"}</span>
                    {item.durationMs != null && <small>{Math.round(item.durationMs)} ms</small>}
                  </>
                )}
                <details className="mc-browser-diagnostic-detail"><summary>详情</summary>
                  <pre>{JSON.stringify(item, null, 2)}</pre>
                  {diagnosticSource(item) && <button type="button" onClick={() => { const source = diagnosticSource(item)!; useAppStore.getState().openEditorFile(source.path, undefined, { line: source.line, exact: true }); }}>打开源码</button>}
                  <button type="button" onClick={() => {
                    addSelectedMention({ kind: "browser_annotation", path: "browser-diagnostic:" + crypto.randomUUID(), name: "页面诊断",
                      url: activeTab.url, targetId: activeTab.id, note: JSON.stringify(item, null, 2) });
                    window.dispatchEvent(new Event("composer:focus"));
                  }}>加入对话</button>
                </details>
              </div>
            ))}
          </div>
        </section>
      )}

      {viewport && <div className="mc-browser-viewport-controls" role="group" aria-label="设备视口">
        {(["width", "height"] as const).map((axis) => <label key={axis}>{axis === "width" ? "宽" : "高"} <NumberInput aria-label={axis === "width" ? "视口宽度" : "视口高度"} min={240} max={3840} value={viewport[axis]} onCommit={(value) => setViewport({ ...viewport, [axis]: value })} /></label>)}
        <span>px</span><button type="button" onClick={() => setViewport({ ...viewport, width: viewport.height, height: viewport.width })}>旋转</button><small>按可用空间缩放显示，网页以设定尺寸布局</small>
        <button type="button" aria-label="恢复自适应视口" onClick={() => setViewport(null)}><X size={14} /></button>
      </div>}
      {annotationOpen && activeTab.url && (
        <div className="mc-browser-annotation" role="region" aria-label="页面批注">
          <div className="mc-browser-annotation-heading">
            <MessageSquarePlus size={15} />
            <span>页面批注</span>
            <small>{activeTab.title || activeTab.url}</small>
          </div>
          <div className="mc-browser-picker-actions">
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
          {pickedElements.map((element, index) => <div key={index} className="mc-browser-selected-target" title={element.selector || "页面区域"}>
            <Crosshair size={14} /><strong>{element.text || element.selector || "选中的区域"}</strong>
            <small>已选择 {Math.round(element.rect.width)} × {Math.round(element.rect.height)} px</small>
            {element.source && <button type="button" onClick={() => useAppStore.getState().openEditorFile(element.source!.path, undefined, { line: element.source!.line, column: element.source!.column, exact: true })}>源代码</button>}
            <button type="button" aria-label={"移除元素 " + (index + 1)} onClick={() => updateAnnotationDraft({ elements: pickedElements.filter((_, itemIndex) => itemIndex !== index), pickedElement: null, selector: "" })}><X size={13} /></button>
          </div>)}
          <textarea
            ref={annotationNoteRef}
            value={annotationNote}
            onChange={(event) => updateAnnotationDraft({ note: event.target.value })}
            placeholder="描述需要修复或验证的内容"
            aria-label="批注内容"
            rows={3}
            autoFocus={pickerMode == null}
          />
          <details className="mc-browser-element-details">
            <summary>高级元素信息</summary>
            <input value={annotationSelector} onChange={(event) => updateAnnotationDraft({ selector: event.target.value, pickedElement: null, elements: [] })} placeholder="元素选择器（可选，例如 #save）" aria-label="元素选择器" spellCheck={false} />
          </details>
          <div className="mc-browser-annotation-actions">
            <button type="button" onClick={() => updateAnnotationDraft({ open: false })}>收起</button>
            <button type="button" disabled={!annotationNote.trim()} onClick={saveAnnotation}>加入对话</button>
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
