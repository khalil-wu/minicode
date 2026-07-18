import {
  Activity,
  FileDiff,
  FileSearch,
  FolderOpen,
  Globe,
  HeartPulse,
  Layers,
  MessageCircle,
  MonitorPlay,
  Plus,
  PanelRightClose,
  TerminalSquare,
  Users,
  X,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { BrowserPanel } from "../panels/BrowserPanel";
import { PreviewPanel } from "../panels/PreviewPanel";
import { DiffPanel } from "../panels/DiffPanel";
import { requestNewTerminalSession } from "../panels/terminalRequests";
import { ScrollablePanel } from "./SidebarShared";
import { ActivityTab } from "./tabs/ActivityTab";

type StackTab = "preview" | "browser" | "terminal" | "tasks" | "diff" | "plan" | "subagents" | "artifacts" | "inspector" | "diagnostics";
type LauncherItem =
  | { type: "separator"; key: string; label: string }
  | { type: "item"; key: string; label: string; badge?: string; icon: React.ReactNode; shortcut?: string; onSelect: () => void };

interface SidebarRightProps {
  embedded?: boolean;
  initialTab?: StackTab | "details" | "context";
}

const normalizeInitialTab = (tab: SidebarRightProps["initialTab"]): StackTab => {
  if (tab === "details" || tab === "context") return "inspector";
  if (tab === "plan") return "tasks";
  return tab ?? "tasks";
};

const defaultOpenTabs: StackTab[] = ["tasks"];

const shouldAllowAutomaticTabSwitch = (activeTab: StackTab, rightPanelOpen: boolean): boolean => {
  if (!rightPanelOpen) return true;
  return activeTab === "preview" || activeTab === "tasks";
};

const LazyTerminalPanel = lazy(() =>
  import("../panels/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
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
    case "terminal":
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
      return 360;
  }
};

let terminalPreloadPromise: Promise<unknown> | null = null;

const preloadTerminal = () => {
  terminalPreloadPromise ??= Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
    import("../panels/TerminalPanel"),
  ]).catch((error) => {
    terminalPreloadPromise = null;
    throw error;
  });
  return terminalPreloadPromise;
};

