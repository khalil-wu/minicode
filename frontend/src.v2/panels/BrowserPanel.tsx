import { createPortal } from "react-dom";
import { openNewSideChat } from "../chat/openSideChat";
import { useI18n } from "../i18n";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Bug,
  Crosshair,
  Download,
  ExternalLink,
  FileDiff,
  FolderOpen,
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
  TerminalSquare,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { ContextMenu } from "../components/ContextMenu";
import {
  embeddedBrowserActivate,
  embeddedBrowserCapture,
  embeddedBrowserClose,
  embeddedBrowserGetSettings,
  embeddedBrowserInspect,
  embeddedBrowserList,
  embeddedBrowserNavigate,
  embeddedBrowserRunAction,
  embeddedBrowserSetBounds,
  isDesktop,
  onEmbeddedBrowserEvent,
  onEmbeddedBrowserSettingsChanged,
  openExternal,
  type EmbeddedBrowserState,
} from "../desktop/runtime";
import { parseHttpUrl } from "../lib/network-target";
import { previewUrlsShareOrigin } from "../lib/preview-projection";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { BrandIcon } from "../components/BrandIcon";
import { SelectMenu } from "../components/SelectMenu";
import { useAppStore } from "../stores";
import { useTurnChanges } from "../chat/useTurnChanges";
import { sendChatMessage } from "../chat/sendChatMessage";
import { captureSteerTurnId } from "../lib/steer-command";
import {
  acknowledgeBrowserRequest,
  subscribeBrowserRequests,
} from "../chat/openWebInBrowser";
import "./BrowserPanel.css";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { BrowserDeviceToolbar, BrowserDeviceResizeHandles, browserDeviceLayout, BrowserDownloads, BrowserFindBar, BrowserPageActions, BrowserSiteInfo, DEFAULT_DEVICE_VIEWPORT, type BrowserDeviceLayout, type BrowserDeviceViewport } from "./BrowserPageActions";
import { captureBrowserAnnotationAttachments } from "./browser-annotation-capture";
import { liveConversationWorkbenchState } from "../stores/ui-slice";
import { pushToast } from "../overlays/ToastContainer";
import { formatShortcut, matchesShortcut } from "../lib/keyboard-shortcuts";

export type BrowserTabSummary = { id: string; title: string; active: boolean };

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
const COMPOSER_REGION = ".workbench-primary .chat-pane-composer-region";
const RENDERER_BOUNDS_ELEMENTS = `${RENDERER_OVERLAYS}, ${COMPOSER_REGION}`;

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
  draftUrl: event.url && event.url !== (tab.url || "about:blank")
    ? event.url === "about:blank" ? "" : event.url
    : tab.draftUrl,
  loading: event.loading,
  canGoBack: event.canGoBack,
  canGoForward: event.canGoForward,
  faviconUrl: event.faviconUrl ?? tab.faviconUrl,
  error: event.type === "error" ? event.error || "页面加载失败。" : undefined,
});

