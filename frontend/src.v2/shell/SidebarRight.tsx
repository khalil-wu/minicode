import { useI18n } from "../i18n";
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
  Maximize2,
  Minimize2,
  MessageCircle,
  ChevronDown,
} from "../lib/icons";
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { RIGHT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { rightPanelDefaultWidth, rightPanelMaximumWidth, rightPanelWidthFromRatio } from "./right-panel-width";
import type { RightStackTab } from "../stores/types";
import { ContextMenu } from "../components/ContextMenu";
import { SideChatPanel } from "../panels/SideChatPanel";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { projectAgentViews } from "../lib/agent-view-model";
import { mcpServerNeedsAttention } from "../lib/mcp-status";
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

const sidebarIconProps = { size: 16, strokeWidth: 1.5 } as const;

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

export const SidebarRight = ({ embedded = false, visible = true, initialTab }: SidebarRightProps) => {
  const { t: tr } = useI18n();
  const mainConversationTitle = useAppStore((s) => s.conversations.find((item) => item.id === s.conversationId)?.title || tr("主聊天"));
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightPanelExpanded = useAppStore((s) => s.rightPanelExpanded);
  const setRightPanelExpanded = useAppStore((s) => s.setRightPanelExpanded);
  const rightSidebarWidth = useAppStore((s) => s.rightSidebarWidth);
  const rightSidebarWidthRatio = useAppStore((s) => s.rightSidebarWidthRatio);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const setRightSidebarWidth = useAppStore((s) => s.setRightSidebarWidth);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const closeSideChat = useAppStore((s) => s.closeSideChat);
  const openBottomTab = useAppStore((s) => s.openBottomTab);
  const toggleQuickOpen = useAppStore((s) => s.toggleQuickOpen);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [openTabIds, setOpenTabIds] = useState<StackTab[]>(() => initialTab !== undefined
    ? [normalizeInitialTab(initialTab)]
    : rightPanelOpen ? [normalizeInitialTab(rightStackTab)] : []);
  const [launcherPosition, setLauncherPosition] = useState<{ x: number; y: number } | null>(null);
  const [openedPanelPosition, setOpenedPanelPosition] = useState<{ x: number; y: number } | null>(null);
  const [tabsOverflow, setTabsOverflow] = useState(false);
  const [viewport, setViewport] = useState({ width: window.innerWidth, height: window.innerHeight });
  const [measuredMainContentWidth, setMeasuredMainContentWidth] = useState<number | null>(null);
  useEffect(() => {
    const resized = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener("resize", resized);
    return () => window.removeEventListener("resize", resized);
  }, []);
  const sidebarRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const stage = sidebarRef.current!.closest<HTMLElement>(".workbench-stage");
    if (!stage) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setMeasuredMainContentWidth(entry.contentRect.width);
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanupRef.current?.(), []);
  const tabListRef = useRef<HTMLDivElement | null>(null);
  const launcherButtonRef = useRef<HTMLButtonElement | null>(null);
  const requestedActiveTab = initialTab === undefined ? rightStackTab : localTab;
  const normalizedRequestedTab = requestedActiveTab === "plan" || requestedActiveTab === "terminal" ? "tasks" : requestedActiveTab;
  const activeTab = normalizedRequestedTab;
  const expanded = initialTab === undefined && rightPanelExpanded;
  const canExpand = initialTab === undefined && ["browser", "preview", "diff", "artifacts"].includes(activeTab);
  const setActiveTab = initialTab === undefined ? setRightStackTab : setLocalTab;
  const closeLauncher = useCallback(() => setLauncherPosition(null), []);
  useEffect(() => {
    if (!visible) { closeLauncher(); setOpenedPanelPosition(null); }
  }, [visible, closeLauncher]);

  const subagents = useAppStore((s) => s.subagents);
  const activePreviewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact);
  const previewSurfaceArtifact = useAppStore((s) => selectPreviewSurface(s).previewArtifact);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const runningSubagents = useMemo(() => projectAgentViews(subagents).filter((agent) => agent.status === "running").length, [subagents]);
  const mcpErrors = mcpServers.filter(mcpServerNeedsAttention).length;
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "tasks", label: tr("上下文"), icon: <PanelRightOpen {...sidebarIconProps} /> },
    { id: "diff", label: tr("变更"), badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff {...sidebarIconProps} /> },
    { id: "preview", label: tr("预览"), badge: previewSurfaceArtifact ? tr("开") : undefined, icon: <MonitorPlay {...sidebarIconProps} /> },
    { id: "browser", label: tr("浏览器"), icon: <Globe2 {...sidebarIconProps} /> },
    { id: "sidechat", label: tr("侧边聊天"), icon: <MessageCirclePlus {...sidebarIconProps} /> },
    { id: "artifacts", label: tr("产物"), badge: activePreviewArtifact ? "1" : undefined, icon: <Layers {...sidebarIconProps} /> },
    { id: "subagents", label: tr("子智能体"), badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Bot {...sidebarIconProps} /> },
    { id: "inspector", label: tr("运行详情"), icon: <FileSearch {...sidebarIconProps} /> },
    { id: "diagnostics", label: tr("运行状态"), badge: mcpErrors ? String(mcpErrors) : undefined, icon: <HeartPulse {...sidebarIconProps} /> },
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
    window.requestAnimationFrame(() => {
      tabListRef.current?.querySelector<HTMLButtonElement>(`[data-sidebar-tab="${fallbackTab}"]`)?.focus();
    });
  }, [activeTab, setActiveTab, openTabIds, closeSideChat, rightPanelOpen, toggleRightPanel]);
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

  useLayoutEffect(() => {
    const track = tabListRef.current!;
    const revealActive = () => {
      track.querySelector<HTMLElement>(`[data-sidebar-tab-frame="${activeTab}"]`)
        ?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
      setTabsOverflow(track.scrollWidth > track.clientWidth);
    };
    revealActive();
    const observer = new ResizeObserver(revealActive);
    observer.observe(track);
    return () => observer.disconnect();
  }, [activeTab, openTabIds]);

  useEffect(() => {
    if (diffReview || gitChangeCount > 0) addOpenTab("diff");
  }, [addOpenTab, diffReview, gitChangeCount]);

  useEffect(() => {
    if (previewSurfaceArtifact) addOpenTab("preview");
  }, [addOpenTab, previewSurfaceArtifact]);

  const mainContentWidth = measuredMainContentWidth ?? viewport.width - leftSidebarWidth;
  const minSidebarWidth = embedded ? 0 : 320;
  const effectiveResizeMin = minSidebarWidth;
  const effectiveResizeMax = rightPanelMaximumWidth(mainContentWidth);
  const defaultSidebarWidth = rightPanelDefaultWidth(mainContentWidth, viewport.height);
  const boundWidth = (width: number) => Math.min(effectiveResizeMax, Math.max(effectiveResizeMin, width));
  const sidebarWidth = boundWidth(rightSidebarWidthRatio === null
    ? rightSidebarWidth === RIGHT_SIDEBAR_DEFAULT_WIDTH ? defaultSidebarWidth : rightSidebarWidth
    : rightPanelWidthFromRatio(rightSidebarWidthRatio, mainContentWidth));
  const sidebarWidthStyle = expanded || embedded ? "100%" : rightPanelOpen ? `${sidebarWidth}px` : "0px";
  const sidebarMinWidth = expanded || embedded ? 0 : rightPanelOpen ? minSidebarWidth : 0;
  const sidebarMaxWidth = expanded || embedded ? "none" : rightPanelOpen ? `${effectiveResizeMax}px` : "0px";
  const activateTab = (tab: StackTab) => {
    addOpenTab(tab);
    setActiveTab(tab);
    closeLauncher();
    setOpenedPanelPosition(null);
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
    resizeCleanupRef.current?.();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    const previouslyDragging = document.body.classList.contains("layout-dragging");
    const startX = event.clientX;
    const startWidth = sidebarWidth;
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      setRightSidebarWidth(boundWidth(startWidth + startX - moveEvent.clientX), { mainContentWidth, persist: false });
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", finishResize);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      if (!previouslyDragging) document.body.classList.remove("layout-dragging");
      resizeCleanupRef.current = null;
    };
    const finishResize = () => {
      setRightSidebarWidth(useAppStore.getState().rightSidebarWidth, { mainContentWidth });
      cleanup();
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      finishResize();
    };
    resizeCleanupRef.current = cleanup;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("layout-dragging");
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", finishResize);
  };

  const resetSidebarWidth = () => {
    if (embedded) return;
    setRightSidebarWidth(defaultSidebarWidth, { mainContentWidth });
  };
  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = sidebarWidth + step;
    else if (event.key === "ArrowRight") nextWidth = sidebarWidth - step;
    else if (event.key === "Home") nextWidth = effectiveResizeMin;
    else if (event.key === "End") nextWidth = effectiveResizeMax;
    else if (event.key === "Enter") nextWidth = defaultSidebarWidth;
    if (nextWidth == null) return;
    event.preventDefault();
    setRightSidebarWidth(boundWidth(nextWidth), { mainContentWidth });
  };

  return (
    <aside
      ref={sidebarRef}
      id="workbench-right-sidebar"
      className="mc-sidebar-right relative flex flex-col overflow-hidden"
      data-embedded={embedded ? "true" : "false"}
      data-expanded={expanded ? "true" : "false"}
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
        flex: expanded || embedded ? "1 1 auto" : `0 0 ${rightPanelOpen ? sidebarWidth : 0}px`,
        background: "var(--surface-base)",
        border: 0,
        borderLeft: expanded || embedded || !rightPanelOpen ? 0 : "1px solid var(--border-subtle)",
        borderRadius: 0,
        boxShadow: "none",
        opacity: embedded || rightPanelOpen ? 1 : 0,
        transform: embedded || rightPanelOpen ? "none" : "translateX(8px)",
        visibility: embedded || rightPanelOpen ? "visible" : "hidden",
        pointerEvents: embedded || rightPanelOpen ? "auto" : "none",
        transition: `width var(--transition-normal), min-width var(--transition-normal), max-width var(--transition-normal), flex-basis var(--transition-normal), margin var(--transition-normal), opacity var(--transition-fast), transform var(--duration-base) var(--easing-enter), border-color var(--transition-fast), box-shadow var(--transition-normal), visibility 0s linear ${embedded || rightPanelOpen ? "0ms" : "var(--duration-base)"}`,
      } as React.CSSProperties}
    >
      {!embedded && !expanded && (
        <div
          className="mc-sidebar-right-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label={tr("调整右侧栏宽度")}
           aria-valuemin={effectiveResizeMin}
          aria-valuemax={effectiveResizeMax}
           aria-valuenow={Math.round(sidebarWidth)}
           aria-valuetext={tr("{count} 像素", { count: Math.round(sidebarWidth) })}
           tabIndex={0}
          title={tr("调整侧栏宽度")}
          onPointerDown={startResize}
           onDoubleClick={resetSidebarWidth}
           onKeyDown={handleResizeKeyDown}
          style={resizeHandleStyle}
        />
      )}
      <div className="mc-sidebar-right-header">
        {expanded && <button type="button" className="mc-sidebar-return-chat" aria-label={tr("返回主聊天")} title={tr("返回主聊天")}
          onClick={() => setRightPanelExpanded(false)}><MessageCircle size={15} /><span>{mainConversationTitle}</span></button>}
        <div ref={tabListRef} className="mc-sidebar-right-tabs" role="tablist" aria-label={tr("右侧栏面板")}>
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
              aria-label={tr("打开{label}", { label: t.label })}
              className="mc-sidebar-right-tab"
            >
              <span className="mc-sidebar-tab-icon">{t.icon}</span>
              <span className="mc-sidebar-tab-label">{t.label}</span>
              {t.badge && <span className="mc-sidebar-tab-badge">{t.badge}</span>}
            </button>
            {(
              <button
                type="button"
                title={tr("关闭{label}标签页", { label: t.label })}
                aria-label={tr("关闭{label}标签页", { label: t.label })}
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
          {tabsOverflow && <button
            type="button"
            className="mc-sidebar-right-action mc-icon-button"
            aria-label={tr("已打开的面板")}
            title={tr("已打开的面板")}
            aria-haspopup="menu"
            aria-expanded={openedPanelPosition !== null}
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              closeLauncher();
              setOpenedPanelPosition((position) => position ? null : { x: Math.max(8, rect.right - 240), y: rect.bottom + 6 });
            }}
          ><ChevronDown size={15} /></button>}
          {canExpand && <button
            type="button"
            className="mc-sidebar-right-action mc-icon-button"
            aria-label={expanded ? tr("还原到侧栏") : tr("扩大到主工作区")}
            title={expanded ? tr("还原到侧栏") : tr("扩大到主工作区")}
            aria-pressed={expanded}
            onClick={() => setRightPanelExpanded(!expanded)}
          >{expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>}
          <button
            ref={launcherButtonRef}
            type="button"
            onClick={() => {
              setOpenedPanelPosition(null);
              const rect = launcherButtonRef.current!.getBoundingClientRect();
              setLauncherPosition((position) => position ? null : { x: Math.max(8, rect.right - 260), y: rect.bottom + 6 });
            }}
            title={tr("添加面板")}
            aria-label={tr("添加面板")}
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
            title={tr("关闭面板")}
            aria-label={tr("关闭右侧栏")}
            className="mc-sidebar-right-action mc-icon-button"
          >
              <PanelRightClose size={16} strokeWidth={1.8} />
            </button>
          )}
        </div>
      </div>
      {openedPanelPosition && <ContextMenu
        position={openedPanelPosition}
        onClose={() => setOpenedPanelPosition(null)}
        items={openedTabs.map((tab) => ({
          label: `${tab.label}${activeTab === tab.id ? tr(" · 当前") : ""}`,
          icon: tab.icon,
          onClick: () => openRightTab(tab.id),
        }))}
      />}
      {launcherPosition && <ContextMenu
        position={launcherPosition}
        onClose={closeLauncher}
        items={[
          { label: tr("终端"), icon: <TerminalSquare {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.terminal), onClick: () => openBottomTab("terminal") },
          { label: tr("侧边聊天"), icon: <MessageCirclePlus {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.sideChat), onClick: () => openRightTab("sidechat") },
          { label: tr("浏览器"), icon: <Globe2 {...sidebarIconProps} />, onClick: () => openRightTab("browser") },
          { label: tr("文件"), icon: <FolderOpen {...sidebarIconProps} />, shortcut: formatShortcut(shortcutBindings.globalSearch), onClick: toggleQuickOpen },
          { label: "", separator: true },
          ...tabs.filter((tab) => !["sidechat", "browser", "inspector", "diagnostics"].includes(tab.id)).map((tab) => ({
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
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("预览")} />}>
                  <LazyPreviewPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "browser" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="browser" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("浏览器")} />}>
                  <LazyBrowserPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "tasks" && <ScrollablePanel><ActivityTab /></ScrollablePanel>}
          {panelTab === "diff" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="diff" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("变更")} />}>
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
          {panelTab === "diagnostics" && <Suspense fallback={<PanelSkeleton kind="diagnostics" />}><ScrollablePanel><LazyDiagnosticsTab /></ScrollablePanel></Suspense>}
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
