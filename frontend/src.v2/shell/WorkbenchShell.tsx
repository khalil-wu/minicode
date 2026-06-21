import { useEffect } from "react";
import { X } from "lucide-react";
import { HeaderBar } from "./HeaderBar";
import { SidebarLeft } from "./SidebarLeft";
import { SidebarRight } from "./SidebarRight";
import { MainSlots } from "./MainSlots";
import { BottomDock } from "./BottomDock";
import { SideChatPanel } from "../panels/SideChatPanel";
import { ChatPane } from "../chat/ChatPane";
import { CoworkHome } from "./CoworkHome";
import { useAppStore } from "../stores";
import { SafeBoundary } from "./ChunkErrorBoundary";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { isDesktop, runtime } from "../desktop/runtime";
import { hasVisibleActiveConversation } from "../chat/activeConversation";

const connectionBannerMessage = (): string => {
  const runtimeInfo = runtime();
  const hasToken = Boolean(runtimeInfo?.runtimeToken?.trim());
  if (isDesktop()) return "Connecting to MiniCode backend...";
  if (!hasToken) return "Development browser is missing the Electron runtime token. Open the MiniCode desktop app for full access.";
  return "Backend unavailable. Check that the MiniCode backend is running.";
};

const ConnectionBanner = () => {
  const isConnected = useAppStore((s) => s.isConnected);
  if (isConnected) return null;
  return (
    <div
      className="flex items-center gap-2 px-4 py-1.5"
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
        className="w-2 h-2 rounded-full"
        style={{ width: 8, height: 8, borderRadius: "50%", animation: "thinking-pulse 1.5s ease-in-out infinite", background: "var(--state-warning)" }}
      />
      {connectionBannerMessage()}
    </div>
  );
};

const ChatModeShell = () => (
  <div
    className="flex-1 min-h-0 flex flex-col overflow-hidden w-full"
    style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", width: "100%" }}
  >
    <SafeBoundary fallback={<ChatErrorFallback />}>
      <ChatPane />
    </SafeBoundary>
  </div>
);

const CoworkModeShell = () => {
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const isEmptyConversation = useAppStore(
    (s) => !hasVisibleActiveConversation(s.conversationId, s.conversations) || s.messages.length === 0,
  );
  return (
    <div
      className="flex-1 flex overflow-hidden min-h-0"
      style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}
    >
      <SidebarLeft />
      {isEmptyConversation ? (
        <CoworkHome />
      ) : (
        <div
          className="flex-1 min-h-0 flex flex-col overflow-hidden"
          style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}
        >
          <SafeBoundary fallback={<ChatErrorFallback />}>
            <ChatPane />
          </SafeBoundary>
        </div>
      )}
      {rightPanelOpen && !isEmptyConversation && <SidebarRight />}
    </div>
  );
};

const CodeModeShell = () => {
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const dockCollapsed = useAppStore((s) => s.dockCollapsed);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const ensureCodeLayout = useAppStore((s) => s.ensureCodeLayout);
  const activeSlot =
    panelSlots.find((slot) => slot.focused) ??
    panelSlots.find((slot) => slot.kind !== "chat") ??
    panelSlots.find((slot) => slot.kind === "chat");
  const activeMaximized = Boolean(activeSlot?.maximized && activeSlot.kind !== "chat");
  useEffect(() => {
    ensureCodeLayout();
  }, [ensureCodeLayout]);
  return (
    <>
      <div
        className="flex-1 flex overflow-hidden min-h-0"
        style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}
      >
        {!activeMaximized && <SidebarLeft />}
        <MainSlots />
        {!activeMaximized && rightPanelOpen && <SidebarRight />}
      </div>
      {!activeMaximized && !dockCollapsed && (
        <BottomDock />
      )}
    </>
  );
};

export const WorkbenchShell = () => {
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const toggleSideChat = useAppStore((s) => s.toggleSideChat);
  const appMode = useAppStore((s) => s.appMode);

  useEffect(() => {
    if (!sideChatOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") toggleSideChat();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sideChatOpen, toggleSideChat]);

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
      <HeaderBar />
      <ConnectionBanner />

      {appMode === "chat" && <ChatModeShell />}
      {appMode === "code" && <CodeModeShell />}
      {appMode === "cowork" && <CoworkModeShell />}

      {sideChatOpen && (
        <div
          onMouseDown={toggleSideChat}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: "var(--z-drawer)",  // 🆕 使用统一的 z-index
            background: "var(--backdrop-subtle)",
            display: "flex",
            justifyContent: "flex-end",
            padding: "48px 16px 16px",
            pointerEvents: "auto",
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-label="Side chat"
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
                style={{
                  width: 22,
                  height: 22,
                  border: 0,
                  borderRadius: "var(--radius-sm, 4px)",
                  background: "transparent",
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  padding: 0,
                }}
              >
                <X size={14} />
              </button>
            </div>
            <SideChatPanel />
          </section>
        </div>
      )}
    </div>
  );
};
