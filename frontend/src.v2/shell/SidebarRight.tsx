import {
  Activity,
  Box,
  CheckCircle,
  Copy,
  ExternalLink,
  FileText,
  FolderOpen,
  GitBranch,
  Globe,
  Image,
  Link,
  MonitorPlay,
  MoreHorizontal,
  PanelRightClose,
  RefreshCw,
  Search,
  TerminalSquare,
  Users,
  Wrench,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiBase, authHeaders } from "../protocol/api";
import { isDesktop, revealPath } from "../desktop/runtime";
import { fetchWorkspaceGitStatus, type WorkspaceGitStatusResponse } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { BrowserPanel } from "../panels/BrowserPanel";
import { PreviewPanel } from "../panels/PreviewPanel";
import { DiffPanel } from "../panels/DiffPanel";
import { AgentProgressTrace } from "../panels/AgentProgressTrace";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { getWebSocket } from "../hooks/useWebSocket";
import { PanelSkeleton } from "./PanelSkeleton";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import {
  buildActivitySidebarState,
  type ActivityBrowserItem,
  type ActivityOutputItem,
  type ActivityProgressItem,
  type ActivitySourceItem,
} from "./activitySidebarState";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import {
  capabilityFlagLabel,
  capabilityHasDetails,
  capabilityHasInventory,
  capabilityItemNames,
  capabilityToolNames,
  formatAgentToolCounts,
  formatCapabilityPreview,
  formatCapabilitySource,
  formatDeferredCapability,
  formatExposureBreakdown,
  formatInventoryCount,
  formatMcpProxyCount,
  formatSkillCapability,
  mergeCapabilities,
  summarizeToolViews,
  withDerivedCapabilitySummary,
  type AgentCapabilityToolView,
  type CapabilitySource,
  type DoctorPayload,
} from "../protocol/capabilities";

type StackTab = "preview" | "browser" | "terminal" | "tasks" | "diff" | "plan" | "subagents" | "inspector" | "diagnostics";

interface SidebarRightProps {
  embedded?: boolean;
  initialTab?: StackTab | "details" | "context";
}

type InfoTone = "default" | "muted" | "accent" | "warning";

const normalizeInitialTab = (tab: SidebarRightProps["initialTab"]): StackTab => {
  if (tab === "details" || tab === "context") return "inspector";
  return tab ?? "preview";
};

const shouldAllowAutomaticTabSwitch = (activeTab: StackTab, rightPanelOpen: boolean): boolean => {
  if (!rightPanelOpen) return true;
  return activeTab === "preview" || activeTab === "tasks" || activeTab === "plan";
};

