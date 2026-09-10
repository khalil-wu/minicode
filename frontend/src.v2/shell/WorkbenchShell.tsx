import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { HeaderBar } from "./HeaderBar";
import { SidebarLeft } from "./SidebarLeft";
import { SidebarRight } from "./SidebarRight";
import { BottomDock } from "./BottomDock";
import { MainSlots } from "./MainSlots";
import { ChatPane } from "../chat/ChatPane";
import { useAppStore } from "../stores";
import { selectPreviewSurface } from "../lib/preview-projection";
import { isCompactWorkbenchViewport, LEFT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { SafeBoundary } from "./ChunkErrorBoundary";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { isDesktop, runtime } from "../desktop/runtime";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";
import { getConnectionPresentation } from "./connectionPresentation";

const SkillsMarketplace = lazy(() => import("../overlays/SkillsMarketplace").then((m) => ({ default: m.SkillsMarketplace })));

const useCompactWorkbench = () => {
  const [compact, setCompact] = useState(isCompactWorkbenchViewport);
  useEffect(() => {
    const onResize = () => setCompact(isCompactWorkbenchViewport());
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return compact;
};

const currentConnectionPresentation = (state: {
  isConnected: boolean;
  connectionPhase: Parameters<typeof getConnectionPresentation>[0]["connectionPhase"];
  reconnectAttempt: number;
  reconnectMaxAttempts: number | null;
  connectionError: string | null;
}) => getConnectionPresentation({
  isConnected: state.isConnected,
  isDesktop: isDesktop(),
  hasRuntimeToken: Boolean(runtime()?.runtimeToken?.trim()),
  connectionPhase: state.connectionPhase,
  reconnectAttempt: state.reconnectAttempt,
  reconnectMaxAttempts: state.reconnectMaxAttempts,
  connectionError: state.connectionError,
});

const ConnectionBanner = () => {
  const isConnected = useAppStore((s) => s.isConnected);
  const connectionPhase = useAppStore((s) => s.connectionPhase);
  const reconnectAttempt = useAppStore((s) => s.reconnectAttempt);
  const reconnectMaxAttempts = useAppStore((s) => s.reconnectMaxAttempts);
  const connectionError = useAppStore((s) => s.connectionError);
  const connectionState = {
    isConnected,
    connectionPhase,
    reconnectAttempt,
    reconnectMaxAttempts,
    connectionError,
  };
  const presentation = currentConnectionPresentation(connectionState);
  const previousAnnouncementRef = useRef(presentation.accessibleLabel);
  const [announcement, setAnnouncement] = useState(
    presentation.accessibleLabel,
  );
  useEffect(() => {
    if (previousAnnouncementRef.current === presentation.accessibleLabel) return;
    previousAnnouncementRef.current = presentation.accessibleLabel;
    setAnnouncement(presentation.accessibleLabel);
  }, [presentation.accessibleLabel]);
  return (
    <>
      <span role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </span>
      {!connectionState.isConnected && (
        <div
          aria-hidden="true"
          className="mc-connection-banner"
          data-kind={presentation.kind}
        >
          <span className="mc-connection-banner-dot" />
          <span>{presentation.bannerMessage}</span>
        </div>
      )}
    </>
  );
};

const ChatModeShell = () => (
  <div
    className="mc-main-surface flex-1 min-h-0 flex flex-col overflow-hidden w-full"
    style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", width: "100%" }}
  >
    <SafeBoundary fallback={<ChatErrorFallback />}>
      <ChatPane />
    </SafeBoundary>
  </div>
);

const WorkbenchModeShell = ({ mode }: { mode: "cowork" | "code" }) => {
  const ensureCodeLayout = useAppStore((s) => s.ensureCodeLayout);
  useEffect(() => {
    if (mode === "code") ensureCodeLayout();
  }, [ensureCodeLayout, mode]);
  return (
    <>
      <div
        className="flex-1 flex overflow-hidden min-h-0"
        style={{ flex: 1, display: "flex", overflow: "hidden", minWidth: 0, minHeight: 0 }}
      >
        <MainSlots mode="tabs" forceChat={mode === "cowork"} />
      </div>
    </>
  );
};

const NarrowSidebarDrawer = ({
  children,
  id,
  label,
  onClose,
  side,
  modal = true,
  open = true,
}: {
  children: ReactNode;
  id: string;
  label: string;
  onClose: () => void;
  side: "left" | "right";
  modal?: boolean;
  open?: boolean;
}) => {
  const dialogRef = useFocusTrap(modal && open);
  useEscapeKey(onClose, modal && open, dialogRef);
  return (
    <div
      className={modal && open ? "mc-narrow-drawer-backdrop" : undefined}
      data-side={side}
      onMouseDown={modal ? onClose : undefined}
      style={modal ? {
        position: "fixed",
        inset: 0,
        zIndex: "var(--z-drawer)",
        display: open ? "flex" : "none",
        justifyContent: side === "left" ? "flex-start" : "flex-end",
        background: "var(--backdrop-subtle)",
        padding: 8,
      } : { display: open ? "contents" : "none" }}
    >
      <div
        className={modal && open ? "mc-narrow-drawer-surface" : undefined}
        data-side={side}
        ref={dialogRef}
        id={id}
        role={modal ? "dialog" : undefined}
        aria-modal={modal ? true : undefined}
        aria-label={modal ? label : undefined}
        tabIndex={modal ? -1 : undefined}
        onMouseDown={(event) => event.stopPropagation()}
        style={modal ? {
          width: side === "right" ? "min(480px, calc(100vw - 16px))" : "min(380px, calc(100vw - 16px))",
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          background: "var(--surface-base)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 8px)",
          boxShadow: "var(--shadow-strong, var(--shadow-medium))",
        } : { display: "contents" }}
      >
        {modal && <div
          style={{
            minHeight: 40,
            display: "flex",
            alignItems: "center",
            padding: "4px 8px 4px 12px",
            borderBottom: "1px solid var(--border-subtle)",
          }}
        >
          <strong style={{ flex: 1, fontSize: "var(--text-sm)" }}>{label}</strong>
          <button
            type="button"
            className="btn-ghost mc-icon-button"
            aria-label={`关闭${label}`}
            title={`关闭${label}`}
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </div>}
        <div style={modal ? { flex: 1, minHeight: 0, display: "flex", overflow: "hidden" } : { display: "contents" }}>{children}</div>
      </div>
    </div>
  );
};

export const WorkbenchShell = () => {
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const [marketplaceVisited, setMarketplaceVisited] = useState(skillsMarketplaceOpen);
  useEffect(() => {
    if (skillsMarketplaceOpen) setMarketplaceVisited(true);
  }, [skillsMarketplaceOpen]);
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const appMode = useAppStore((s) => s.appMode);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const previewRequestAt = useAppStore((s) => selectPreviewSurface(s).previewArtifact?.loadedAt ?? 0);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const compact = useCompactWorkbench();
  const [compactPanel, setCompactPanel] = useState<"left" | "right" | null>(null);
  const previousRightPanelOpenRef = useRef(rightPanelOpen);
  const previousRightStackTabRef = useRef(rightStackTab);
  const activeCodeSlot =
    panelSlots.find((slot) => slot.focused) ??
    panelSlots.find((slot) => slot.kind !== "chat") ??
    panelSlots.find((slot) => slot.kind === "chat");
  const codePanelMaximized = Boolean(
    appMode === "code" && activeCodeSlot?.maximized && activeCodeSlot.kind !== "chat",
  );
  const leftPanelAvailable = skillsMarketplaceOpen || appMode === "cowork" || (appMode === "code" && !codePanelMaximized);
  const rightPanelAvailable = !skillsMarketplaceOpen && (sideChatOpen || appMode === "cowork" || (appMode === "code" && !codePanelMaximized));

  useEffect(() => {
    if (!compact) setCompactPanel(null);
  }, [compact]);

  useEffect(() => {
    if (compactPanel === "left" && !leftPanelAvailable) setCompactPanel(null);
    if (compactPanel === "right" && !rightPanelAvailable) setCompactPanel(null);
  }, [compactPanel, leftPanelAvailable, rightPanelAvailable]);

  useEffect(() => {
    const panelWasOpened = !previousRightPanelOpenRef.current && rightPanelOpen;
    const panelWasClosed = previousRightPanelOpenRef.current && !rightPanelOpen;
    const requestedTabChanged = previousRightStackTabRef.current !== rightStackTab;
    previousRightPanelOpenRef.current = rightPanelOpen;
    previousRightStackTabRef.current = rightStackTab;
    if (compact && rightPanelAvailable && (panelWasOpened || requestedTabChanged)) setCompactPanel("right");
    if (compact && panelWasClosed) setCompactPanel(null);
  }, [compact, rightPanelAvailable, rightPanelOpen, rightStackTab]);

  useEffect(() => {
    if (compact && rightPanelAvailable && previewRequestAt > 0) setCompactPanel("right");
  }, [compact, previewRequestAt, rightPanelAvailable]);

  const toggleLeftPanel = () => {
    if (compact) {
      setCompactPanel((current) => current === "left" ? null : "left");
      return;
    }
    setLeftSidebarWidth(leftSidebarWidth > 0 ? 0 : LEFT_SIDEBAR_DEFAULT_WIDTH);
  };

  const toggleWorkbenchRightPanel = () => {
    if (compact) {
      if (compactPanel !== "right" && !rightPanelOpen) toggleRightPanel();
      setCompactPanel((current) => current === "right" ? null : "right");
      return;
    }
    toggleRightPanel();
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        background: "var(--surface-base)",
        color: "var(--text-primary)",
        fontFamily: "var(--font-ui)",
        fontSize: "var(--text-md)",
      }}
    >
      <HeaderBar
        leftPanelControls={compact ? "left-sidebar-drawer" : undefined}
        leftPanelAvailable={leftPanelAvailable}
        leftPanelOpen={compact ? compactPanel === "left" : leftSidebarWidth > 0}
        rightPanelControls={compact ? "right-panel-drawer" : undefined}
        rightPanelAvailable={rightPanelAvailable}
        rightPanelOpen={compact ? compactPanel === "right" : rightPanelOpen}
        onToggleLeftPanel={toggleLeftPanel}
        onToggleRightPanel={toggleWorkbenchRightPanel}
      />
      <ConnectionBanner />

        <div className="workbench-mode-body" style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
          {!compact && leftPanelAvailable && leftSidebarWidth > 0 && <SidebarLeft />}
          <div className="workbench-stage" style={{ position: "relative", flex: 1, minWidth: 0, minHeight: 0, display: "flex", overflow: "hidden" }}>
            <div className="workbench-primary" hidden={skillsMarketplaceOpen} style={{ flex: 1, minWidth: 0, minHeight: 0, display: skillsMarketplaceOpen ? "none" : "flex", flexDirection: "column", overflow: "hidden" }}>
              <div className="workbench-content" style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", overflow: "hidden" }}>
                {appMode === "chat" ? <ChatModeShell /> : <WorkbenchModeShell mode={appMode === "code" ? "code" : "cowork"} />}
              </div>
              {appMode !== "chat" && !codePanelMaximized && <BottomDock />}
            </div>
            <div hidden={!skillsMarketplaceOpen} style={{ display: skillsMarketplaceOpen ? "flex" : "none", flex: 1, minWidth: 0 }}>
              <Suspense fallback={<div role="status" className="skills-empty-state">正在加载插件与技能…</div>}>
                {(marketplaceVisited || skillsMarketplaceOpen) && <SkillsMarketplace />}
              </Suspense>
            </div>
            <NarrowSidebarDrawer
              id="right-panel-drawer" label="右侧面板" side="right"
              modal={compact} open={!settingsOpen && rightPanelAvailable && (!compact || compactPanel === "right")}
              onClose={() => setCompactPanel(null)}
            >
              <SidebarRight embedded={compact} visible={!settingsOpen && rightPanelAvailable && (compact ? compactPanel === "right" : rightPanelOpen)} />
            </NarrowSidebarDrawer>
          </div>
        </div>

      {compact && !settingsOpen && leftPanelAvailable && compactPanel === "left" && (
        <NarrowSidebarDrawer id="left-sidebar-drawer" label="左侧栏" side="left" onClose={() => setCompactPanel(null)}>
          <SidebarLeft embedded onNavigate={() => setCompactPanel(null)} />
        </NarrowSidebarDrawer>
      )}
    </div>
  );
};
