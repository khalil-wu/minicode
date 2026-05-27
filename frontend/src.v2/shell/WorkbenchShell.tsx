import { useEffect } from "react";
import { X } from "lucide-react";
import { HeaderBar } from "./HeaderBar";
import { SidebarLeft } from "./SidebarLeft";
import { SidebarRight } from "./SidebarRight";
import { MainSlots } from "./MainSlots";
import { BottomDock } from "./BottomDock";
import { SideChatPanel } from "../panels/SideChatPanel";
import { ChatPane } from "../chat/ChatPane";
import { useAppStore } from "../stores";

const ConnectionBanner = () => {
  const isConnected = useAppStore((s) => s.isConnected);
  if (isConnected) return null;
  return (
    <div
      style={{
        padding: "6px 16px",
        background: "color-mix(in oklch, var(--state-warning) 15%, var(--surface-base))",
        borderBottom: "1px solid var(--state-warning)",
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: "var(--text-sm)",
        color: "var(--state-warning)",
      }}
    >
      <span style={{ animation: "thinking-pulse 1.5s ease-in-out infinite", width: 8, height: 8, borderRadius: "50%", background: "var(--state-warning)" }} />
      Reconnecting to server...
    </div>
  );
};

const ChatModeShell = () => (
  <div
    style={{
      flex: 1,
      minHeight: 0,
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      maxWidth: 980,
      width: "100%",
      margin: "0 auto",
    }}
  >
    <ChatPane />
  </div>
);

const CoworkModeShell = () => {
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  return (
    <div style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}>
      <SidebarLeft />
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          maxWidth: 760,
          margin: "0 auto",
        }}
      >
        <ChatPane />
      </div>
      {rightPanelOpen && <SidebarRight />}
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
      <div style={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}>
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
            zIndex: 900,
            background: "rgba(0,0,0,0.18)",
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
              boxShadow: "0 8px 32px rgba(0,0,0,0.3)",
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