const requestTerminalPreload = () => {
  void preloadTerminal().catch(() => {});
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
  const quickOpenVisible = useAppStore((s) => s.quickOpenVisible);
  const toggleQuickOpen = useAppStore((s) => s.toggleQuickOpen);
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const toggleSideChat = useAppStore((s) => s.toggleSideChat);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [openTabIds, setOpenTabIds] = useState<StackTab[]>(() => Array.from(new Set([...defaultOpenTabs, normalizeInitialTab(initialTab)])));
  const [launcherMenuOpen, setLauncherMenuOpen] = useState(false);
  const launcherMenuRef = useRef<HTMLDivElement | null>(null);
  const requestedActiveTab = embedded ? localTab : rightStackTab;
  const normalizedRequestedTab = requestedActiveTab === "plan" ? "tasks" : requestedActiveTab;
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
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const [userTabLocked, setUserTabLocked] = useState(false);
  const allowAutoSwitch = !embedded && !rightStackTabLocked && !userTabLocked && shouldAllowAutomaticTabSwitch(activeTab, rightPanelOpen);

  // 切换会话时释放tab锁
  useEffect(() => {
    setUserTabLocked(false);
    setOpenTabIds(Array.from(new Set([...defaultOpenTabs, activeTab])));
  }, [conversationId]);

  useEffect(() => {
    if (!allowAutoSwitch) return;
    if (livePreviewUrl) {
      setRightStackTab("preview", { automatic: true });
    }
  }, [allowAutoSwitch, livePreviewUrl, setRightStackTab]);

  const runningSubagents = subagents.filter((subagent) => subagent.status === "running").length;
  const runningTerminals = terminalSessions.filter((t) => t.status !== "exited").length;
  const mcpErrors = mcpServers.filter((s) => s.status === "error").length;
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;

  useEffect(() => {
    if (activeTab === "terminal" || runningTerminals > 0) requestTerminalPreload();
  }, [activeTab, runningTerminals]);

  const hasHealthIssues = mcpErrors > 0;
  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "tasks", label: "Activity", icon: <Activity size={15} /> },
    { id: "subagents", label: "Agents", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Users size={15} /> },
    { id: "artifacts", label: "Artifacts", badge: previewArtifact ? "1" : undefined, icon: <Layers size={15} /> },
    { id: "inspector", label: "Inspector", icon: <FileSearch size={15} /> },
    { id: "diff", label: "Review", badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff size={15} /> },
    { id: "preview", label: "Preview", badge: livePreviewUrl || previewArtifact ? "on" : undefined, icon: <MonitorPlay size={15} /> },
    { id: "terminal", label: "Terminal", badge: runningTerminals ? String(runningTerminals) : undefined, icon: <TerminalSquare size={15} /> },
    { id: "browser", label: "Browser Control", icon: <Globe size={15} /> },
    { id: "diagnostics", label: "Health", badge: mcpErrors ? String(mcpErrors) : undefined, icon: <HeartPulse size={15} /> },
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
  const launcherItems: LauncherItem[] = [
    {
      type: "item",
      key: "terminal",
      label: "New Terminal",
      badge: runningTerminals ? String(runningTerminals) : undefined,
      icon: <TerminalSquare size={15} />,
      onSelect: () => {
        openRightTab("terminal");
        requestTerminalPreload();
        requestNewTerminalSession();
      },
    },
    {
      type: "item",
      key: "preview",
      label: "Open Preview",
      badge: livePreviewUrl || previewArtifact ? "on" : undefined,
      icon: <MonitorPlay size={15} />,
      shortcut: "Ctrl+Shift+B",
      onSelect: () => openRightTab("preview"),
    },
    {
      type: "item",
      key: "files",
      label: "Open File",
      icon: <FolderOpen size={15} />,
      shortcut: "Ctrl+P",
      onSelect: () => {
        setLauncherMenuOpen(false);
        if (!quickOpenVisible) toggleQuickOpen();
      },
    },
    {
      type: "item",
      key: "side-chat",
      label: "Side Chat",
      icon: <MessageCircle size={15} />,
      shortcut: "Ctrl+;",
      onSelect: () => {
        setLauncherMenuOpen(false);
        if (!sideChatOpen) toggleSideChat();
      },
    },
    { type: "item", key: "review", label: "Show Review", badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff size={15} />, onSelect: () => openRightTab("diff") },
    { type: "item", key: "activity", label: "Show Activity", icon: <Activity size={15} />, onSelect: () => openRightTab("tasks") },
    { type: "item", key: "artifacts", label: "Show Artifacts", badge: previewArtifact ? "1" : undefined, icon: <Layers size={15} />, onSelect: () => openRightTab("artifacts") },
    { type: "separator", key: "advanced", label: "Advanced" },
    { type: "item", key: "browser", label: "Browser Control", icon: <Globe size={15} />, onSelect: () => openRightTab("browser") },
    { type: "item", key: "agents", label: "Show Agents", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Users size={15} />, onSelect: () => openRightTab("subagents") },
    ...(hasHealthIssues
      ? [{ type: "item" as const, key: "health", label: "Show Health", badge: String(mcpErrors), icon: <HeartPulse size={15} />, onSelect: () => openRightTab("diagnostics") }]
      : []),
  ];
  useEffect(() => {
    addOpenTab(activeTab);
  }, [activeTab, addOpenTab]);

  useEffect(() => {
    if (diffReview || gitChangeCount > 0) addOpenTab("diff");
  }, [addOpenTab, diffReview, gitChangeCount]);

  useEffect(() => {
    if (livePreviewUrl || previewArtifact) addOpenTab("preview");
  }, [addOpenTab, livePreviewUrl, previewArtifact]);

  const activePrimaryTab = openedTabs.some((tab) => tab.id === activeTab);
  const compactPanel = ["tasks", "inspector", "subagents", "artifacts", "diagnostics"].includes(activeTab);
  const minSidebarWidth = embedded ? 0 : compactPanel ? 292 : 332;
  const effectiveResizeMin = embedded ? 0 : Math.max(320, minSidebarWidth);
  const sidebarWidth = Math.max(minSidebarWidth, rightSidebarWidth);
  const sidebarWidthStyle = embedded ? "100%" : `${sidebarWidth}px`;
  const sidebarMaxWidth = embedded ? "none" : "min(1040px, calc(100vw - 360px))";
  const activateTab = (tab: StackTab) => {
    addOpenTab(tab);
    lockAndSetTab(tab);
    setLauncherMenuOpen(false);
  };

  useEffect(() => {
    if (!launcherMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!launcherMenuRef.current?.contains(target)) {
        setLauncherMenuOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setLauncherMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [launcherMenuOpen]);

  const moveTabFocus = (index: number) => {
    window.setTimeout(() => {
      const id = openedTabs[index]?.id;
      if (!id) return;
      document.querySelector<HTMLButtonElement>(`[data-sidebar-tab="${id}"]`)?.focus();
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
    const startWidth = rightSidebarWidth;
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
    if (event.key === "ArrowLeft") nextWidth = rightSidebarWidth + step;
    else if (event.key === "ArrowRight") nextWidth = rightSidebarWidth - step;
    else if (event.key === "Home") nextWidth = effectiveResizeMin;
    else if (event.key === "End") nextWidth = 1040;
    else if (event.key === "Enter") nextWidth = preferredManualSidebarWidth(activeTab);
    if (nextWidth == null) return;
    event.preventDefault();
    setRightSidebarWidth(nextWidth);
  };

  return (
    <aside
      className={`mc-sidebar-right relative flex flex-col overflow-hidden ${!embedded ? "anim-slide-right" : ""}`}
      data-embedded={embedded ? "true" : "false"}
      style={{
        "--right-sidebar-width": `${sidebarWidth}px`,
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        width: sidebarWidthStyle,
        minWidth: minSidebarWidth,
        maxWidth: sidebarMaxWidth,
        flex: embedded ? "1 1 auto" : `0 0 ${sidebarWidthStyle}`,
        background: "var(--surface-base)",
        borderLeft: embedded ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
      } as React.CSSProperties}
    >
      {!embedded && (
        <div
          className="mc-sidebar-right-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize right sidebar"
           aria-valuemin={effectiveResizeMin}
          aria-valuemax={1040}
           aria-valuenow={Math.round(rightSidebarWidth)}
           aria-valuetext={`${Math.round(rightSidebarWidth)} pixels`}
           tabIndex={0}
          title="Resize side panel"
          onPointerDown={startResize}
           onDoubleClick={resetSidebarWidth}
           onKeyDown={handleResizeKeyDown}
          style={resizeHandleStyle}
        />
      )}
      <div className="mc-sidebar-right-header flex items-center gap-1.5 px-2.5 py-2 min-h-[42px]" style={{ background: "var(--surface-sidebar)", borderBottom: "1px solid var(--border-subtle)" }}>
        <div className="flex-1 min-w-0 overflow-x-auto overflow-y-hidden flex items-center gap-1" role="tablist" aria-label="Right sidebar panels" style={{ scrollbarWidth: "none" }}>
        {openedTabs.map((t, index) => (
          <div
            key={t.id}
            className="inline-flex items-center border relative"
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
              onFocus={t.id === "terminal" ? requestTerminalPreload : undefined}
              onMouseEnter={t.id === "terminal" ? requestTerminalPreload : undefined}
              onKeyDown={(event) => handlePrimaryTabKeyDown(event, index)}
              title={t.label}
              aria-label={`Open ${t.label}`}
              className="min-w-0 inline-flex items-center gap-1.5 border-0 cursor-pointer"
              style={sidebarTabButtonStyle(activeTab === t.id)}
            >
              <span className="mc-sidebar-tab-icon" style={{ color: activeTab === t.id ? "var(--text-secondary)" : "var(--text-muted)" }}>{t.icon}</span>
              <span className="truncate">{t.label}</span>
              {t.badge && <span className="text-[10px] min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full" style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)", border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)" }}>{t.badge}</span>}
            </button>
            {openedTabs.length > 1 && (
              <button
                type="button"
                title={`Close ${t.label} tab`}
                aria-label={`Close ${t.label} tab`}
                onClick={(event) => {
                  event.stopPropagation();
                  closeOpenTab(t.id);
                }}
                className="mc-icon-button mc-icon-button-compact"
                style={sidebarTabCloseStyle(activeTab === t.id)}
              >
                <X size={12} />
              </button>
            )}
          </div>
        ))}
        </div>
        <div ref={launcherMenuRef} className="relative">
          <button
            type="button"
            onClick={() => {
              setLauncherMenuOpen((open) => !open);
            }}
            title="Add panel"
            aria-label="Add panel"
            aria-haspopup="menu"
            aria-expanded={launcherMenuOpen}
            className="mc-icon-button"
            data-active={launcherMenuOpen ? "true" : "false"}
          >
            <Plus size={17} />
          </button>
          {launcherMenuOpen && (
            <div role="menu" className="absolute top-[calc(100%+6px)] right-0 z-20 w-[214px] p-[6px] rounded-[var(--radius-md,8px)]" style={{ background: "var(--surface-raised)", border: "1px solid var(--border-subtle)", boxShadow: "var(--shadow-strong, var(--shadow-md))" }}>
              {launcherItems.map((tab) => (
                tab.type === "separator" ? (
                  <div key={tab.key} role="separator" className="px-2 pb-1 pt-2 text-[10px] uppercase font-bold tracking-wide" style={{ color: "var(--text-muted)", borderTop: "1px solid var(--border-subtle)", marginTop: 4 }}>
                    {tab.label}
                  </div>
                ) : (
                  <button
                    key={tab.key}
                    type="button"
                    role="menuitem"
                    onClick={tab.onSelect}
                    className="w-full flex items-center gap-2 min-h-[34px] px-2 border-0 rounded-[var(--radius-sm,5px)] cursor-pointer text-sm text-left"
                    style={{
                      background: "transparent",
                      color: "var(--text-primary)",
                    }}
                  >
                    <span className="inline-flex" style={{ color: "var(--text-muted)" }}>{tab.icon}</span>
                    <span className="flex-1 min-w-0 truncate">{tab.label}</span>
                    {tab.badge && <span className="text-[10px] min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full" style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)", border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)" }}>{tab.badge}</span>}
                    {tab.shortcut && <span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-ui)" }}>{tab.shortcut}</span>}
                  </button>
                )
              ))}
            </div>
          )}
        </div>
        {!embedded && (
          <button
            type="button"
            onClick={toggleRightPanel}
            title="Close panel"
            aria-label="Close right panel"
            className="mc-icon-button"
          >
            <PanelRightClose size={15} />
          </button>
        )}
      </div>
      <div
        key={activeTab}
        className="anim-fade-in flex-1 overflow-hidden text-sm flex flex-col"
        id={`right-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={activePrimaryTab ? `right-tab-${activeTab}` : undefined}
        aria-label={activePrimaryTab ? undefined : `${activeItem.label} panel`}
        style={{ background: "var(--surface-base)", minHeight: 0, minWidth: 0 }}
      >
        <div className="panel-content-wrapper" style={{ minHeight: 0, minWidth: 0, overflow: "hidden" }}>
          {activeTab === "preview" && (
            <SafeBoundary fallback={<PanelErrorFallback panelName="Preview" />}>
              <PreviewPanel />
            </SafeBoundary>
          )}
          {activeTab === "browser" && (
            <SafeBoundary fallback={<PanelErrorFallback panelName="Browser Control" />}>
              <BrowserPanel />
            </SafeBoundary>
          )}
          {activeTab === "terminal" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="terminal" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="Terminal" />}>
                  <LazyTerminalPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {activeTab === "tasks" && <ScrollablePanel><ActivityTab /></ScrollablePanel>}
          {activeTab === "diff" && (
            <SafeBoundary fallback={<PanelErrorFallback panelName="Diff" />}>
              <DiffPanel />
            </SafeBoundary>
          )}
          {activeTab === "subagents" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="subagents" />}>
                <ScrollablePanel><LazySubagentsTab /></ScrollablePanel>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {activeTab === "artifacts" && <Suspense fallback={<PanelSkeleton kind="artifacts" />}><ScrollablePanel><LazyArtifactsTab /></ScrollablePanel></Suspense>}
          {activeTab === "inspector" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyInspectorTab /></ScrollablePanel></Suspense>}
          {activeTab === "diagnostics" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyDiagnosticsTab /></ScrollablePanel></Suspense>}
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
  maxWidth: 168,
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
  padding: "0 4px 0 10px",
  background: "transparent",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  fontWeight: active ? 700 : 600,
  whiteSpace: "nowrap",
});

const sidebarTabCloseStyle = (active: boolean): React.CSSProperties => ({
  marginRight: 4,
  color: active ? "var(--text-muted)" : "color-mix(in oklch, var(--text-muted) 72%, transparent)",
});
