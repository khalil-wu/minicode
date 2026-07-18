import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { HeaderBar } from "./HeaderBar";
import { SidebarLeft } from "./SidebarLeft";
import { SidebarRight } from "./SidebarRight";
import { MainSlots } from "./MainSlots";
import { SideChatPanel } from "../panels/SideChatPanel";
import { ChatPane } from "../chat/ChatPane";
import { CoworkHome } from "./CoworkHome";
import { useAppStore } from "../stores";
import { SafeBoundary } from "./ChunkErrorBoundary";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { isDesktop, runtime } from "../desktop/runtime";
import { hasVisibleActiveConversation } from "../chat/activeConversation";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";

const COMPACT_WORKBENCH_MAX_WIDTH = 1023;

const isCompactWorkbench = () => (
  typeof window !== "undefined" && window.innerWidth <= COMPACT_WORKBENCH_MAX_WIDTH
);

const useCompactWorkbench = () => {
  const [compact, setCompact] = useState(isCompactWorkbench);
  useEffect(() => {
    const onResize = () => setCompact(isCompactWorkbench());
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return compact;
};

const connectionBannerMessage = (): string => {
  const runtimeInfo = runtime();
  const hasToken = Boolean(runtimeInfo?.runtimeToken?.trim());
  if (isDesktop()) return "Connecting to MiniCode backend...";
  if (!hasToken) return "Development browser is missing the Electron runtime token. Open the MiniCode desktop app for full access.";
  return "Backend unavailable. Check that the MiniCode backend is running.";
};

const ConnectionBanner = () => {
  const isConnected = useAppStore((s) => s.isConnected);
  const previousConnectedRef = useRef(isConnected);
  const [announcement, setAnnouncement] = useState(
    isConnected ? "Backend connected" : connectionBannerMessage(),
  );
  useEffect(() => {
    if (previousConnectedRef.current === isConnected) return;
    previousConnectedRef.current = isConnected;
    setAnnouncement(isConnected ? "Backend connected" : connectionBannerMessage());
  }, [isConnected]);
  return (
    <>
      <span role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </span>
      {!isConnected && <div
      aria-hidden="true"
      className="mc-connection-banner flex items-center gap-2 px-4 py-1.5"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 16px",
        background: "color-mix(in oklch, var(--state-warning) 15%, var(--surface-base))",
        borderBottom: "1px solid var(--state-warning)",
        fontSize: "var(--text-sm)",
        color: "var(--state-warning)",
      }}
    >
      <span
        className="w-2 h-2 rounded-full thinking-pulse-dot"
        style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--state-warning)" }}
      />
      {connectionBannerMessage()}
      </div>}
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

const CoworkModeShell = ({
  isEmptyConversation,
  showDesktopPanels,
}: {
  isEmptyConversation: boolean;
  showDesktopPanels: boolean;
}) => {
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  return (
    <div
      className="flex-1 flex overflow-hidden min-h-0"
      style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}
    >
      {isEmptyConversation ? (
        <CoworkHome />
      ) : (
        <ChatModeShell />
      )}
      {showDesktopPanels && rightPanelOpen && !isEmptyConversation && <SidebarRight />}
    </div>
  );
};

const CodeModeShell = ({
  activeMaximized,
  isEmptyConversation,
  showDesktopPanels,
}: {
  activeMaximized: boolean;
  isEmptyConversation: boolean;
  showDesktopPanels: boolean;
}) => {
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const ensureCodeLayout = useAppStore((s) => s.ensureCodeLayout);
  useEffect(() => {
    ensureCodeLayout();
  }, [ensureCodeLayout]);
  return (
    <>
      <div
        className="flex-1 flex overflow-hidden min-h-0"
        style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}
      >
        <MainSlots mode="tabs" />
        {showDesktopPanels && !activeMaximized && rightPanelOpen && !isEmptyConversation && <SidebarRight />}
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
}: {
  children: ReactNode;
  id: string;
  label: string;
  onClose: () => void;
  side: "left" | "right";
}) => {
  const dialogRef = useFocusTrap(true);
  useEscapeKey(onClose);
  return (
    <div
      onMouseDown={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: "var(--z-drawer)",
        display: "flex",
        justifyContent: side === "left" ? "flex-start" : "flex-end",
        background: "var(--backdrop-subtle)",
        padding: 8,
      }}
    >
      <div
        ref={dialogRef}
        id={id}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        style={{
          width: "min(380px, calc(100vw - 16px))",
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          background: "var(--surface-base)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 8px)",
          boxShadow: "var(--shadow-strong, var(--shadow-medium))",
        }}
      >
        <div
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
            aria-label={`Close ${label.toLowerCase()}`}
            title={`Close ${label.toLowerCase()}`}
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </div>
        <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>{children}</div>
      </div>
    </div>
  );
};