const LazyTerminalPanel = lazy(() =>
  import("../panels/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

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

export const SidebarRight = ({ embedded = false, initialTab = "preview" }: SidebarRightProps) => {
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
  const [tabMenuOpen, setTabMenuOpen] = useState(false);
  const tabMenuRef = useRef<HTMLDivElement | null>(null);
  const activeTab = embedded ? localTab : rightStackTab;
  const setActiveTab = embedded ? setLocalTab : setRightStackTab;

  // 用户手动切换tab时锁定自动切换
  const lockAndSetTab = useCallback((tab: StackTab) => {
    setUserTabLocked(true);
    setActiveTab(tab);
  }, [setActiveTab]);
  const plan = useAppStore((s) => s.plan);
  const todos = useAppStore((s) => s.todos);
  const subagents = useAppStore((s) => s.subagents);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const previewArtifact = useAppStore((s) => s.previewArtifact);
  const diffReview = useAppStore((s) => s.diffReview);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const runningTasks = todos.filter((t) => t.status === "in_progress").length;

  const runningProgress = useAppStore((s) => {
    const key = s.conversationId || "__active__";
    return s.agentProgress.filter((entry) =>
      entry.status === "running" && (entry.conversationId === key || entry.conversationId === "__active__")
    ).length;
  });

  const [userTabLocked, setUserTabLocked] = useState(false);
  const allowAutoSwitch = !embedded && !rightStackTabLocked && !userTabLocked && shouldAllowAutomaticTabSwitch(activeTab, rightPanelOpen);
  const runningToolCount = runningTasks + runningProgress;

  // 切换会话时释放tab锁
  useEffect(() => {
    setUserTabLocked(false);
  }, [conversationId]);

  useEffect(() => {
    if (!allowAutoSwitch) return;
    if (plan && plan.status === "executing") {
      setRightStackTab("plan");
    } else if (livePreviewUrl) {
      setRightStackTab("preview");
    } else if (runningToolCount > 0) {
      setRightStackTab("tasks");
    }
  }, [allowAutoSwitch, plan?.status, livePreviewUrl, runningToolCount]);

  const lastToolCalls = useMemo(() => messages.flatMap((m) => getToolCallsFromMessage(m)).slice(-5), [messages]);
  const runningSubagents = subagents.filter((subagent) => subagent.status === "running").length;
  const runningTerminals = terminalSessions.filter((t) => t.status !== "exited").length;
  const mcpErrors = mcpServers.filter((s) => s.status === "error").length;

  useEffect(() => {
    if (activeTab === "terminal" || runningTerminals > 0) requestTerminalPreload();
  }, [activeTab, runningTerminals]);

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "preview", label: "Preview", badge: livePreviewUrl || previewArtifact ? "on" : undefined, icon: <MonitorPlay size={15} /> },
    ...(isDesktop() ? [{ id: "browser" as const, label: "Browser", icon: <Globe size={15} /> }] : []),
    { id: "terminal", label: "Terminal", badge: runningTerminals ? String(runningTerminals) : undefined, icon: <TerminalSquare size={15} /> },
    { id: "tasks", label: "Activity", badge: runningTasks || runningProgress ? String(runningTasks + runningProgress) : undefined, icon: <CheckCircle size={15} /> },
    { id: "diff", label: "Review", badge: diffReview ? "1" : undefined, icon: <GitBranch size={15} /> },
    { id: "inspector", label: "Context", icon: <Search size={15} /> },
    { id: "plan", label: "Plan", badge: plan?.status === "executing" ? "run" : undefined, icon: <Activity size={15} /> },
    { id: "subagents", label: "Agents", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Users size={15} /> },
    { id: "diagnostics", label: "Health", badge: mcpErrors ? String(mcpErrors) : undefined, icon: <Wrench size={15} /> },
  ];
  const activeItem = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const primaryTabs = tabs.filter((tab) => ["preview", "terminal", "tasks", "diff", "inspector"].includes(tab.id));
  const overflowTabs = tabs.filter((tab) => !primaryTabs.some((primary) => primary.id === tab.id));
  const activePrimaryTab = primaryTabs.some((tab) => tab.id === activeTab);
  const activateTab = (tab: StackTab) => {
    lockAndSetTab(tab);
    setTabMenuOpen(false);
  };

  useEffect(() => {
    if (!tabMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!tabMenuRef.current?.contains(event.target as Node)) {
        setTabMenuOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setTabMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [tabMenuOpen]);

  const moveTabFocus = (index: number) => {
    window.setTimeout(() => {
      const id = primaryTabs[index]?.id;
      if (!id) return;
      document.querySelector<HTMLButtonElement>(`[data-sidebar-tab="${id}"]`)?.focus();
    }, 0);
  };
  const handlePrimaryTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % primaryTabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + primaryTabs.length) % primaryTabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = primaryTabs.length - 1;
    else return;
    event.preventDefault();
    activateTab(primaryTabs[nextIndex].id);
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
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("lostpointercapture", cleanup);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    handle.setPointerCapture(pointerId);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("lostpointercapture", cleanup);
  };

  return (
    <aside
      className={`relative flex flex-col overflow-hidden ${!embedded ? "anim-slide-right" : ""}`}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        width: embedded ? "100%" : rightSidebarWidth,
        minWidth: embedded ? 0 : 320,
        maxWidth: embedded ? "none" : 1040,
        flex: embedded ? "1 1 auto" : `0 0 ${rightSidebarWidth}px`,
        background: "var(--surface-base)",
        borderLeft: embedded ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
      }}
    >
      {!embedded && (
        <div
          role="separator"
          aria-orientation="vertical"
          title="Resize side panel"
          onPointerDown={startResize}
          style={resizeHandleStyle}
        />
      )}
      <div className="flex items-center gap-2 px-2.5 py-2 min-h-[42px]" style={{ background: "var(--surface-sidebar)", borderBottom: "1px solid var(--border-subtle)" }}>
        <div className="flex-1 min-w-0 inline-flex items-center gap-[5px] text-sm font-semibold" style={{ color: "var(--text-primary)", fontWeight: 650 }}>
          <span className="inline-flex" style={{ color: "var(--text-muted)" }}>{activeItem.icon}</span>
          <span>{activeItem.label}</span>
          {activeItem.badge && <span className="text-[10px] min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full" style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)", border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)" }}>{activeItem.badge}</span>}
        </div>
        <div className="inline-flex items-center gap-1" role="tablist" aria-label="Right sidebar panels">
        {primaryTabs.map((t, index) => (
          <button
            key={t.id}
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
            className="w-8 h-8 inline-flex items-center justify-center border border-transparent rounded-[var(--radius-sm,6px)] cursor-pointer text-xs p-0 relative"
            style={{
              background: activeTab === t.id ? "var(--surface-page)" : "transparent",
              color: activeTab === t.id ? "var(--text-primary)" : "var(--text-muted)",
              borderColor: activeTab === t.id ? "var(--border-subtle)" : "transparent",
            }}
          >
            {t.icon}
            {t.badge && <span className="text-[10px] min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full" style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)", border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)" }}>{t.badge}</span>}
          </button>
        ))}
        <div ref={tabMenuRef} className="relative">
          <button
            type="button"
            onClick={() => setTabMenuOpen((open) => !open)}
            title="More panels"
            aria-label="More panels"
            aria-haspopup="menu"
            aria-expanded={tabMenuOpen}
            className="w-8 h-8 inline-flex items-center justify-center border border-transparent rounded-[var(--radius-sm,6px)] cursor-pointer text-xs p-0 relative"
            style={{
              background: tabMenuOpen ? "var(--surface-page)" : "transparent",
              color: tabMenuOpen ? "var(--text-primary)" : "var(--text-muted)",
              borderColor: tabMenuOpen ? "var(--border-subtle)" : "transparent",
            }}
          >
            <MoreHorizontal size={15} />
          </button>
          {tabMenuOpen && (
            <div role="menu" className="absolute top-[calc(100%+6px)] right-0 z-20 w-[180px] p-[5px] rounded-[var(--radius-md,8px)]" style={{ background: "var(--surface-raised)", border: "1px solid var(--border-subtle)", boxShadow: "var(--shadow-strong, var(--shadow-md))" }}>
              {overflowTabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  role="menuitem"
                  onClick={() => activateTab(tab.id)}
                  className="w-full flex items-center gap-2 min-h-[30px] px-2 border-0 rounded-[var(--radius-sm,5px)] cursor-pointer text-sm text-left"
                  style={{
                    background: activeTab === tab.id ? "var(--surface-soft)" : "transparent",
                    color: "var(--text-primary)",
                  }}
                >
                  <span className="inline-flex" style={{ color: "var(--text-muted)" }}>{tab.icon}</span>
                  <span className="flex-1">{tab.label}</span>
                  {tab.badge && <span className="text-[10px] min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full" style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)", border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)" }}>{tab.badge}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
        {!embedded && (
          <button
            type="button"
            onClick={toggleRightPanel}
            title="Close panel (Ctrl+Shift+B)"
            aria-label="Close right panel"
            className="w-8 h-8 inline-flex items-center justify-center border border-transparent rounded-[var(--radius-sm,6px)] cursor-pointer text-xs p-0 relative"
            style={{
              background: "transparent",
              color: "var(--text-muted)",
              borderColor: "transparent",
            }}
          >
            <PanelRightClose size={15} />
          </button>
        )}
        </div>
      </div>
      <div
        key={activeTab}
        className="anim-fade-in flex-1 overflow-hidden text-sm flex flex-col"
        id={`right-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={activePrimaryTab ? `right-tab-${activeTab}` : undefined}
        aria-label={activePrimaryTab ? undefined : `${activeItem.label} panel`}
        style={{ background: "var(--surface-base)" }}
      >
        <div className="panel-content-wrapper">
          {activeTab === "preview" && (
            <SafeBoundary fallback={<PanelErrorFallback panelName="Preview" />}>
              <PreviewPanel />
            </SafeBoundary>
          )}
          {activeTab === "browser" && (
            <SafeBoundary fallback={<PanelErrorFallback panelName="Browser" />}>
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
          {activeTab === "plan" && <ScrollablePanel><PlanTab /></ScrollablePanel>}
          {activeTab === "subagents" && <ScrollablePanel><SubagentsTab /></ScrollablePanel>}
          {activeTab === "inspector" && <ScrollablePanel><InspectorTab toolCalls={lastToolCalls} /></ScrollablePanel>}
          {activeTab === "diagnostics" && <ScrollablePanel><DiagnosticsTab /></ScrollablePanel>}
        </div>
      </div>
    </aside>
  );
};

const PlanTab = () => {
  const plan = useAppStore((s) => s.plan);
  if (!plan) {
    return (
      <div className="grid gap-2.5">
        <PanelHeader title="Plan" />
        <AgentProgressTrace mode="compact" />
        <EmptyLine>No proposed plan in this session.</EmptyLine>
      </div>
    );
  }
  const doneCount = plan.steps.filter((s) => s.status === "done").length;

  return (
    <div className="grid gap-2.5">
      <PanelHeader title="Plan" meta={`${doneCount}/${plan.steps.length} ${plan.status}`} />
      <AgentProgressTrace mode="compact" />
      <div className="grid gap-0.5">
        {plan.steps.map((s, i) => (
          <div key={s.id} style={rowCardStyle(i === plan.currentStep && s.status === "running")}>
            <StatusMark status={s.status} />
            <div className="flex-1 min-w-0">
              <div style={{ color: s.status === "done" ? "var(--text-muted)" : "var(--text-primary)", textDecoration: s.status === "done" ? "line-through" : "none", fontSize: "var(--text-xs)", lineHeight: 1.45, fontWeight: i === plan.currentStep ? 600 : 400 }}>
                {s.title}
              </div>
              {s.detail && <div className="text-xs mt-0.5" style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{s.detail}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const ActivityTab = () => {
  const conversationId = useAppStore((s) => s.conversationId);
  const messages = useAppStore((s) => s.messages);
  const todos = useAppStore((s) => s.todos);
  const plan = useAppStore((s) => s.plan);
  const agentProgress = useAppStore((s) => s.agentProgress);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const previewArtifact = useAppStore((s) => s.previewArtifact);
  const previewVerification = useAppStore((s) => s.previewVerification);
  const previewServers = useAppStore((s) => s.previewServers);
  const previewLaunchProcesses = useAppStore((s) => s.previewLaunchProcesses);

  const state = useMemo(() => buildActivitySidebarState({
    conversationId,
    messages,
    todos,
    plan,
    agentProgress,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
  }), [
    conversationId,
    messages,
    todos,
    plan,
    agentProgress,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
  ]);

  if (!state.hasConversation) {
    return (
      <div style={activityPanelStyle}>
        <PanelHeader title="Activity" />
        <EmptyLine>No active conversation.</EmptyLine>
      </div>
    );
  }

  return (
    <div style={activityPanelStyle}>
      <ActivityProgressSection items={state.progress} />
      <ActivityOutputSection items={state.output} />
      <ActivityBrowserSection items={state.browser} />
      <ActivitySourcesSection items={state.sources} />
      {state.isEmpty && <EmptyLine>No activity for this conversation.</EmptyLine>}
    </div>
  );
};

const ActivityProgressSection = ({ items }: { items: ActivityProgressItem[] }) => (
  <ActivitySection title="Progress" empty="No progress yet.">
    {items.map((item) => (
      <div key={item.id} style={activityRowStyle(item.status === "running")}>
        <StatusMark status={item.status} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div title={item.label} style={activityTitleStyle(item.status === "completed")}>
            {item.label}
          </div>
          {item.detail && <div title={item.detail} style={activityMetaTextStyle}>{item.detail}</div>}
        </div>
      </div>
    ))}
  </ActivitySection>
);

const ActivityOutputSection = ({ items }: { items: ActivityOutputItem[] }) => {
  const openOutput = (item: ActivityOutputItem) => {
    const store = useAppStore.getState();
    if (item.artifactId) {
      store.setPreviewArtifact(null);
      store.addPanel({ id: `artifact-${item.artifactId}`, kind: "preview", label: item.label.slice(0, 24) || "Artifact" });
      store.setRightStackTab("preview");
      getWebSocket()?.send({ type: "read_artifact", artifact_id: item.artifactId });
      return;
    }
    if (item.path) {
      store.openEditorFile(item.path, item.label);
      return;
    }
    if (item.url) {
      store.openLivePreview(item.url);
    }
  };

  return (
    <ActivitySection title="Output" empty="No outputs yet.">
      {items.map((item) => {
        const Icon = outputIcon(item);
        return (
          <button key={item.id} type="button" onClick={() => openOutput(item)} style={activityButtonRowStyle} title={item.detail || item.label}>
            <span style={activityIconStyle}><Icon size={13} /></span>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              <span style={activityMetaTextStyle}>{item.kind}{item.detail ? ` - ${item.detail}` : ""}</span>
            </span>
            <ExternalLink size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          </button>
        );
      })}
    </ActivitySection>
  );
};

const ActivityBrowserSection = ({ items }: { items: ActivityBrowserItem[] }) => {
  const openBrowser = (item: ActivityBrowserItem) => {
    const store = useAppStore.getState();
    store.openLivePreview(item.url);
    store.setRightStackTab("preview");
  };

  return (
    <ActivitySection title="Browser" empty="No browser target.">
      {items.map((item) => (
        <button key={item.id} type="button" onClick={() => openBrowser(item)} style={activityButtonRowStyle} title={item.url}>
          <span style={activityIconStyle}><MonitorPlay size={13} /></span>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={activityButtonLabelStyle}>{item.label}</span>
            <span style={activityMetaTextStyle}>{item.host}{item.detail ? ` - ${item.detail}` : ""}</span>
          </span>
          <span style={activityStatusPillStyle(item.status)}>{browserStatusLabel(item.status)}</span>
        </button>
      ))}
    </ActivitySection>
  );
};

const ActivitySourcesSection = ({ items }: { items: ActivitySourceItem[] }) => (
  <ActivitySection title="Sources" empty="No cited sources.">
    {items.map((item) => (
      <a key={item.id} href={item.url} target="_blank" rel="noreferrer" style={activityButtonRowStyle} title={item.title || item.url}>
        <span style={activityIconStyle}><Link size={13} /></span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={activityButtonLabelStyle}>{item.label}</span>
          <span style={activityMetaTextStyle}>{item.title || item.host}</span>
        </span>
        <ExternalLink size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
      </a>
    ))}
  </ActivitySection>
);

const ActivitySection = ({ title, empty, children }: { title: string; empty: string; children: React.ReactNode }) => {
  const childCount = Array.isArray(children) ? children.length : children ? 1 : 0;
  return (
    <section style={activitySectionStyle} aria-label={title}>
      <SectionLabel label={title} />
      {childCount > 0 ? <div style={activitySectionBodyStyle}>{children}</div> : <EmptyLine>{empty}</EmptyLine>}
    </section>
  );
};

function outputIcon(item: ActivityOutputItem) {
  if (item.kind === "image" || item.mediaType?.startsWith("image/")) return Image;
  if (item.kind === "file" || item.kind === "text" || item.kind === "code" || item.kind === "pdf") return FileText;
  return Box;
}

function browserStatusLabel(status: ActivityBrowserItem["status"]): string {
  if (status === "verified") return "ok";
  if (status === "failed") return "fail";
  if (status === "running") return "run";
  return "idle";
}

const SubagentsTab = () => {
  const subagents = useAppStore((s) => s.subagents);
  if (subagents.length === 0) return <EmptyLine>No delegated subagents are running.</EmptyLine>;
  const running = subagents.filter((subagent) => subagent.status === "running").length;
  return (
    <div>
      <PanelHeader title="Subagents" meta={running ? `${running}/${subagents.length} running` : `${subagents.length} recent`} />
      <div style={{ display: "grid", gap: 4 }}>
        {subagents.map((subagent) => (
          <div key={subagent.id} style={rowCardStyle(subagent.status === "running")}>
            <StatusMark status={subagent.status} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text-primary)", fontSize: "var(--text-xs)", lineHeight: 1.45 }}>
                {subagent.role}
              </div>
              <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
                {subagent.id}
              </div>
              {subagent.summary && (
                <div style={{ color: "var(--text-secondary)", fontSize: "var(--text-xs)", marginTop: 4 }}>
                  {subagent.summary}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const InspectorTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => (
  <div style={{ display: "grid", gap: 12 }}>
    <ContextTab />
    <DetailsTab toolCalls={toolCalls} />
  </div>
);

const ContextTab = () => {
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const contextUsage = useAppStore((s) => s.contextUsage);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const [gitStatus, setGitStatus] = useState<WorkspaceGitStatusResponse | null>(null);
  const [gitLoading, setGitLoading] = useState(false);
  const [gitError, setGitError] = useState<string | null>(null);
  const conversation = conversations.find((item) => item.id === conversationId);
  const workspacePath = conversation?.worktreePath || conversation?.workspaceRoot || workingDirectory || workspaceGit?.currentPath || "";
  const branch = conversation?.gitBranch || workspaceGit?.branch || "No branch";
  const hasWorkspacePath = Boolean(workspacePath.trim());
  const displayWorkspace = workspaceDisplayName(workspacePath, "Computer");
  const displayBranch = branchDisplayName(branch) || "No branch";
  const contextPercent = contextUsage && contextUsage.limit > 0 ? Math.round((contextUsage.used / contextUsage.limit) * 100) : null;
  const changedCount = gitStatus ? gitStatus.modified.length + gitStatus.staged.length + gitStatus.untracked.length : null;

  const refreshGitStatus = () => {
    setGitLoading(true);
    setGitError(null);
    fetchWorkspaceGitStatus()
      .then((result) => setGitStatus(result))
      .catch((error) => {
        setGitStatus(null);
        setGitError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setGitLoading(false));
  };

  useEffect(() => {
    refreshGitStatus();
  }, [workspacePath]);

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <SectionLabel label="Session" />
      <InfoCard>
        <InfoRow label="Conversation" value={conversation?.title || conversationId || "No active conversation"} />
        <InfoRow label="Isolation" value={conversation?.gitIsolated || workspaceGit?.isWorktree ? "Protected workspace" : hasWorkspacePath ? "Shared workspace" : "Computer"} tone={conversation?.gitIsolated || workspaceGit?.isWorktree ? "accent" : "muted"} />
        <InfoRow label="Branch" value={displayBranch} mono />
      </InfoCard>
      <SectionLabel label="Workspace" />
      <InfoCard>
        <InfoRow label="Path" value={displayWorkspace} mono />
        <InfoRow
          label="Changes"
          value={gitLoading ? "Checking..." : gitError ? "Unavailable" : changedCount == null ? "Unknown" : changedCount === 0 ? "Clean" : `${changedCount} changed`}
          tone={gitError || (changedCount && changedCount > 0) ? "warning" : "muted"}
        />
        {gitError && <div style={inlineWarningStyle}>Git status failed: {gitError}</div>}
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          <SmallButton icon={<FolderOpen size={12} />} label="Reveal" disabled={!workspacePath || !isDesktop()} onClick={() => void revealPath(workspacePath)} />
          <SmallButton icon={<Copy size={12} />} label="Copy" disabled={!workspacePath} onClick={() => void navigator.clipboard?.writeText(workspacePath)} />
          <SmallButton icon={<GitBranch size={12} />} label="Refresh" onClick={refreshGitStatus} />
        </div>
      </InfoCard>
      <SectionLabel label="Runtime" />
      <InfoCard>
        <InfoRow label="Context" value={contextPercent == null ? "Not reported" : `${contextPercent}% (${contextUsage?.used}/${contextUsage?.limit})`} tone={contextPercent != null && contextPercent >= 85 ? "warning" : "muted"} />
        <InfoRow
          label="Compact"
          value={contextUsage?.compactedAt ? `${new Date(contextUsage.compactedAt).toLocaleTimeString()}: ${contextUsage.compactSummary || "Done"}` : "Not compacted"}
          tone={contextUsage?.compactedAt ? "accent" : "muted"}
        />
        <InfoRow label="Terminals" value={String(terminalSessions.length)} />
        <InfoRow label="Editor" value={activeEditorPath || "No active file"} mono />
        <SmallButton icon={<TerminalSquare size={12} />} label="Open Terminal" onClick={() => setRightStackTab("terminal")} />
      </InfoCard>
    </div>
  );
};

const DetailsTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => {
  const inspectorEntries = useAppStore((s) => s.inspectorEntries);
  const inspectorFocus = useAppStore((s) => s.inspectorFocus);
  const focusedEntry = inspectorFocus ? inspectorEntries.find((e) => e.targetId === inspectorFocus.id) : null;
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <SectionLabel label="Inspector" />
      {focusedEntry && (
        <pre style={jsonBlockStyle}>
          {JSON.stringify(focusedEntry.payload, null, 2)}
        </pre>
      )}
      {inspectorEntries.length > 0 && inspectorEntries.slice(-8).reverse().map((entry, i) => (
        <button key={`${entry.targetId}-${i}`} onClick={() => useAppStore.getState().setInspectorFocus({ kind: entry.targetKind, id: entry.targetId })} style={eventButtonStyle(entry.targetId === inspectorFocus?.id)}>
          <span style={{ color: "var(--text-muted)" }}>{entry.targetKind}</span>
          <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{entry.targetId.slice(0, 12)}</span>
        </button>
      ))}
      {toolCalls.length > 0 && (
        <>
          <SectionLabel label="Recent Tools" />
          {toolCalls.map((tc) => (
            <div key={tc.id} style={toolCallRowStyle}>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{tc.name}</span>
              <span style={{ marginLeft: 8, color: statusColor(tc.status) }}>{tc.status}</span>
            </div>
          ))}
        </>
      )}
      {inspectorEntries.length === 0 && toolCalls.length === 0 && <EmptyLine>No recent activity to inspect.</EmptyLine>}
    </div>
  );
};

const DiagnosticsTab = () => {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const local = useLocalDiagnostics();
  const mcpSnapshot = useAppStore((s) => mcpDiagnosticsSnapshot(s.mcpServers));
  const lastMcpSnapshot = useRef<string | null>(null);
  const capabilities = doctor?.capabilities?.summary;

  const refresh = useCallback(() => {
    setLoading(true);
    fetch(`${apiBase()}/api/doctor`, { cache: "no-store", headers: authHeaders() })
      .then((res) => res.ok ? res.json() : Promise.reject(new Error(res.statusText)))
      .then((payload) => withCapabilityFallback(payload as DoctorPayload))
      .then((payload) => setDoctor(payload))
      .catch((error) => setDoctor({ error: String(error) }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (lastMcpSnapshot.current === null) {
      lastMcpSnapshot.current = mcpSnapshot;
      return;
    }
    if (lastMcpSnapshot.current === mcpSnapshot) return;
    lastMcpSnapshot.current = mcpSnapshot;
    refresh();
  }, [mcpSnapshot, refresh]);

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <PanelHeader title="Diagnostics" meta={loading ? "checking" : "ready"} action={<SmallButton icon={<RefreshCw size={12} />} label="Refresh" onClick={refresh} />} />
      {doctor?.error && <div style={errorStyle}>{doctor.error}</div>}
      <InfoCard>
        <InfoRow label="Backend" value={String(doctor?.backend?.status ?? "unknown")} tone={doctor?.backend?.status === "ok" ? "accent" : "warning"} />
        <InfoRow label="Sessions" value={String(doctor?.backend?.active_sessions ?? local.activeSessions)} />
        <InfoRow label="Provider" value={String(doctor?.llm?.provider ?? "unknown")} />
        <InfoRow label="Model" value={String(doctor?.llm?.active_model ?? doctor?.llm?.current_model ?? local.model)} mono />
      </InfoCard>
      <InfoCard>
        <InfoRow label="Workspace" value={workspaceDisplayName(String((doctor?.workspace?.root ?? local.workspace) || ""), "Computer")} mono />
        <InfoRow label="Branch" value={branchDisplayName(String(doctor?.git?.branch ?? local.branch ?? "")) || "--"} />
        <InfoRow label="Preview Pane" value={String(doctor?.preview?.url ?? local.preview ?? "--")} mono />
        <InfoRow label="Terminal" value={`${local.terminals} sessions`} />
      </InfoCard>
      <InfoCard>
        <InfoRow label="MCP" value={`${Array.isArray(doctor?.mcp) ? doctor?.mcp.length : local.mcpServers} servers`} tone={local.mcpErrors ? "warning" : "muted"} />
        <InfoRow label="MCP errors" value={String(local.mcpErrors)} tone={local.mcpErrors ? "warning" : "muted"} />
        <InfoRow label="Runtime" value={isDesktop() ? "Electron desktop" : "Web fallback"} />
      </InfoCard>
      <SectionLabel label="Agent" />
      <InfoCard>
        <InfoRow label="Tools" value={formatAgentToolCounts(capabilities)} tone={capabilities ? "accent" : "muted"} />
        <InfoRow label="MCP resources" value={capabilityFlagLabel(capabilities?.mcp_resource_bridge)} tone={capabilityFlagTone(capabilities?.mcp_resource_bridge)} />
        <InfoRow label="Deferred" value={formatDeferredCapability(capabilities)} tone={capabilityFlagTone(capabilities?.deferred_bridge)} />
        <InfoRow label="Skills" value={formatSkillCapability(capabilities)} tone={capabilityFlagTone(capabilities?.skill_bridge)} />
        <InfoRow label="MCP proxies" value={formatMcpProxyCount(capabilities)} tone={capabilities ? "muted" : "warning"} />
      </InfoCard>
      <SectionLabel label="Inventory" />
      <InfoCard>
        <InfoRow label="Source" value={formatCapabilitySource(doctor?.capabilitySource)} tone={capabilitySourceTone(doctor?.capabilitySource)} />
        <InfoRow label="Exposure" value={formatExposureBreakdown(capabilities)} tone={capabilities ? "muted" : "warning"} />
        <InfoRow label="Commands" value={formatInventoryCount(doctor?.capabilities?.commands, capabilities?.commands, "command", "commands")} />
        <InfoRow label="Tool sample" value={formatCapabilityPreview(capabilityToolNames(doctor?.capabilities?.tools))} mono />
        <InfoRow label="Command" value={formatCapabilityPreview(capabilityItemNames(doctor?.capabilities?.commands))} mono />
        <InfoRow label="Skill sample" value={formatCapabilityPreview(capabilityItemNames(doctor?.capabilities?.skills))} mono />
      </InfoCard>
      <ToolExposureCard toolViews={doctor?.capabilities?.tool_views} />
    </div>
  );
};

const withCapabilityFallback = async (payload: DoctorPayload): Promise<DoctorPayload> => {
  const fallbackSource = capabilityHasDetails(payload.capabilities) ? "doctor" : "unknown";
  if (capabilityHasInventory(payload.capabilities)) {
    return { ...payload, capabilities: withDerivedCapabilitySummary(payload.capabilities), capabilitySource: "doctor" };
  }
  try {
    const res = await fetch(`${apiBase()}/api/status`, { cache: "no-store", headers: authHeaders() });
    if (!res.ok) return { ...payload, capabilitySource: fallbackSource };
    const statusPayload = await res.json() as DoctorPayload;
    const statusHasDetails = capabilityHasDetails(statusPayload.capabilities);
    return {
      ...payload,
      capabilities: mergeCapabilities(payload.capabilities, statusPayload.capabilities),
      capabilitySource: statusHasDetails ? "status" : fallbackSource,
    };
  } catch {
    return { ...payload, capabilitySource: fallbackSource };
  }
};

const mcpDiagnosticsSnapshot = (servers: { name: string; status: string; phase?: string; tools?: number; lastError?: string }[]): string => (
  servers
    .map((server) => [
      server.name,
      server.status,
      server.phase ?? "",
      server.tools ?? "",
      server.lastError ?? "",
    ].join(":"))
    .sort()
    .join("|")
);

const capabilityFlagTone = (ready: boolean | undefined): InfoTone => {
  if (ready === true) return "accent";
  if (ready === false) return "warning";
  return "muted";
};

const capabilitySourceTone = (source: CapabilitySource | undefined): InfoTone => (
  source === "doctor" ? "muted" : "warning"
);

const ToolExposureCard = ({ toolViews }: { toolViews: AgentCapabilityToolView[] | undefined }) => {
  const exposure = summarizeToolViews(toolViews);
  if (exposure.total == null) return null;
  return (
    <>
      <SectionLabel label="Tool Exposure" />
      <InfoCard>
        <InfoRow label="Direct tools" value={formatCapabilityPreview(exposure.direct)} tone={exposure.direct.length ? "accent" : "muted"} mono />
        <InfoRow label="Deferred tools" value={formatCapabilityPreview(exposure.deferred)} tone={exposure.deferred.length ? "muted" : "default"} mono />
        <InfoRow label="Hidden tools" value={formatCapabilityPreview(exposure.hidden)} tone={exposure.hidden.length ? "warning" : "muted"} mono />
      </InfoCard>
    </>
  );
};

const useLocalDiagnostics = () => {
  const conversations = useAppStore((s) => s.conversations);
  const currentModel = useAppStore((s) => s.currentModel);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  return useMemo(() => ({
    activeSessions: conversations.length,
    model: currentModel || "Select model",
    workspace: workingDirectory,
    branch: workspaceGit?.branch,
    preview: livePreviewUrl,
    terminals: terminalSessions.length,
    mcpServers: mcpServers.length,
    mcpErrors: mcpServers.filter((s) => s.status === "error").length,
  }), [conversations.length, currentModel, workingDirectory, workspaceGit?.branch, livePreviewUrl, terminalSessions.length, mcpServers]);
};

function statusColor(status: string): string {
  if (status === "running" || status === "in_progress") return "var(--state-info)";
  if (status === "done" || status === "success" || status === "completed") return "var(--state-success)";
  if (status === "error" || status === "failed") return "var(--state-danger)";
  if (status === "blocked") return "var(--state-warning)";
  return "var(--text-muted)";
}

const StatusMark = ({ status }: { status: string }) => {
  const size = 14;
  const color = statusColor(status);
  if (status === "completed" || status === "done") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" fill={color} opacity={0.15} />
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
        <path d="M5 8.2 7 10.2 11 6" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      </svg>
    );
  }
  if (status === "running" || status === "in_progress") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" strokeDasharray="11 33" strokeLinecap="round">
          <animateTransform attributeName="transform" type="rotate" from="0 8 8" to="360 8 8" dur="0.8s" repeatCount="indefinite" />
        </circle>
        <circle cx="8" cy="8" r="3" fill={color} opacity={0.5} />
      </svg>
    );
  }
  if (status === "failed" || status === "error" || status === "blocked") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
        <path d="M6 6l4 4M10 6l-4 4" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
      <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
    </svg>
  );
};

const ScrollablePanel = ({ children }: { children: React.ReactNode }) => (
  <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px 14px", background: "var(--surface-base)" }}>
    {children}
  </div>
);

const EmptyLine = ({ children }: { children: React.ReactNode }) => (
  <span style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", lineHeight: 1.5 }}>{children}</span>
);

const PanelHeader = ({ title, meta, action }: { title: string; meta?: string; action?: React.ReactNode }) => (
  <div style={panelHeaderStyle}>
    <span style={{ fontWeight: 700, color: "var(--text-primary)", flex: 1 }}>{title}</span>
    {meta && <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{meta}</span>}
    {action}
  </div>
);

const SectionLabel = ({ label }: { label: string }) => (
  <div style={sectionLabelStyle}>
    {label}
  </div>
);

const InfoCard = ({ children }: { children: React.ReactNode }) => (
  <div style={infoCardStyle}>
    {children}
  </div>
);

const InfoRow = ({ label, value, mono, tone = "default" }: { label: string; value: string; mono?: boolean; tone?: InfoTone }) => {
  const color = tone === "accent" ? "var(--accent-primary)" : tone === "warning" ? "var(--state-warning)" : tone === "muted" ? "var(--text-muted)" : "var(--text-secondary)";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "88px minmax(0, 1fr)", gap: 8, fontSize: "var(--text-xs)" }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span title={value} style={{ color, fontFamily: mono ? "var(--font-mono)" : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</span>
    </div>
  );
};

const SmallButton = ({ icon, label, onClick, disabled }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) => (
  <button type="button" disabled={disabled} onClick={onClick} style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 7px", background: "var(--surface-page)", color: disabled ? "var(--text-muted)" : "var(--text-secondary)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", cursor: disabled ? "not-allowed" : "pointer", fontSize: "var(--text-xs)", whiteSpace: "nowrap" }}>
    {icon}
    {label}
  </button>
);

const panelTopBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 10px",
  minHeight: 42,
  background: "var(--surface-sidebar)",
  borderBottom: "1px solid var(--border-subtle)",
};

const resizeHandleStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  bottom: 0,
  left: -3,
  width: 7,
  cursor: "col-resize",
  zIndex: 30,
  background: "transparent",
};

const activePanelTitleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  color: "var(--text-primary)",
  fontSize: "14px",
  fontWeight: 650,
};

const panelIconGroupStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
};

const panelIconButtonStyle: React.CSSProperties = {
  width: 32,
  height: 32,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  padding: 0,
  position: "relative",
};

const panelMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: "calc(100% + 6px)",
  right: 0,
  zIndex: 20,
  width: 180,
  padding: 5,
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-strong, var(--shadow-md))",
};

const panelMenuItemStyle: React.CSSProperties = {
  width: "100%",
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 30,
  padding: "0 8px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: "transparent",
  color: "var(--text-primary)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  textAlign: "left",
};

const badgeStyle: React.CSSProperties = {
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  color: "var(--accent-primary)",
  minWidth: 16,
  height: 16,
  padding: "0 4px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: 999,
  background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)",
};

const rowCardStyle = (active: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "flex-start",
  gap: 8,
  padding: "8px 0",
  borderRadius: 0,
  borderTop: "1px solid var(--border-subtle)",
  background: active ? "color-mix(in oklch, var(--state-info) 6%, transparent)" : "transparent",
  boxShadow: active ? "inset 2px 0 0 var(--state-info)" : "none",
});

const activityPanelStyle: React.CSSProperties = {
  display: "grid",
  gap: 13,
};

const activitySectionStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
};

const activitySectionBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 2,
};

const activityRowStyle = (active: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "flex-start",
  gap: 8,
  minWidth: 0,
  minHeight: 28,
  padding: "6px 0",
  borderTop: "1px solid var(--border-subtle)",
  background: active ? "color-mix(in oklch, var(--state-info) 5%, transparent)" : "transparent",
});

const activityButtonRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
  minHeight: 34,
  width: "100%",
  padding: "6px 0",
  border: 0,
  borderTop: "1px solid var(--border-subtle)",
  borderRadius: 0,
  background: "transparent",
  color: "var(--text-primary)",
  cursor: "pointer",
  textAlign: "left",
  textDecoration: "none",
};

const activityIconStyle: React.CSSProperties = {
  width: 22,
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  color: "var(--text-muted)",
  background: "var(--surface-page)",
};

const activityTitleStyle = (done: boolean): React.CSSProperties => ({
  color: done ? "var(--text-muted)" : "var(--text-primary)",
  textDecoration: done ? "line-through" : "none",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
});

const activityButtonLabelStyle: React.CSSProperties = {
  display: "block",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.35,
  fontWeight: 650,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const activityMetaTextStyle: React.CSSProperties = {
  display: "block",
  marginTop: 2,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.25,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const activityStatusPillStyle = (status: ActivityBrowserItem["status"]): React.CSSProperties => ({
  flexShrink: 0,
  minWidth: 28,
  height: 18,
  padding: "0 6px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  color: status === "verified"
    ? "var(--state-success)"
    : status === "failed"
      ? "var(--state-danger)"
      : status === "running"
        ? "var(--state-info)"
        : "var(--text-muted)",
  background: "var(--surface-page)",
});

const eventButtonStyle = (active: boolean): React.CSSProperties => ({
  display: "flex",
  gap: 8,
  padding: "6px 8px",
  background: active ? "var(--surface-soft)" : "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  fontSize: "var(--text-xs)",
  cursor: "pointer",
  textAlign: "left",
});

const toolCallRowStyle: React.CSSProperties = {
  padding: "6px 8px",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  fontSize: "var(--text-xs)",
};

const jsonBlockStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  color: "var(--text-secondary)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  maxHeight: "min(48vh, 420px)",
  overflow: "auto",
  margin: 0,
  padding: 8,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
};

const sidebarContentStyle: React.CSSProperties = {
  flex: 1,
  overflow: "hidden",
  fontSize: "var(--text-sm)",
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-base)",
};

const panelHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  marginBottom: 8,
  minHeight: 28,
  paddingBottom: 8,
  borderBottom: "1px solid var(--border-subtle)",
};

const sectionLabelStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
  textTransform: "uppercase",
  fontWeight: 700,
  paddingTop: 2,
  letterSpacing: 0,
};

const infoCardStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  padding: 14,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
};

const errorStyle: React.CSSProperties = {
  color: "var(--state-danger)",
  background: "var(--state-danger-soft)",
  border: "1px solid var(--state-danger)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: 8,
  fontSize: "var(--text-xs)",
};

const inlineWarningStyle: React.CSSProperties = {
  color: "var(--state-warning)",
  background: "color-mix(in oklch, var(--state-warning) 9%, transparent)",
  border: "1px solid color-mix(in oklch, var(--state-warning) 32%, transparent)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 8px",
  fontSize: "var(--text-xs)",
  lineHeight: 1.4,
};