export const BrowserPanel = ({ tabHosts, tabInteractions, onTabsChanged, onTabClosed, onLastTabClosed }: {
  tabHosts?: Record<string, HTMLElement>;
  tabInteractions?: (id: string) => import("react").HTMLAttributes<HTMLDivElement>;
  onTabsChanged?: (tabs: BrowserTabSummary[]) => void;
  onTabClosed?: (id: string, active: boolean) => void;
  onLastTabClosed?: () => void;
}) => {
  const { t } = useI18n();
  const { preferences } = useDesktopPreferences();
  const [addressFocused, setAddressFocused] = useState(false);
  const conversationId = useAppStore((state) => state.conversationId) || "";
  const shortcutBindings = useAppStore((state) => state.shortcutBindings);
  const [terminalMenuAnchor, setTerminalMenuAnchor] = useState<HTMLButtonElement | null>(null);
  const rightStackTab = useAppStore((state) => state.rightStackTab);
  const browserVisible = useAppStore((state) => state.rightPanelOpen && state.rightStackTab === "browser");
  const floatingComposerVisible = useAppStore((state) => state.rightPanelExpanded && state.rightPanelOpen);
  const addBrowserAnnotation = useAppStore((state) => state.addBrowserAnnotation);
  const addSelectedMention = useAppStore((state) => state.addSelectedMention);
  const isStreaming = useAppStore((state) => state.isStreaming);
  const isConnected = useAppStore((state) => state.isConnected);
  const [tabs, setRenderedTabs] = useState<BrowserTab[]>(() => [blankTab()]);
  const [activeId, setActiveId] = useState(() => tabs[0].id);
  useEffect(() => { onTabsChanged?.(tabs.map(({ id, title }) => ({ id, title, active: id === activeId }))); }, [onTabsChanged, tabs, activeId]);
  const placeholderTab = useRef(tabs[0].id);
  const [hydratedConversationId, setHydratedConversationId] = useState<string | null>(null);
  const browserHydrated = hydratedConversationId === conversationId;
  const [annotationDrafts, setAnnotationDrafts] = useState<Record<string, AnnotationDraft>>({});
  const [feedbackSubmittingPage, setFeedbackSubmittingPage] = useState<string | null>(null);
  const [feedbackStatus, setFeedbackStatus] = useState<Record<string, string>>({});
  const [feedbackBusyBehavior, setFeedbackBusyBehavior] = useState<"queue" | "steer">("queue");
  const [pickerMode, setPickerMode] = useState<"element" | "region" | null>(null);
  const [moreMenuPosition, setMoreMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const [downloadsOpen, setDownloadsOpen] = useState(false);
  useEffect(() => {
    const openDownloads = () => setDownloadsOpen(true);
    window.addEventListener("browser:downloads", openDownloads);
    return () => window.removeEventListener("browser:downloads", openDownloads);
  }, []);
  const [inspectorPage, setInspectorPage] = useState<string | null>(null);
  const [inspectorKind, setInspectorKind] = useState<InspectorKind>("console");
  const [diagnostics, setDiagnostics] = useState<BrowserDiagnosticItem[]>([]);
  const [diagnosticQuery, setDiagnosticQuery] = useState("");
  const [diagnosticFilter, setDiagnosticFilter] = useState("all");
  const [viewports, setViewports] = useState<Record<string, BrowserDeviceViewport | null>>({});
  const viewport = viewports[activeId] ?? null;
  const setViewport = (next: BrowserDeviceViewport | null) => setViewports(current => ({ ...current, [activeId]: next }));
  const alwaysShowDeviceToolbar = useRef(false);
  const [devicePreferenceLoaded, setDevicePreferenceLoaded] = useState(false);
  useEffect(() => {
    if (!devicePreferenceLoaded) return;
    setViewports(current => current[activeId] === undefined
      ? { ...current, [activeId]: alwaysShowDeviceToolbar.current ? { ...DEFAULT_DEVICE_VIEWPORT } : null } : current);
  }, [activeId, devicePreferenceLoaded]);
  const viewportRef = useRef(viewport);
  viewportRef.current = viewport;
  const [inspectorLoading, setInspectorLoading] = useState(false);
  const [inspectorError, setInspectorError] = useState("");
  const [settingsPage, setSettingsPage] = useState<string | null>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const deviceResizeActive = useRef(false);
  const [deviceFrame, setDeviceFrame] = useState<{ id: string; owner: string; layout: BrowserDeviceLayout } | null>(null);
  const activeDeviceLayout = deviceFrame?.id === activeId && deviceFrame.owner === conversationId ? deviceFrame.layout : null;
  const occlusion = useRef<{ id: string; owner: string; url: string; ready: boolean } | null>(null);
  const [occludedSnapshot, setOccludedSnapshot] = useState<{ id: string; owner: string; url: string; src: string; height: number; token: { id: string; owner: string; url: string; ready: boolean } } | null>(null);
  const overlaysRef = useRef(new Set<HTMLElement>());
  const addressRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focusAddress = () => {
      if (!browserVisible) return;
      addressRef.current?.focus();
      addressRef.current?.select();
    };
    window.addEventListener("browser:focus-address", focusAddress);
    return () => window.removeEventListener("browser:focus-address", focusAddress);
  }, [browserVisible]);
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
  const navigationRequestsRef = useRef(new Map<string, number>());
  const activeTab = useMemo(
    () => tabs.find((tab) => tab.id === activeId) ?? tabs[0],
    [activeId, tabs],
  );
  useEffect(() => {
    if (!isDesktop()) return;
    void embeddedBrowserGetSettings("")!.then(settings => {
      alwaysShowDeviceToolbar.current = settings!.deviceToolbarAlwaysOnNewTabs!;
      setDevicePreferenceLoaded(true);
    });
    return onEmbeddedBrowserSettingsChanged(settings => { alwaysShowDeviceToolbar.current = settings.deviceToolbarAlwaysOnNewTabs; });
  }, []);
  const { summary: turnChanges, openReview } = useTurnChanges({
    conversationId, tab: "browser", url: activeTab.url, targetId: activeTab.id,
  });
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
    occlusion.current = null;
    setOccludedSnapshot(null);
    pageGenerationRef.current += 1;
    inspectorRequestRef.current += 1;
    setPickerMode(null);
    setMoreMenuPosition(null);
    setInspectorPage(null);
    setInspectorLoading(false);
    setInspectorError("");
    setDiagnostics([]);
    setSettingsPage(null);
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
    placeholderTab.current = initialTab.id;
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
    const trackedElements = Array.from(overlaysRef.current);
    const composer = trackedElements.find((item) => item.matches('.workbench-primary[data-floating="true"] .chat-pane-composer-region'));
    const composerRect = composer?.getBoundingClientRect();
    // A native WebContentsView cannot be covered by an HTML composer. Reserve
    // its bottom strip while keeping the same page, tab, and browser viewport.
    const bottom = composerRect && composerRect.width > 0
      && composerRect.left < rect.right && composerRect.right > rect.left
      ? Math.min(rect.bottom, composerRect.top - 8) : rect.bottom;
    const stage = { x: rect.left, y: rect.top, width: rect.width, height: Math.max(0, bottom - rect.top) };
    const device = viewportRef.current ? browserDeviceLayout(stage, viewportRef.current) : null;
    const frame = device?.visualBounds || stage;
    if (device) setDeviceFrame(current => current?.id === id && current.owner === owner
      && current.layout.scale === device.scale && current.layout.stageBounds.x === stage.x && current.layout.stageBounds.y === stage.y
      && current.layout.stageBounds.width === stage.width && current.layout.stageBounds.height === stage.height
      && current.layout.visualBounds.width === frame.width && current.layout.visualBounds.height === frame.height ? current : { id, owner, layout: device });
    else setDeviceFrame(current => current === null ? current : null);
    // WebContentsView sits above the renderer, regardless of CSS z-index.
    // Yield its surface to overlapping menus and modal backdrops; retain the
    // same tab and navigation state when the renderer overlay closes.
    const obscured = deviceResizeActive.current || trackedElements.some((overlay) => {
      if (overlay.matches(COMPOSER_REGION) || overlay.contains(element)) return false;
      const bounds = overlay.getBoundingClientRect();
      return bounds.width > 0 && bounds.height > 0 && getComputedStyle(overlay).visibility !== "hidden"
        && (overlay.getAttribute("aria-modal") === "true"
          || (bounds.left < frame.x + frame.width && bounds.right > frame.x && bounds.top < frame.y + frame.height && bounds.bottom > frame.y));
    });
    const url = tabsRef.current.find(tab => tab.id === id)!.url;
    if (obscured && rect.width > 0 && bottom > rect.top) {
      if (occlusion.current?.id !== id || occlusion.current.owner !== owner || occlusion.current.url !== url) {
        const pending = { id, owner, url, ready: false };
        occlusion.current = pending;
        // Capture the same visible native owner before yielding it to the
        // renderer. This image is only an in-memory composition surface.
        void embeddedBrowserCapture({ id, conversationId: owner, expectedUrl: url }).then(capture => {
          if (occlusion.current === pending) setOccludedSnapshot({ id, owner, url, src: `data:${capture.mimeType};base64,${capture.data}`,
            height: stage.height, token: pending });
        }).catch(error => {
          if (occlusion.current !== pending) return;
          pushToast(String(error), "error");
          pending.ready = true;
          syncBounds();
        });
      }
      // The image load event yields the native surface only after its actual
      // pixels can be painted. Closing the overlay invalidates this request.
      if (!occlusion.current.ready) return;
    } else if (occlusion.current) {
      occlusion.current = null;
      setOccludedSnapshot(null);
    }
    void embeddedBrowserSetBounds({
      id,
      conversationId: owner,
      x: frame.x,
      y: frame.y,
      width: obscured ? 0 : frame.width,
      height: obscured ? 0 : frame.height,
      viewport: viewportRef.current && device ? { ...viewportRef.current, scale: device.scale } : null,
    });
  }, []);
  useEffect(() => { syncBounds(); }, [viewport, floatingComposerVisible, syncBounds]);
  const setDeviceResizeActive = useCallback((active: boolean) => { deviceResizeActive.current = active; syncBounds(); }, [syncBounds]);

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
      const overlays = [...node.querySelectorAll<HTMLElement>(RENDERER_BOUNDS_ELEMENTS)];
      if (node.matches(RENDERER_BOUNDS_ELEMENTS)) overlays.push(node);
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
    if (!reusableBlank) setViewports(current => ({ ...current, [tab.id]: alwaysShowDeviceToolbar.current ? { ...DEFAULT_DEVICE_VIEWPORT } : null }));
    activeIdRef.current = tab.id;
    setActiveId(tab.id);
    if (requestedUrl) {
      void performNativeNavigation(tab.id, requestedUrl);
    } else {
      window.setTimeout(() => addressRef.current?.focus(), 0);
    }
  }, [performNativeNavigation]);

  const removeClosedTab = useCallback((tabId: string) => {
    visibleIdsRef.current.delete(tabId);
    createdIdsRef.current.delete(tabId);
    navigationRequestsRef.current.delete(tabId);
    const index = tabsRef.current.findIndex((tab) => tab.id === tabId);
    if (index < 0) return;
    const liveTabs = tabsRef.current.filter((tab) => tab.id !== tabId);
    if (liveTabs.length === 0) {
      const replacement = blankTab();
      setTabs([replacement]);
      activeIdRef.current = replacement.id;
      setActiveId(replacement.id);
      onLastTabClosed?.();
    } else {
      setTabs(liveTabs);
      const wasActive = activeIdRef.current === tabId;
      if (activeIdRef.current === tabId) {
        const next = liveTabs[Math.min(index, liveTabs.length - 1)];
        activeIdRef.current = next.id;
        setActiveId(next.id);
      }
      onTabClosed?.(tabId, wasActive);
    }
  }, [setTabs, onLastTabClosed, onTabClosed]);

  useEffect(() => {
    if (!isDesktop()) return;
    const unsubscribe = onEmbeddedBrowserEvent((event) => {
      if (event.conversationId !== ownerRef.current) return;
      // The global desktop listener owns automatic presentation, including
      // current-owner and user-dismissal checks while this panel is unmounted.
      if (event.type === "presentation-requested") return;
      if (event.type === "find-result" || event.type === "find-request" || event.type === "print-request") return;
      if (event.type === "numbered-shortcut" || event.type === "external-link-request") {
        const state = useAppStore.getState();
        if (state.conversationId !== event.conversationId || state.pendingConversationSwitchId
          || event.id !== activeIdRef.current || !createdIdsRef.current.has(event.id)
          || !state.rightPanelOpen || state.rightStackTab !== "browser") return;
        if (event.type === "numbered-shortcut") {
          const { target, index } = event.numberedShortcut!;
          window.dispatchEvent(new CustomEvent(target === "tabs" ? "select-numbered-tab" : "select-numbered-chat", {
            detail: { index, conversationId: event.conversationId, surface: "sidebar" },
          }));
        } else {
          void openExternal(event.requestedUrl!);
        }
        return;
      }
      if (event.type === "closed") {
        removeClosedTab(event.id);
        return;
      }
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
        const tab = updateTabFromEvent(blankTab(event.id), event);
        const openerIndex = current.findIndex(candidate => candidate.id === event.insertToRightOfTabId);
        return openerIndex < 0 ? [...current, tab]
          : [...current.slice(0, openerIndex + 1), tab, ...current.slice(openerIndex + 1)];
      });
      if (!knownTab) {
        setViewports(current => ({ ...current, [event.id]: alwaysShowDeviceToolbar.current ? { ...DEFAULT_DEVICE_VIEWPORT } : null }));
        if (event.active !== false) {
          activeIdRef.current = event.id;
          setActiveId(event.id);
        }
      }
      if (event.id === activeIdRef.current && event.url !== "about:blank") {
        window.requestAnimationFrame(syncBounds);
      }
    });
    return () => unsubscribe?.();
  }, [openTab, removeClosedTab]);

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
      if (request.kind === "focus-address" && !browserVisible) return;
      acknowledgeBrowserRequest(request.id);
      if (request.kind === "focus-address") {
        window.requestAnimationFrame(() => {
          if (request.conversationId !== ownerRef.current) return;
          addressRef.current?.focus();
          addressRef.current?.select();
        });
        return;
      }
      if (request.kind === "present") {
        const target = request.target;
        createdIdsRef.current.add(target.id);
        visibleIdsRef.current.add(target.id);
        setTabs((current) => current.some((tab) => tab.id === target.id)
          ? current
          : [...current.filter((tab) => tab.url || createdIdsRef.current.has(tab.id)), updateTabFromEvent(blankTab(target.id), target)]);
        activeIdRef.current = target.id;
        setActiveId(target.id);
        // Activation and real native bounds are sent by the existing layout
        // effect after React commits this same target's browser surface.
        window.requestAnimationFrame(syncBounds);
        return;
      }
      if (request.kind === "open") {
        // Present the existing native target. Explicit new tabs are created by
        // the tab control; readiness events must not multiply the same page.
        const existing = tabsRef.current.find((tab) => tab.url === request.url);
        if (existing) {
          activeIdRef.current = existing.id;
          setActiveId(existing.id);
          window.requestAnimationFrame(syncBounds);
        } else openTab(request.url);
        return;
      }
      if (request.kind === "new") {
        const placeholder = tabsRef.current.find(tab => tab.id === placeholderTab.current && !tab.url);
        if (placeholder) { setActiveId(placeholder.id); placeholderTab.current = ""; }
        else openTab();
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
      if (!workspaceRootsEqual(request.workspaceRoot, useAppStore.getState().workingDirectory)) return;
      for (const tab of tabsRef.current) {
        if (createdIdsRef.current.has(tab.id) && previewUrlsShareOrigin(tab.url, request.url)) {
          void runNavigationAction(tab.id, "reload");
        }
      }
    });
  }, [browserHydrated, conversationId, openTab, performNativeNavigation, runNavigationAction, setTabs, syncBounds, browserVisible]);

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
    removeClosedTab(tabId);
  };

  const annotationItems = () => {
    const note = annotationNote.trim();
    return (pickedElements.length ? pickedElements : [null]).map((selectedElement) => {
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
    const reference = {
      kind: "browser_annotation" as const,
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
    };
    return { annotation, reference };
    });
  };

  const saveAnnotation = async () => {
    if (!annotationNote.trim() || !activeTab?.url) return;
    const owner = conversationId;
    const initialWorkbench = liveConversationWorkbenchState(useAppStore.getState());
    const workspaceRoot = useAppStore.getState().workingDirectory;
    const submittedPage = pageKey;
    const submittedDraft = annotationDraft;
    const items = annotationItems();
    setFeedbackSubmittingPage(submittedPage);
    try {
      const captured = await captureBrowserAnnotationAttachments({ conversationId: owner, workspaceRoot, targetId: activeTab.id, url: activeTab.url, elements: pickedElements });
      useAppStore.setState((state) => {
        if (state.conversationId === owner) return { selectedMentions: [...state.selectedMentions, ...items.map((item) => item.reference)], attachments: [...state.attachments, ...captured.composerAttachments] };
        const workbench = state.conversationWorkbenchStates[owner] ?? initialWorkbench;
        return { conversationWorkbenchStates: { ...state.conversationWorkbenchStates, [owner]: { ...workbench,
          selectedMentions: [...workbench.selectedMentions, ...items.map((item) => item.reference)], attachments: [...workbench.attachments, ...captured.composerAttachments],
        } } };
      });
      if (useAppStore.getState().conversationId === owner) items.forEach(({ annotation }) => addBrowserAnnotation(annotation));
      setAnnotationDrafts((drafts) => drafts[submittedPage] === submittedDraft ? { ...drafts, [submittedPage]: { ...EMPTY_ANNOTATION_DRAFT, elements: [] } } : drafts);
    } catch (error) {
      setFeedbackStatus((statuses) => ({ ...statuses, [submittedPage]: error instanceof Error ? error.message : String(error) }));
      return;
    } finally { setFeedbackSubmittingPage((current) => current === submittedPage ? null : current); }
    const store = useAppStore.getState();
    if (store.conversationId !== owner) return;
    const chatPanel = store.panelSlots.find((slot) => slot.kind === "chat");
    if (chatPanel) store.focusPanel(chatPanel.id);
    else store.addPanel({ id: "main-chat", kind: "chat", label: "Chat" });
    window.dispatchEvent(new Event("composer:focus"));
    requestAnimationFrame(() => {
      const current = useAppStore.getState();
      if (current.conversationId !== conversationId || current.workingDirectory !== workspaceRoot) return;
      document.querySelector<HTMLTextAreaElement>("[data-composer-input]")?.focus();
    });
  };

  const sendAnnotation = async () => {
    if (!annotationNote.trim() || !activeTab.url || feedbackSubmittingPage === pageKey) return;
    const submittedPage = pageKey;
    const submittedDraft = annotationDraft;
    const owner = conversationId;
    const expectedTurnId = feedbackBusyBehavior === "steer" ? captureSteerTurnId(useAppStore.getState(), owner) : undefined;
    const items = annotationItems();
    setFeedbackSubmittingPage(submittedPage);
    let captured;
    try {
      captured = await captureBrowserAnnotationAttachments({ conversationId: owner, workspaceRoot: useAppStore.getState().workingDirectory, targetId: activeTab.id, url: activeTab.url, elements: pickedElements });
    } catch (error) {
      setFeedbackStatus((statuses) => ({ ...statuses, [submittedPage]: error instanceof Error ? error.message : String(error) }));
      setFeedbackSubmittingPage((current) => current === submittedPage ? null : current);
      return;
    }
    let admissionStatus = "";
    const accepted = await sendChatMessage({
      displayContent: annotationNote.trim(), backendContent: annotationNote.trim(), conversationId: owner,
      contextRefs: items.map((item) => item.reference), allowWhileStreaming: true, busyBehavior: feedbackBusyBehavior,
      expectedTurnId,
      attachments: captured.attachments, attachmentRefs: captured.attachmentRefs,
      awaitAdmission: true,
      onAdmission: (event) => {
        if (event.data?.steered === true) admissionStatus = "已引导当前任务";
        else if (event.data?.queued === true) admissionStatus = "反馈已排队";
        else admissionStatus = "反馈已发送";
      },
    });
    setFeedbackSubmittingPage((current) => current === submittedPage ? null : current);
    if (!accepted) return;
    // Admission owns this feedback. Keep any edits typed while it was in
    // flight, and leave the separate main-composer draft and references intact.
    if (useAppStore.getState().conversationId === owner) items.forEach((item) => addBrowserAnnotation(item.annotation));
    setAnnotationDrafts((drafts) => drafts[submittedPage] === submittedDraft
      ? { ...drafts, [submittedPage]: { ...EMPTY_ANNOTATION_DRAFT, open: true, elements: [] } }
      : drafts);
    setFeedbackStatus((statuses) => ({ ...statuses, [submittedPage]: admissionStatus }));
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

  if (!isDesktop()) {
    return (
      <div className="mc-browser-panel"><div className="mc-browser-unavailable">
        <Globe2 size={24} strokeWidth={1.8} />
        <strong>内置浏览器仅在桌面版可用</strong>
        <span>请在 MiniCode 桌面应用中打开网页。</span>
      </div></div>
    );
  }

  if (!conversationId) {
    return (
      <div className="mc-browser-panel">

        <div className="mc-browser-surface" data-empty="true">
          <div className="mc-browser-empty" role="status" tabIndex={0}>
            <Globe2 size={24} strokeWidth={1.8} aria-hidden="true" />
            <strong>{t("请先选择会话，再使用浏览器")}</strong>
            <span>从侧边栏选择一个会话，或创建新会话后打开浏览器。</span>
          </div>
        </div>
      </div>
    );
  }

  if (!browserHydrated) {
    return (
      <div className="mc-browser-panel">

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
    <div className="mc-browser-panel" onKeyDown={event => {
      if (!(event.ctrlKey || event.metaKey) || !activeTab.url) return;
        if (matchesShortcut(event.nativeEvent, useAppStore.getState().shortcutBindings.find)) { event.preventDefault(); event.stopPropagation(); window.dispatchEvent(new CustomEvent("browser:find", { detail: activeTab.id })); }
    }}>
      {tabHosts ? tabs.map((tab) => tabHosts[tab.id] ? createPortal(
            <div className="mc-browser-tab" {...tabInteractions?.(tab.id)} data-sidebar-tab-frame="browser" data-active={tab.id === activeId && rightStackTab === "browser" ? "true" : "false"}>
              <button
                type="button"
                role="tab"
                id={`right-browser-tab-${tab.id}`}
                aria-selected={tab.id === activeId && rightStackTab === "browser"}
                tabIndex={tab.id === activeId && rightStackTab === "browser" ? 0 : -1}
                aria-controls="right-panel-browser"
                title={tab.title}
                onClick={() => { useAppStore.getState().setRightStackTab("browser"); setActiveId(tab.id); }}
              >
                {tab.loading
                  ? <LoaderCircle className="mc-browser-spin" size={14} />
                  : <BrandIcon value={`${tab.title} ${tab.url}`} iconUrl={tab.faviconUrl} websiteUrl={tab.url} fallback="web" size={14} />}
                <span>{tab.title || t("新标签页")}</span>
              </button>
              <button type="button" aria-label={`关闭 ${tab.title || "标签页"}`} onClick={() => void closeTab(tab.id)}>
                <X size={14} />
              </button>
            </div>
        , tabHosts[tab.id], tab.id) : null) : (
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
                <span>{tab.title || t("新标签页")}</span>
              </button>
              <button type="button" aria-label={`关闭 ${tab.title || "标签页"}`} onClick={() => void closeTab(tab.id)}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
        <button type="button" className="mc-browser-new-tab" aria-label={t("新建标签页")} title={t("新建标签页")} onClick={() => openTab()}>
          <Plus size={16} />
        </button>
      </div>
      )}

      <div className="mc-browser-toolbar">
        <button
          type="button"
          aria-label={t("后退")}
          title={t("后退")}
          disabled={!activeTab.canGoBack}
          onClick={() => void runNavigationAction(activeTab.id, "back")}
        >
          <ArrowLeft size={16} />
        </button>
        <button
          type="button"
          aria-label={t("前进")}
          title={t("前进")}
          disabled={!activeTab.canGoForward}
          onClick={() => void runNavigationAction(activeTab.id, "forward")}
        >
          <ArrowRight size={16} />
        </button>
        <button
          type="button"
          aria-label={activeTab.loading ? t("停止加载") : t("刷新")}
          title={activeTab.loading ? t("停止加载") : t("刷新")}
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
          {activeTab.url && <button type="button" aria-label={t("站点信息")} onClick={() => setSettingsPage(settingsOpen ? null : pageKey)}><Settings2 size={15} /></button>}
          <input
            ref={addressRef}
            value={!addressFocused && preferences && !preferences.showFullUrl && activeTab.url && activeTab.draftUrl === activeTab.url ? new URL(activeTab.url).host : activeTab.draftUrl}
            onChange={(event) => {
              const value = event.target.value;
              setTabs((current) => current.map((tab) => tab.id === activeTab.id ? { ...tab, draftUrl: value } : tab));
            }}
            onFocus={(event) => { setAddressFocused(true); event.currentTarget.value = activeTab.draftUrl; event.currentTarget.select(); }}
            onBlur={() => setAddressFocused(false)}
            placeholder={t("搜索或输入网址")}
            aria-label={t("搜索或输入网址")}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
            }}
            spellCheck={false}
          />
          {activeTab.url && <button type="button" aria-label={t("在外部浏览器中打开")} onClick={() => void openExternal(activeTab.url)}><ExternalLink size={15} /></button>}
        </form>
        <button type="button" aria-label={t("下载")} title={t("下载")} onClick={() => setDownloadsOpen(!downloadsOpen)}><Download size={16} /></button>
        <button type="button" aria-label={t("更多浏览器操作")} title={t("更多浏览器操作")} aria-haspopup="menu" aria-expanded={moreMenuPosition != null} onClick={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setMoreMenuPosition(moreMenuPosition ? null : { x: bounds.right - 190, y: bounds.bottom + 4 });
        }}><MoreHorizontal size={16} /></button>
      </div>

      {moreMenuPosition && <BrowserPageActions position={moreMenuPosition} onClose={() => setMoreMenuPosition(null)} conversationId={conversationId} tabId={activeTab.id} url={activeTab.url}
        viewportEnabled={Boolean(viewport)} onViewportChange={enabled => setViewport(enabled ? { ...DEFAULT_DEVICE_VIEWPORT } : null)} />}
      <BrowserFindBar key={pageKey} conversationId={conversationId} tabId={activeTab.id} />
      {downloadsOpen && <BrowserDownloads onClose={() => setDownloadsOpen(false)} />}

      {settingsOpen && activeTab.url && <BrowserSiteInfo key={pageKey} conversationId={conversationId} tabId={activeTab.id} url={activeTab.url} onClose={() => setSettingsPage(null)} />}

      {inspectorOpen && activeTab.url && (
        <section className="mc-browser-inspector" aria-label={t("页面诊断")}>
          <div className="mc-browser-inspector-heading">
            <div role="tablist" aria-label="诊断类别" onKeyDown={navigateTabList}>
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "console"}
                tabIndex={inspectorKind === "console" ? 0 : -1}
                onClick={() => { setInspectorKind("console"); setDiagnosticFilter("all"); }}
              >
                <Bug size={14} />{t("控制台")}</button>
              <button
                type="button"
                role="tab"
                aria-selected={inspectorKind === "network"}
                tabIndex={inspectorKind === "network" ? 0 : -1}
                onClick={() => { setInspectorKind("network"); setDiagnosticFilter("all"); }}
              >
                <Network size={14} />{t("网络")}</button>
            </div>
            <button type="button" onClick={() => void refreshInspector()} disabled={inspectorLoading}>
              <RefreshCw className={inspectorLoading ? "mc-browser-spin" : undefined} size={14} />{t("刷新")}</button>
          </div>
          <div className="mc-browser-diagnostic-filters">
            <input aria-label={t("搜索页面诊断")} placeholder={t("搜索消息或 URL")} value={diagnosticQuery} onChange={(event) => setDiagnosticQuery(event.target.value)} />
            <SelectMenu ariaLabel={t("筛选页面诊断")} align="end" value={diagnosticFilter} onValueChange={setDiagnosticFilter}>
              <option value="all">{t("全部")}</option><option value="errors">{t("错误")}</option>
              {inspectorKind === "console" ? <option value="warnings">{t("警告")}</option> : <option value="fetch">Fetch / XHR</option>}
            </SelectMenu><small>{visibleDiagnostics.length} / {diagnostics.length}</small>
          </div>
          <div className="mc-browser-inspector-list">
            {inspectorError ? (
              <span role="alert" className="mc-browser-inspector-empty">{inspectorError}</span>
            ) : inspectorLoading ? (
              <span className="mc-browser-inspector-empty">正在读取页面诊断…</span>
            ) : visibleDiagnostics.length === 0 ? (
              <span className="mc-browser-inspector-empty">尚未记录{inspectorKind === "console" ? t("控制台") : t("网络")}事件</span>
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
                  {diagnosticSource(item) && <button type="button" onClick={() => { const source = diagnosticSource(item)!; useAppStore.getState().openEditorFile(source.path, undefined, { line: source.line, exact: true }); }}>{t("打开源码")}</button>}
                  <button type="button" onClick={() => {
                    addSelectedMention({ kind: "browser_annotation", path: "browser-diagnostic:" + crypto.randomUUID(), name: "页面诊断",
                      url: activeTab.url, targetId: activeTab.id, note: JSON.stringify(item, null, 2) });
                    window.dispatchEvent(new Event("composer:focus"));
                  }}>{t("加入对话")}</button>
                </details>
              </div>
            ))}
          </div>
        </section>
      )}

      {viewport && activeTab.url && <BrowserDeviceToolbar key={pageKey} conversationId={conversationId} tabId={activeTab.id} viewport={viewport} onChange={setViewport} />}
      {annotationOpen && activeTab.url && (
        <div className="mc-browser-annotation" role="region" aria-label={t("页面批注")}>
          <div className="mc-browser-annotation-heading">
            <MessageSquarePlus size={15} />
            <span>{t("页面批注")}</span>
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
              {pickerMode === "region" ? "在页面中拖拽区域…" : t("框选区域")}
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
            aria-label={t("批注内容")}
            rows={3}
            autoFocus={pickerMode == null}
          />
          <details className="mc-browser-element-details">
            <summary>{t("高级元素信息")}</summary>
            <input value={annotationSelector} onChange={(event) => updateAnnotationDraft({ selector: event.target.value, pickedElement: null, elements: [] })} placeholder={t("元素选择器（可选，例如 #save）")} aria-label={t("元素选择器")} spellCheck={false} />
          </details>
          <div className="mc-browser-annotation-actions">
            <button type="button" onClick={() => updateAnnotationDraft({ open: false })}>{t("收起")}</button>
            <button type="button" disabled={!isConnected || !annotationNote.trim() || feedbackSubmittingPage === pageKey} onClick={() => void saveAnnotation()}>{t("加入对话")}</button>
            {isStreaming && <SelectMenu ariaLabel={t("页面反馈发送方式")} value={feedbackBusyBehavior} onValueChange={(value) => setFeedbackBusyBehavior(value as "queue" | "steer")}><option value="queue">{t("排队发送")}</option><option value="steer">{t("立即引导")}</option></SelectMenu>}
            <button type="button" className="mc-browser-feedback-send" aria-label={feedbackSubmittingPage === pageKey ? "正在提交页面反馈" : "发送页面反馈"} title={isStreaming ? feedbackBusyBehavior === "queue" ? "排队发送反馈" : "引导当前任务" : "发送页面反馈"}
              disabled={!isConnected || !annotationNote.trim() || feedbackSubmittingPage === pageKey} onClick={() => void sendAnnotation()}>
              {feedbackSubmittingPage === pageKey ? <LoaderCircle className="mc-browser-spin" size={15} /> : <ArrowUp size={15} />}
            </button>
          </div>
          {feedbackStatus[pageKey] && <div className="mc-browser-feedback-status" role="status">{feedbackStatus[pageKey]}</div>}
        </div>
      )}

      {activeTab.error && (
        <div className="mc-browser-error" role="alert">
          <span>{activeTab.error}</span>
          <button type="button" onClick={() => void navigate(activeTab.id, activeTab.draftUrl)}>{t("重试")}</button>
        </div>
      )}

      <div ref={surfaceRef} className="mc-browser-surface" data-empty={!activeTab.url ? "true" : "false"} data-device={Boolean(viewport && activeTab.url)}>
        {viewport && activeTab.url && activeDeviceLayout && (viewport.presetId || "responsive") === "responsive" && <BrowserDeviceResizeHandles key={pageKey}
          viewport={viewport} layout={activeDeviceLayout} onChange={setViewport} onResizeActiveChange={setDeviceResizeActive} />}
        {occludedSnapshot?.id === activeTab.id && occludedSnapshot.owner === conversationId && occludedSnapshot.url === activeTab.url && <img
          className="mc-browser-occluded-snapshot" src={occludedSnapshot.src} alt="" aria-hidden="true" style={activeDeviceLayout && viewport
            ? { left: activeDeviceLayout.visualBounds.x - activeDeviceLayout.stageBounds.x, top: activeDeviceLayout.visualBounds.y - activeDeviceLayout.stageBounds.y, width: activeDeviceLayout.visualBounds.width, height: activeDeviceLayout.visualBounds.height }
            : { left: 0, top: 0, width: "100%", height: occludedSnapshot.height }}
          onLoad={() => {
            if (occlusion.current === occludedSnapshot.token) {
              occlusion.current.ready = true;
              syncBounds();
            }
          }} />}
        {!activeTab.url && (
          <div className="mc-browser-empty">
            <div className="mc-browser-new-tools" aria-label={t("工具")}>
              <h3>{t("工具")}</h3>
              <div className="mc-browser-tool-grid">
              {([
                ["变更", FileDiff, "sidePanel", () => useAppStore.getState().setRightStackTab("diff")],
                ["终端", TerminalSquare, "terminal", () => useAppStore.getState().setRightStackTab("terminal")],
                ...conversationId ? [["侧边聊天", MessageSquarePlus, "openSideChat", () => { const state = useAppStore.getState(); openNewSideChat({ conversationId, workspaceRoot: state.workingDirectory, permissionMode: state.permissionMode }); }] as const] : [],
                ["文件", FolderOpen, "fileTree", () => useAppStore.getState().setRightStackTab("files")],
              ] as const).map(([label, Icon, shortcut, action]) => <div className="mc-browser-tool-card" key={label}>
                <button type="button" aria-label={t(label)} onClick={() => { action(); void closeTab(activeTab.id); }}>
                  <Icon size={16} strokeWidth={1.5} aria-hidden="true" /><span>{t(label)}</span>
                  {shortcutBindings[shortcut].length > 0 && <kbd>{formatShortcut(shortcutBindings[shortcut][0])}</kbd>}
                </button>
                {label === "终端" && <button type="button" className="mc-browser-terminal-options" aria-label={t("终端选项")}
                  aria-haspopup="menu" aria-expanded={Boolean(terminalMenuAnchor)} onClick={event => setTerminalMenuAnchor(event.currentTarget)}><MoreHorizontal size={16} /></button>}
              </div>)}
              </div>
              {terminalMenuAnchor && <ContextMenu anchor={terminalMenuAnchor} returnFocusTo={terminalMenuAnchor} position={{ x: 0, y: 0 }}
                ariaLabel={t("终端选项")} onClose={() => setTerminalMenuAnchor(null)} items={[{
                  label: t("打开底部终端"), icon: <TerminalSquare size={16} />, shortcut: formatShortcut(shortcutBindings.bottomPanel),
                  onClick: () => { useAppStore.getState().openBottomTab("terminal"); void closeTab(activeTab.id); },
                }]} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