export const WorkbenchShell = () => {
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const toggleSideChat = useAppStore((s) => s.toggleSideChat);
  const appMode = useAppStore((s) => s.appMode);
  const conversations = useAppStore((s) => s.conversations);
  const conversationId = useAppStore((s) => s.conversationId);
  const messages = useAppStore((s) => s.messages);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const compact = useCompactWorkbench();
  const [compactPanel, setCompactPanel] = useState<"left" | "right" | null>(null);
  const previousRightPanelOpenRef = useRef(rightPanelOpen);
  const previousRightStackTabRef = useRef(rightStackTab);
  const sideChatFallbackButtonRef = useRef<HTMLButtonElement>(null);
  const sideChatDialogRef = useFocusTrap(sideChatOpen, sideChatFallbackButtonRef);
  useEscapeKey(toggleSideChat, sideChatOpen);
  const coworkConversationEmpty =
    !hasVisibleActiveConversation(conversationId, conversations) || messages.length === 0;
  const activeCodeSlot =
    panelSlots.find((slot) => slot.focused) ??
    panelSlots.find((slot) => slot.kind !== "chat") ??
    panelSlots.find((slot) => slot.kind === "chat");
  const codePanelMaximized = Boolean(activeCodeSlot?.maximized && activeCodeSlot.kind !== "chat");
  const codeConversationEmpty = activeCodeSlot?.kind === "chat" && coworkConversationEmpty;
  const leftPanelAvailable = appMode === "cowork" || (appMode === "code" && !codePanelMaximized);
  const rightPanelAvailable =
    (appMode === "cowork" && !coworkConversationEmpty) ||
    (appMode === "code" && !codePanelMaximized && !codeConversationEmpty);

  useEffect(() => {
    if (!compact) setCompactPanel(null);
  }, [compact]);

  useEffect(() => {
    if (sideChatOpen) setCompactPanel(null);
  }, [sideChatOpen]);

  useEffect(() => {
    if (compactPanel === "left" && !leftPanelAvailable) setCompactPanel(null);
    if (compactPanel === "right" && !rightPanelAvailable) setCompactPanel(null);
  }, [compactPanel, leftPanelAvailable, rightPanelAvailable]);

  useEffect(() => {
    const panelWasOpened = !previousRightPanelOpenRef.current && rightPanelOpen;
    const requestedTabChanged = previousRightStackTabRef.current !== rightStackTab;
    previousRightPanelOpenRef.current = rightPanelOpen;
    previousRightStackTabRef.current = rightStackTab;
    if (compact && rightPanelAvailable && (panelWasOpened || requestedTabChanged)) setCompactPanel("right");
  }, [compact, rightPanelAvailable, rightPanelOpen, rightStackTab]);

  const toggleLeftPanel = () => {
    if (compact) {
      setCompactPanel((current) => current === "left" ? null : "left");
      return;
    }
    setLeftSidebarWidth(leftSidebarWidth > 0 ? 0 : 320);
  };

  const toggleWorkbenchRightPanel = () => {
    if (compact) {
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
        sideChatFallbackButtonRef={sideChatFallbackButtonRef}
        rightPanelOpen={compact ? compactPanel === "right" : rightPanelOpen}
        onToggleLeftPanel={toggleLeftPanel}
        onToggleRightPanel={toggleWorkbenchRightPanel}
      />
      <ConnectionBanner />

      {appMode === "chat" && <ChatModeShell />}
      {appMode !== "chat" && (
        <div className="workbench-mode-body" style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
          {!compact && leftPanelAvailable && <SidebarLeft />}
          <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
            {appMode === "code" && <CodeModeShell activeMaximized={codePanelMaximized} isEmptyConversation={codeConversationEmpty} showDesktopPanels={!compact} />}
            {appMode === "cowork" && <CoworkModeShell isEmptyConversation={coworkConversationEmpty} showDesktopPanels={!compact} />}
          </div>
        </div>
      )}

      {compact && !sideChatOpen && leftPanelAvailable && compactPanel === "left" && (
        <NarrowSidebarDrawer id="left-sidebar-drawer" label="Left sidebar" side="left" onClose={() => setCompactPanel(null)}>
          <SidebarLeft embedded onNavigate={() => setCompactPanel(null)} />
        </NarrowSidebarDrawer>
      )}
      {compact && !sideChatOpen && rightPanelAvailable && compactPanel === "right" && (
        <NarrowSidebarDrawer id="right-panel-drawer" label="Right panel" side="right" onClose={() => setCompactPanel(null)}>
          <SidebarRight key={rightStackTab} embedded initialTab={rightStackTab} />
        </NarrowSidebarDrawer>
      )}

      {sideChatOpen && (
        <div
          onMouseDown={toggleSideChat}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: "var(--z-drawer)",
            background: "var(--backdrop-subtle)",
            display: "flex",
            justifyContent: "flex-end",
            padding: "48px 16px 16px",
            pointerEvents: "auto",
          }}
        >
          <div
            ref={sideChatDialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="Side chat"
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            style={{
              width: "min(420px, calc(100vw - 32px))",
              maxWidth: "100%",
              display: "flex",
              flexDirection: "column",
              background: "var(--surface-page)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md, 8px)",
              boxShadow: "var(--shadow-medium)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                alignItems: "center",
                display: "flex",
                padding: "8px 12px",
                borderBottom: "1px solid var(--border-subtle)",
                background: "var(--surface-soft)",
              }}
            >
              <span style={{ flex: 1, fontSize: "var(--text-sm)", fontWeight: 600 }}>Side Chat</span>
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginRight: 8 }}>Ctrl+;</span>
              <button
                type="button"
                onClick={toggleSideChat}
                aria-label="Close side chat"
                title="Close side chat"
                className="btn-ghost mc-icon-button mc-icon-button-compact"
              >
                <X size={14} />
              </button>
            </div>
            <SideChatPanel />
          </div>
        </div>
      )}
    </div>
  );
};
