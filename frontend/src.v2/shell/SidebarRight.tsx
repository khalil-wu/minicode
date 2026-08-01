import {
  Bot,
  FileDiff,
  FileSearch,
  Globe2,
  HeartPulse,
  Layers,
  MonitorPlay,
  Plus,
  PanelRightOpen,
  PanelRightClose,
  X,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { RIGHT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ScrollablePanel } from "./SidebarShared";
import { ActivityTab } from "./tabs/ActivityTab";

type StackTab = "preview" | "browser" | "terminal" | "tasks" | "diff" | "plan" | "subagents" | "artifacts" | "inspector" | "diagnostics";

interface SidebarRightProps {
  embedded?: boolean;
  initialTab?: StackTab | "details" | "context";
}

const normalizeInitialTab = (tab: SidebarRightProps["initialTab"]): StackTab => {
  if (tab === "details") return "inspector";
  if (tab === "context") return "tasks";
  if (tab === "plan" || tab === "terminal") return "tasks";
  return tab ?? "tasks";
};

const defaultOpenTabs: StackTab[] = ["tasks"];
const sidebarIconProps = { size: 16, strokeWidth: 1.85 } as const;

const shouldAllowAutomaticTabSwitch = (activeTab: StackTab, rightPanelOpen: boolean): boolean => {
  if (!rightPanelOpen) return true;
  return activeTab === "preview" || activeTab === "tasks";
};

const LazyPreviewPanel = lazy(() =>
  import("../panels/PreviewPanel").then((module) => ({ default: module.PreviewPanel })),
);
const LazyBrowserPanel = lazy(() =>
  import("../panels/BrowserPanel").then((module) => ({ default: module.BrowserPanel })),
);
const LazyDiffPanel = lazy(() =>
  import("../panels/DiffPanel").then((module) => ({ default: module.DiffPanel })),
);
const LazySubagentsTab = lazy(() =>
  import("./tabs/SubagentsTab").then((module) => ({ default: module.SubagentsTab })),
);
const LazyArtifactsTab = lazy(() =>
  import("./tabs/ArtifactsTab").then((module) => ({ default: module.ArtifactsTab })),
);
const LazyInspectorTab = lazy(() =>
  import("./tabs/InspectorTab").then((module) => ({ default: module.InspectorTab })),
);
const LazyDiagnosticsTab = lazy(() =>
  import("./tabs/DiagnosticsTab").then((module) => ({ default: module.DiagnosticsTab })),
);

const preferredManualSidebarWidth = (tab: StackTab): number => {
  switch (tab) {
    case "browser":
      return 720;
    case "diff":
      return 640;
    case "preview":
      return 560;
    case "artifacts":
    case "inspector":
    case "plan":
    case "subagents":
    case "diagnostics":
    case "tasks":
    default:
      return RIGHT_SIDEBAR_DEFAULT_WIDTH;
  }
};

export const SidebarRight = ({ embedded = false, initialTab = "tasks" }: SidebarRightProps) => {
  const messages = useAppStore((s) => s.messages);
  const conversationId = useAppStore((s) => s.conversationId);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const rightStackTabLocked = useAppStore((s) => s.rightStackTabLocked);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightSidebarWidth = useAppStore((s) => s.rightSidebarWidth);
  const setRightSidebarWidth = useAppStore((s) => s.setRightSidebarWidth);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [openTabIds, setOpenTabIds] = useState<StackTab[]>(() => Array.from(new Set([...defaultOpenTabs, normalizeInitialTab(initialTab)])));
  const [launcherOpen, setLauncherOpen] = useState(false);
  const tabListRef = useRef<HTMLDivElement | null>(null);
  const requestedActiveTab = embedded ? localTab : rightStackTab;
  const normalizedRequestedTab = requestedActiveTab === "plan" || requestedActiveTab === "terminal" ? "tasks" : requestedActiveTab;
  const activeTab = normalizedRequestedTab;
  const setActiveTab = embedded ? setLocalTab : setRightStackTab;

  useEffect(() => {
    setOpenTabIds((current) => {
      return Array.from(new Set([...defaultOpenTabs, ...current]));
    });
  }, []);

  // 用户手动切换tab时锁定自动切换
  const lockAndSetTab = useCallback((tab: StackTab) => {
    setUserTabLocked(true);
    setActiveTab(tab);
  }, [setActiveTab]);
  const subagents = useAppStore((s) => s.subagents);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const previewArtifact = useAppStore((s) => s.previewArtifact);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const [userTabLocked, setUserTabLocked] = useState(false);
  const allowAutoSwitch = !embedded && !rightStackTabLocked && !userTabLocked && shouldAllowAutomaticTabSwitch(activeTab, rightPanelOpen);

  // Switching conversations must not rebuild or reorder the user's open panel
  // tabs. Only release the automatic-switch lock for the new conversation.
  useEffect(() => {
    setUserTabLocked(false);
  }, [conversationId]);

  useEffect(() => {
    if (!allowAutoSwitch) return;
    if (livePreviewUrl) {
      setRightStackTab("preview", { automatic: true });
    }
  }, [allowAutoSwitch, livePreviewUrl, setRightStackTab]);

  const runningSubagents = subagents.filter((subagent) => subagent.status === "running").length;
  const mcpErrors = mcpServers.filter((s) => s.status === "error").length;
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "tasks", label: "上下文", icon: <PanelRightOpen {...sidebarIconProps} /> },
    { id: "subagents", label: "子智能体", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Bot {...sidebarIconProps} /> },
    { id: "artifacts", label: "产物", badge: previewArtifact ? "1" : undefined, icon: <Layers {...sidebarIconProps} /> },
    { id: "inspector", label: "检查器", icon: <FileSearch {...sidebarIconProps} /> },
    { id: "diff", label: "审阅", badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff {...sidebarIconProps} /> },
    { id: "preview", label: "预览", badge: livePreviewUrl || previewArtifact ? "开" : undefined, icon: <MonitorPlay {...sidebarIconProps} /> },
    { id: "browser", label: "浏览器", icon: <Globe2 {...sidebarIconProps} /> },
    { id: "diagnostics", label: "运行状态", badge: mcpErrors ? String(mcpErrors) : undefined, icon: <HeartPulse {...sidebarIconProps} /> },
  ];
  const activeItem = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const tabById = useMemo(() => new Map(tabs.map((tab) => [tab.id, tab])), [tabs]);
  const openedTabs = openTabIds
    .map((id) => tabById.get(id))
    .filter((tab): tab is { id: StackTab; label: string; badge?: string; icon: React.ReactNode } => Boolean(tab));
  const addOpenTab = useCallback((tab: StackTab) => {
    setOpenTabIds((current) => current.includes(tab) ? current : [...current, tab]);
  }, []);
  const openRightTab = (tab: StackTab) => {
    activateTab(tab);
    setRightSidebarWidth(preferredManualSidebarWidth(tab));
  };
  const closeOpenTab = useCallback((tab: StackTab) => {
    const tabIndex = openTabIds.indexOf(tab);
    if (tabIndex < 0 || openTabIds.length <= 1) return;
    const nextTabs = openTabIds.filter((id) => id !== tab);
    setOpenTabIds(nextTabs);
    if (activeTab !== tab) return;
    const fallbackTab = nextTabs[Math.min(tabIndex, nextTabs.length - 1)] ?? "tasks";
    lockAndSetTab(fallbackTab);
    setRightSidebarWidth(preferredManualSidebarWidth(fallbackTab));
  }, [activeTab, lockAndSetTab, openTabIds, setRightSidebarWidth]);
  useEffect(() => {
    addOpenTab(activeTab);
  }, [activeTab, addOpenTab]);

  useEffect(() => {
    tabListRef.current
      ?.querySelector<HTMLElement>(`[data-sidebar-tab="${activeTab}"]`)
      ?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
  }, [activeTab, openTabIds]);

  useEffect(() => {
    if (diffReview || gitChangeCount > 0) addOpenTab("diff");
  }, [addOpenTab, diffReview, gitChangeCount]);

  useEffect(() => {
    if (livePreviewUrl || previewArtifact) addOpenTab("preview");
  }, [addOpenTab, livePreviewUrl, previewArtifact]);

  const activePrimaryTab = openedTabs.some((tab) => tab.id === activeTab);
  const compactPanel = ["tasks", "inspector", "subagents", "artifacts", "diagnostics"].includes(activeTab);
  const minSidebarWidth = embedded ? 0 : compactPanel ? 360 : 420;
  const effectiveResizeMin = embedded ? 0 : minSidebarWidth;
  const sidebarWidth = compactPanel
    ? Math.min(420, Math.max(minSidebarWidth, rightSidebarWidth))
    : Math.max(minSidebarWidth, rightSidebarWidth);
  const sidebarWidthStyle = embedded ? "100%" : rightPanelOpen ? `${sidebarWidth}px` : "0px";
  const sidebarMinWidth = embedded ? 0 : rightPanelOpen ? minSidebarWidth : 0;
  const sidebarMaxWidth = embedded ? "none" : rightPanelOpen ? "min(1040px, calc(100vw - 720px))" : "0px";
  const activateTab = (tab: StackTab) => {
    addOpenTab(tab);
    lockAndSetTab(tab);
    setLauncherOpen(false);
  };

  const moveTabFocus = (index: number) => {
    window.setTimeout(() => {
      const id = openedTabs[index]?.id;
      if (!id) return;
      tabListRef.current?.querySelector<HTMLButtonElement>(`[data-sidebar-tab="${id}"]`)?.focus();
    }, 0);
  };
  const handlePrimaryTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % openedTabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + openedTabs.length) % openedTabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = openedTabs.length - 1;
    else return;
    event.preventDefault();
    activateTab(openedTabs[nextIndex].id);
    moveTabFocus(nextIndex);
  };

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (embedded) return;
    event.preventDefault();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startWidth = sidebarWidth;
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      setRightSidebarWidth(startWidth + startX - moveEvent.clientX);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", cleanup);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.body.classList.remove("layout-dragging");
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("layout-dragging");
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", cleanup);
  };

  const resetSidebarWidth = () => {
    if (embedded) return;
    setRightSidebarWidth(preferredManualSidebarWidth(activeTab));
  };
  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = sidebarWidth + step;
    else if (event.key === "ArrowRight") nextWidth = sidebarWidth - step;
    else if (event.key === "Home") nextWidth = effectiveResizeMin;
    else if (event.key === "End") nextWidth = 1040;
    else if (event.key === "Enter") nextWidth = preferredManualSidebarWidth(activeTab);
    if (nextWidth == null) return;
    event.preventDefault();
    setRightSidebarWidth(nextWidth);
  };

  return (
    <aside
      className="mc-sidebar-right relative flex flex-col overflow-hidden"
      data-embedded={embedded ? "true" : "false"}
      data-open={embedded || rightPanelOpen ? "true" : "false"}
      aria-hidden={!embedded && !rightPanelOpen}
      style={{
        "--right-sidebar-width": `${sidebarWidth}px`,
        position: "relative",
        alignSelf: "stretch",
        margin: embedded || !rightPanelOpen ? 0 : "8px 8px 8px 0",
        zIndex: embedded ? undefined : 2,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        width: sidebarWidthStyle,
        minWidth: sidebarMinWidth,
        maxWidth: sidebarMaxWidth,
        flex: embedded ? "1 1 auto" : `0 0 ${rightPanelOpen ? sidebarWidth : 0}px`,
        background: "var(--surface-base)",
        border: embedded || !rightPanelOpen ? 0 : "1px solid var(--border-subtle)",
        borderRadius: embedded ? 0 : 14,
        boxShadow: embedded || !rightPanelOpen
          ? "none"
          : "0 8px 24px color-mix(in oklch, black 12%, transparent)",
        opacity: embedded || rightPanelOpen ? 1 : 0,
        transform: embedded || rightPanelOpen ? "translateX(0)" : "translateX(8px)",
        visibility: embedded || rightPanelOpen ? "visible" : "hidden",
        pointerEvents: embedded || rightPanelOpen ? "auto" : "none",
        transition: `width 220ms var(--easing-standard), min-width 220ms var(--easing-standard), max-width 220ms var(--easing-standard), flex-basis 220ms var(--easing-standard), margin 220ms var(--easing-standard), opacity 180ms var(--easing-standard), transform 220ms var(--easing-enter), border-color 180ms var(--easing-standard), box-shadow 220ms var(--easing-standard), visibility 0s linear ${embedded || rightPanelOpen ? 0 : 220}ms`,
      } as React.CSSProperties}
    >
      {!embedded && (
        <div
          className="mc-sidebar-right-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整右侧栏宽度"
           aria-valuemin={effectiveResizeMin}
          aria-valuemax={1040}
           aria-valuenow={Math.round(sidebarWidth)}
           aria-valuetext={`${Math.round(sidebarWidth)} pixels`}
           tabIndex={0}
          title="调整侧栏宽度"
          onPointerDown={startResize}
           onDoubleClick={resetSidebarWidth}
           onKeyDown={handleResizeKeyDown}
          style={resizeHandleStyle}
        />
      )}
      <div className="mc-sidebar-right-header">
        <div ref={tabListRef} className="mc-sidebar-right-tabs" role="tablist" aria-label="右侧栏面板">
        {openedTabs.map((t, index) => (
          <div
            key={t.id}
            className="mc-sidebar-right-tab-frame"
            data-active={activeTab === t.id ? "true" : "false"}
            style={sidebarTabFrameStyle(activeTab === t.id)}
          >
            <button
              type="button"
              role="tab"
              id={`right-tab-${t.id}`}
              aria-selected={activeTab === t.id}
              aria-controls={`right-panel-${t.id}`}
              data-sidebar-tab={t.id}
              onClick={() => activateTab(t.id)}
              onKeyDown={(event) => handlePrimaryTabKeyDown(event, index)}
              title={t.label}
              aria-label={`打开${t.label}`}
              className="mc-sidebar-right-tab"
              style={sidebarTabButtonStyle(activeTab === t.id)}
            >
              <span className="mc-sidebar-tab-icon">{t.icon}</span>
              <span className="mc-sidebar-tab-label">{t.label}</span>
              {t.badge && <span className="mc-sidebar-tab-badge">{t.badge}</span>}
            </button>
            {openedTabs.length > 1 && (
              <button
                type="button"
                title={`关闭${t.label}标签页`}
                aria-label={`关闭${t.label}标签页`}
                onClick={(event) => {
                  event.stopPropagation();
                  closeOpenTab(t.id);
                }}
                className="mc-sidebar-right-tab-close mc-icon-button mc-icon-button-compact"
                style={sidebarTabCloseStyle(activeTab === t.id)}
              >
                <X size={14} />
              </button>
            )}
          </div>
        ))}
        </div>
        <div className="mc-sidebar-right-actions" data-testid="right-sidebar-actions">
          <button
            type="button"
            onClick={() => setLauncherOpen((open) => !open)}
            title="添加面板"
            aria-label="添加面板"
            aria-pressed={launcherOpen}
            className="mc-sidebar-right-action mc-sidebar-right-action-add mc-icon-button"
            data-active={launcherOpen ? "true" : "false"}
          >
            <Plus size={18} strokeWidth={1.8} />
          </button>
          {!embedded && (
            <button
            type="button"
            onClick={toggleRightPanel}
            title="关闭面板"
            aria-label="关闭右侧栏"
            className="mc-sidebar-right-action mc-icon-button"
          >
              <PanelRightClose size={16} strokeWidth={1.8} />
            </button>
          )}
        </div>
      </div>
      <div
        key={launcherOpen ? "launcher" : activeTab}
        className="mc-sidebar-panel-view flex-1 overflow-hidden text-sm flex flex-col"
        id={`right-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={!launcherOpen && activePrimaryTab ? `right-tab-${activeTab}` : undefined}
        aria-label={launcherOpen ? "面板选择" : activePrimaryTab ? undefined : `${activeItem.label}面板`}
        style={{ background: "var(--surface-base)", minHeight: 0, minWidth: 0 }}
      >
        <div className="panel-content-wrapper" style={{ minHeight: 0, minWidth: 0, overflow: "hidden" }}>
          {launcherOpen && (
            <nav className="mc-sidebar-launcher" aria-label="面板选择">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  aria-label={tab.label}
                  data-active={activeTab === tab.id ? "true" : undefined}
                  onClick={() => openRightTab(tab.id)}
                >
                  {tab.icon}
                  <span>{tab.label}</span>
                  {tab.badge && <small>{tab.badge}</small>}
                </button>
              ))}
            </nav>
          )}
          {!launcherOpen && activeTab === "preview" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="preview" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="预览" />}>
                  <LazyPreviewPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {!launcherOpen && activeTab === "browser" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="preview" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="浏览器" />}>
                  <LazyBrowserPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {!launcherOpen && activeTab === "tasks" && <ScrollablePanel><ActivityTab /></ScrollablePanel>}
          {!launcherOpen && activeTab === "diff" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="diff" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="审阅" />}>
                  <LazyDiffPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {!launcherOpen && activeTab === "subagents" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="subagents" />}>
                <ScrollablePanel><LazySubagentsTab /></ScrollablePanel>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {!launcherOpen && activeTab === "artifacts" && <Suspense fallback={<PanelSkeleton kind="artifacts" />}><ScrollablePanel><LazyArtifactsTab /></ScrollablePanel></Suspense>}
          {!launcherOpen && activeTab === "inspector" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyInspectorTab /></ScrollablePanel></Suspense>}
          {!launcherOpen && activeTab === "diagnostics" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyDiagnosticsTab /></ScrollablePanel></Suspense>}
        </div>
      </div>
    </aside>
  );
};

// ── Styles ─────────────────────────────────────────────────────

const resizeHandleStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  bottom: 0,
  left: 0,
  width: 9,
  cursor: "col-resize",
  zIndex: 30,
  background: "transparent",
  transform: "translateX(-4px)",
  touchAction: "none",
};

const sidebarTabFrameStyle = (active: boolean): React.CSSProperties => ({
  height: "var(--mc-sidebar-tab-height, 32px)",
  maxWidth: 152,
  minWidth: 0,
  flex: "0 0 auto",
  borderRadius: "var(--radius-md, 8px)",
  background: active ? "var(--surface-page)" : "transparent",
  borderColor: active ? "var(--border-subtle)" : "transparent",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  overflow: "hidden",
});

const sidebarTabButtonStyle = (active: boolean): React.CSSProperties => ({
  height: "100%",
  minWidth: 0,
  padding: "0 3px 0 9px",
  background: "transparent",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  fontWeight: active ? 620 : 520,
  whiteSpace: "nowrap",
});

const sidebarTabCloseStyle = (active: boolean): React.CSSProperties => ({
  marginRight: 4,
  color: active ? "var(--text-muted)" : "color-mix(in oklch, var(--text-muted) 72%, transparent)",
});
