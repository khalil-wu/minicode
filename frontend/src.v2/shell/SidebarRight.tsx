import {
  Bot,
  FileDiff,
  FileSearch,
  FolderOpen,
  Globe2,
  HeartPulse,
  Layers,
  MonitorPlay,
  MessageCirclePlus,
  Plus,
  PanelRightOpen,
  PanelRightClose,
  X,
  TerminalSquare,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { RIGHT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import type { RightStackTab } from "../stores/types";
import { ContextMenu } from "../components/ContextMenu";
import { SideChatPanel } from "../panels/SideChatPanel";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ScrollablePanel } from "./SidebarShared";
import { ActivityTab } from "./tabs/ActivityTab";
import {
  selectActiveConversationPreview,
  selectPreviewSurface,
} from "../lib/preview-projection";

type StackTab = RightStackTab;

interface SidebarRightProps {
  embedded?: boolean;
  visible?: boolean;
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

export const SidebarRight = ({ embedded = false, visible = true, initialTab }: SidebarRightProps) => {
  const messages = useAppStore((s) => s.messages);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightSidebarWidth = useAppStore((s) => s.rightSidebarWidth);
  const setRightSidebarWidth = useAppStore((s) => s.setRightSidebarWidth);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const closeSideChat = useAppStore((s) => s.closeSideChat);
  const openBottomTab = useAppStore((s) => s.openBottomTab);
  const toggleQuickOpen = useAppStore((s) => s.toggleQuickOpen);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [openTabIds, setOpenTabIds] = useState<StackTab[]>(() => Array.from(new Set([...defaultOpenTabs, normalizeInitialTab(initialTab)])));
  const [launcherPosition, setLauncherPosition] = useState<{ x: number; y: number } | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const tabListRef = useRef<HTMLDivElement | null>(null);
  const launcherButtonRef = useRef<HTMLButtonElement | null>(null);
  const requestedActiveTab = initialTab === undefined ? rightStackTab : localTab;
  const normalizedRequestedTab = requestedActiveTab === "plan" || requestedActiveTab === "terminal" ? "tasks" : requestedActiveTab;
  const activeTab = normalizedRequestedTab;
  const setActiveTab = initialTab === undefined ? setRightStackTab : setLocalTab;
  const closeLauncher = useCallback(() => setLauncherPosition(null), []);
  useEffect(() => {
    if (!visible) closeLauncher();
  }, [visible, closeLauncher]);

  const subagents = useAppStore((s) => s.subagents);
  const activePreviewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact);
  const previewSurfaceArtifact = useAppStore((s) => selectPreviewSurface(s).previewArtifact);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const runningSubagents = subagents.filter((subagent) => subagent.status === "running").length;
  const mcpErrors = mcpServers.filter((s) => s.status === "error").length;
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "tasks", label: "上下文", icon: <PanelRightOpen {...sidebarIconProps} /> },
    { id: "diff", label: "审阅", badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff {...sidebarIconProps} /> },
    { id: "preview", label: "预览", badge: previewSurfaceArtifact ? "开" : undefined, icon: <MonitorPlay {...sidebarIconProps} /> },
    { id: "browser", label: "浏览器", icon: <Globe2 {...sidebarIconProps} /> },
    { id: "sidechat", label: "侧边聊天", icon: <MessageCirclePlus {...sidebarIconProps} /> },
    { id: "artifacts", label: "产物", badge: activePreviewArtifact ? "1" : undefined, icon: <Layers {...sidebarIconProps} /> },
    { id: "subagents", label: "子智能体", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Bot {...sidebarIconProps} /> },
    { id: "inspector", label: "运行详情", icon: <FileSearch {...sidebarIconProps} /> },
    { id: "diagnostics", label: "运行状态", badge: mcpErrors ? String(mcpErrors) : undefined, icon: <HeartPulse {...sidebarIconProps} /> },
  ];
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
    if (tabIndex < 0) return;
    const nextTabs = openTabIds.filter((id) => id !== tab);
    setOpenTabIds(nextTabs);
    if (tab === "sidechat") closeSideChat();
    if (nextTabs.length === 0) {
      setActiveTab("tasks");
      if (useAppStore.getState().rightPanelOpen) toggleRightPanel();
      return;
    }
    if (activeTab !== tab) return;
    const fallbackTab = nextTabs[Math.min(tabIndex, nextTabs.length - 1)] ?? "tasks";
    setActiveTab(fallbackTab);
    setRightSidebarWidth(preferredManualSidebarWidth(fallbackTab));
    window.requestAnimationFrame(() => {
      tabListRef.current?.querySelector<HTMLButtonElement>(`[data-sidebar-tab="${fallbackTab}"]`)?.focus();
    });
  }, [activeTab, setActiveTab, openTabIds, setRightSidebarWidth, closeSideChat, rightPanelOpen, toggleRightPanel]);
  useEffect(() => {
    if (rightPanelOpen && (activeTab !== "sidechat" || sideChatOpen)) addOpenTab(activeTab);
  }, [activeTab, addOpenTab, rightPanelOpen, sideChatOpen]);

  useEffect(() => {
    if (sideChatOpen) {
      addOpenTab("sidechat");
      setActiveTab("sidechat");
    }
  }, [sideChatOpen, addOpenTab, setActiveTab]);
  useEffect(() => {
    if (!sideChatOpen && openTabIds.includes("sidechat")) closeOpenTab("sidechat");
  }, [sideChatOpen, openTabIds, closeOpenTab]);

  useEffect(() => {
    tabListRef.current
      ?.querySelector<HTMLElement>(`[data-sidebar-tab-frame="${activeTab}"]`)
      ?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
  }, [activeTab, openTabIds]);

  useEffect(() => {
    if (diffReview || gitChangeCount > 0) addOpenTab("diff");
  }, [addOpenTab, diffReview, gitChangeCount]);

  useEffect(() => {
    if (previewSurfaceArtifact) addOpenTab("preview");
  }, [addOpenTab, previewSurfaceArtifact]);

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
    setActiveTab(tab);
    closeLauncher();
  };

  const closeRightPanel = () => {
    // The close control lives inside the panel. Blur it before the next render
    // marks the aside aria-hidden; otherwise Chromium retains focus in a
    // hidden subtree and reports a real accessibility violation.
    const active = document.activeElement;
    if (active instanceof HTMLElement && sidebarRef.current?.contains(active)) {
      active.blur();
    }
    toggleRightPanel();
  };

  useLayoutEffect(() => {
    if (embedded || rightPanelOpen) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && sidebarRef.current?.contains(active)) {
      active.blur();
    }
  }, [embedded, rightPanelOpen]);

  useLayoutEffect(() => {
    if (!sidebarRef.current) return;
    (sidebarRef.current as HTMLElement & { inert: boolean }).inert = !visible || (!embedded && !rightPanelOpen);
  }, [embedded, rightPanelOpen, visible]);

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
      ref={sidebarRef}
      className="mc-sidebar-right relative flex flex-col overflow-hidden"
      data-embedded={embedded ? "true" : "false"}
      data-open={visible && (embedded || rightPanelOpen) ? "true" : "false"}
      aria-hidden={!visible || (!embedded && !rightPanelOpen)}
      style={{
        "--right-sidebar-width": `${sidebarWidth}px`,
        position: "relative",
        alignSelf: "stretch",
        margin: 0,
        zIndex: embedded ? undefined : "var(--z-content)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        width: sidebarWidthStyle,
        minWidth: sidebarMinWidth,
        maxWidth: sidebarMaxWidth,
        flex: embedded ? "1 1 auto" : `0 0 ${rightPanelOpen ? sidebarWidth : 0}px`,
        background: "var(--surface-base)",
        border: 0,
        borderLeft: embedded || !rightPanelOpen ? 0 : "1px solid var(--border-subtle)",
        borderRadius: 0,
        boxShadow: "none",
        opacity: embedded || rightPanelOpen ? 1 : 0,
        transform: embedded || rightPanelOpen ? "translateX(0)" : "translateX(8px)",
        visibility: embedded || rightPanelOpen ? "visible" : "hidden",
        pointerEvents: embedded || rightPanelOpen ? "auto" : "none",
        transition: `width var(--transition-normal), min-width var(--transition-normal), max-width var(--transition-normal), flex-basis var(--transition-normal), margin var(--transition-normal), opacity var(--transition-fast), transform var(--duration-base) var(--easing-enter), border-color var(--transition-fast), box-shadow var(--transition-normal), visibility 0s linear ${embedded || rightPanelOpen ? "0ms" : "var(--duration-base)"}`,
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
            data-sidebar-tab-frame={t.id}
          >
            <button
              type="button"
              role="tab"
              id={`right-tab-${t.id}`}
              aria-selected={activeTab === t.id}
              tabIndex={activeTab === t.id ? 0 : -1}
              aria-controls={`right-panel-${t.id}`}
              data-sidebar-tab={t.id}
              onClick={() => activateTab(t.id)}
              onKeyDown={(event) => handlePrimaryTabKeyDown(event, index)}
              title={t.label}
              aria-label={`打开${t.label}`}
              className="mc-sidebar-right-tab"
            >
              <span className="mc-sidebar-tab-icon">{t.icon}</span>
              <span className="mc-sidebar-tab-label">{t.label}</span>
              {t.badge && <span className="mc-sidebar-tab-badge">{t.badge}</span>}
            </button>
            {(
              <button
                type="button"
                title={`关闭${t.label}标签页`}
                aria-label={`关闭${t.label}标签页`}
                onClick={(event) => {
                  event.stopPropagation();
                  closeOpenTab(t.id);
                }}
                className="mc-sidebar-right-tab-close mc-icon-button mc-icon-button-compact"
              >
                <X size={14} />
              </button>
            )}
          </div>
        ))}
        </div>
        <div className="mc-sidebar-right-actions" data-testid="right-sidebar-actions">
          <button
            ref={launcherButtonRef}
            type="button"
            onClick={() => {
              const rect = launcherButtonRef.current!.getBoundingClientRect();
              setLauncherPosition((position) => position ? null : { x: Math.max(8, rect.right - 260), y: rect.bottom + 6 });
            }}
            title="添加面板"
            aria-label="添加面板"
            aria-haspopup="menu"
            aria-expanded={launcherPosition !== null}
            className="mc-sidebar-right-action mc-sidebar-right-action-add mc-icon-button"
            data-active={launcherPosition ? "true" : "false"}
          >
            <Plus size={18} strokeWidth={1.8} />
          </button>
          {!embedded && (
            <button
            type="button"
            onClick={closeRightPanel}
            title="关闭面板"
            aria-label="关闭右侧栏"
            className="mc-sidebar-right-action mc-icon-button"
          >
              <PanelRightClose size={16} strokeWidth={1.8} />
            </button>
          )}
        </div>
      </div>
      {launcherPosition && <ContextMenu
        position={launcherPosition}
        onClose={closeLauncher}
        items={[
          { label: "终端", icon: <TerminalSquare {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.terminal), onClick: () => openBottomTab("terminal") },
          { label: "侧边聊天", icon: <MessageCirclePlus {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.sideChat), onClick: () => openRightTab("sidechat") },
          { label: "浏览器", icon: <Globe2 {...sidebarIconProps} />, onClick: () => openRightTab("browser") },
          { label: "文件", icon: <FolderOpen {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.globalSearch), onClick: toggleQuickOpen },
          { label: "", separator: true },
          ...tabs.filter((tab) => tab.id !== "sidechat" && tab.id !== "browser").map((tab) => ({
            label: tab.label, icon: tab.icon, onClick: () => openRightTab(tab.id),
          })),
        ]}
      />}
      {openedTabs.map(({ id: panelTab }) => <div
        key={panelTab}
        className="mc-sidebar-panel-view flex-1 overflow-hidden text-sm flex flex-col"
        id={`right-panel-${panelTab}`}
        role="tabpanel"
        aria-labelledby={`right-tab-${panelTab}`}
        hidden={activeTab !== panelTab}
        style={{ display: activeTab === panelTab ? "flex" : "none" }}
      >
        <div className="panel-content-wrapper" style={{ minHeight: 0, minWidth: 0, overflow: "hidden" }}>
          {panelTab === "sidechat" && sideChatOpen && <SideChatPanel active={visible && activeTab === "sidechat" && rightPanelOpen} />}
          {panelTab === "preview" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="preview" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="预览" />}>
                  <LazyPreviewPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "browser" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="preview" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="浏览器" />}>
                  <LazyBrowserPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "tasks" && <ScrollablePanel><ActivityTab /></ScrollablePanel>}
          {panelTab === "diff" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="diff" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="审阅" />}>
                  <LazyDiffPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "subagents" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="subagents" />}>
                <ScrollablePanel><LazySubagentsTab /></ScrollablePanel>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "artifacts" && <Suspense fallback={<PanelSkeleton kind="artifacts" />}><ScrollablePanel><LazyArtifactsTab /></ScrollablePanel></Suspense>}
          {panelTab === "inspector" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyInspectorTab /></ScrollablePanel></Suspense>}
          {panelTab === "diagnostics" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyDiagnosticsTab /></ScrollablePanel></Suspense>}
        </div>
      </div>)}
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
  zIndex: "var(--z-composer)",
  background: "transparent",
  transform: "translateX(-4px)",
  touchAction: "none",
};
