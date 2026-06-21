import { Menu, Minus, Moon, PanelLeft, PanelRight, Plus, Search, Square, Sun, X } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { desktop, isDesktop } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { ContextBudgetIndicator } from "../chat/components/ContextBudgetIndicator";
import "../chat/components/context-budget.css";

export const HeaderBar = () => {
  const isConnected = useAppStore((s) => s.isConnected);
  const appMode = useAppStore((s) => s.appMode);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const themeMode = useAppStore((s) => s.themeMode);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const startNewConversation = useAppStore((s) => s.createConversation);

  const toggleLeftPanel = () => {
    const current = useAppStore.getState().leftSidebarWidth;
    setLeftSidebarWidth(current > 0 ? 0 : 320);
  };
  const createConversationInCurrentMode = () => {
    startNewConversation({ appMode, bindWorkspace: Boolean(workingDirectory) });
  };
  const darkThemeActive =
    themeMode === "dark" ||
    (themeMode === "system" && typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  const nextTheme = darkThemeActive ? "light" : "dark";

  return (
    <header className="header-bar" style={headerStyle}>
      <div style={leftGroupStyle}>
        <IconButton label="Settings" onClick={toggleSettings}>
          <Menu size={18} />
        </IconButton>
        <IconButton label="Toggle left sidebar" onClick={toggleLeftPanel}>
          <PanelLeft size={17} />
        </IconButton>
        <IconButton label="New conversation" onClick={createConversationInCurrentMode}>
          <Plus size={17} />
        </IconButton>
      </div>

      <IconButton label="Command palette" onClick={() => toggleCommandPalette()}>
        <Search size={16} />
      </IconButton>
      <IconButton
        label={darkThemeActive ? "Switch to light theme" : "Switch to dark theme"}
        onClick={() => setThemeMode(nextTheme)}
      >
        {darkThemeActive ? <Sun size={16} /> : <Moon size={16} />}
      </IconButton>
      <ContextBudgetIndicator />
      <IconButton label={rightPanelOpen ? "Close right panel" : "Open right panel"} onClick={toggleRightPanel} active={rightPanelOpen}>
        <PanelRight size={17} />
      </IconButton>
      <span aria-label={isConnected ? "Backend connected" : "Backend disconnected"} title={isConnected ? "Backend connected" : "Backend disconnected"} style={connectionDotStyle(isConnected)} />

      {isDesktop() && (
        <>
          <div style={windowDragStyle} onDoubleClick={() => desktop()?.windowControls.maximize()} />
          <div style={windowControlsStyle}>
            <WindowControlButton label="Minimize" onClick={() => desktop()?.windowControls.minimize()}>
              <Minus size={14} />
            </WindowControlButton>
            <WindowControlButton label="Maximize" onClick={() => desktop()?.windowControls.maximize()}>
              <Square size={11} />
            </WindowControlButton>
            <WindowControlButton label="Close" onClick={() => desktop()?.windowControls.close()} danger>
              <X size={14} />
            </WindowControlButton>
          </div>
        </>
      )}
    </header>
  );
};

const IconButton = ({
  active,
  children,
  label,
  onClick,
}: {
  active?: boolean;
  children: ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    className="btn-ghost"
    onClick={onClick}
    style={{
      width: 36,
      height: 36,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      border: "1px solid transparent",
      borderRadius: "var(--radius-sm, 7px)",
      background: active ? "var(--surface-page)" : undefined,
      color: active ? "var(--text-primary)" : undefined,
      cursor: "pointer",
      padding: 0,
      WebkitAppRegion: "no-drag",
    } as CSSProperties & { WebkitAppRegion?: string }}
  >
    {children}
  </button>
);

const headerStyle: CSSProperties & { WebkitAppRegion?: string } = {
  height: "var(--header-height)",
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 10px",
  borderBottom: "1px solid color-mix(in oklch, var(--border-subtle) 35%, transparent)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  userSelect: "none",
  WebkitAppRegion: "drag",
};

const leftGroupStyle: CSSProperties & { WebkitAppRegion?: string } = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  WebkitAppRegion: "no-drag",
};

const connectionDotStyle = (connected: boolean): CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: "50%",
  background: connected ? "var(--state-success)" : "var(--state-danger)",
  margin: "0 6px 0 2px",
});

const windowDragStyle: CSSProperties & { WebkitAppRegion?: string } = {
  alignSelf: "stretch",
  flex: 1,
  minWidth: 40,
  WebkitAppRegion: "drag",
};

const WindowControlButton = ({
  children,
  danger,
  label,
  onClick,
}: {
  children: ReactNode;
  danger?: boolean;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    onClick={onClick}
    className={danger ? "window-control-close" : "window-control"}
    style={{
      width: 46,
      height: 30,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      border: 0,
      background: "transparent",
      color: "var(--text-secondary)",
      cursor: "pointer",
      padding: 0,
      borderRadius: 0,
      WebkitAppRegion: "no-drag",
    } as CSSProperties & { WebkitAppRegion?: string }}
  >
    {children}
  </button>
);

const windowControlsStyle: CSSProperties & { WebkitAppRegion?: string } = {
  display: "flex",
  alignItems: "center",
  alignSelf: "stretch",
  marginRight: -10,
  WebkitAppRegion: "no-drag",
};
