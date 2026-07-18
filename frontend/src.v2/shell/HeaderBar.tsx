import {
  Minus,
  Moon,
  PanelLeft,
  PanelRight,
  Plus,
  Search,
  Settings2,
  Square,
  Sun,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import type { ReactNode, Ref } from "react";
import { desktop, isDesktop } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { ContextBudgetIndicator } from "../chat/components/ContextBudgetIndicator";
import { BrandMark } from "../components/icons";
import "../chat/components/context-budget.css";

interface HeaderBarProps {
  leftPanelControls?: string;
  leftPanelAvailable: boolean;
  leftPanelOpen: boolean;
  rightPanelControls?: string;
  rightPanelAvailable: boolean;
  sideChatFallbackButtonRef?: Ref<HTMLButtonElement>;
  rightPanelOpen: boolean;
  onToggleLeftPanel: () => void;
  onToggleRightPanel: () => void;
}

export const HeaderBar = ({
  leftPanelControls,
  leftPanelAvailable,
  leftPanelOpen,
  rightPanelControls,
  rightPanelAvailable,
  sideChatFallbackButtonRef,
  rightPanelOpen,
  onToggleLeftPanel,
  onToggleRightPanel,
}: HeaderBarProps) => {
  const isConnected = useAppStore((s) => s.isConnected);
  const appMode = useAppStore((s) => s.appMode);
  const themeMode = useAppStore((s) => s.themeMode);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const startNewConversation = useAppStore((s) => s.createConversation);

  const createConversationInCurrentMode = () => {
    startNewConversation({ appMode, bindWorkspace: Boolean(workingDirectory) });
  };
  const darkThemeActive =
    themeMode === "dark" ||
    (themeMode === "system" && typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  const nextTheme = darkThemeActive ? "light" : "dark";
  const connectionLabel = isConnected ? "Backend connected" : "Backend disconnected";

  return (
    <header className="header-bar mc-header">
      <div className="mc-header-start">
        <IconButton label="Settings" onClick={toggleSettings} buttonRef={sideChatFallbackButtonRef}>
          <Settings2 />
        </IconButton>
        {leftPanelAvailable && (
          <IconButton
            label={leftPanelOpen ? "Close left sidebar" : "Open left sidebar"}
            onClick={onToggleLeftPanel}
            active={leftPanelOpen}
            ariaControls={leftPanelControls}
            expanded={leftPanelOpen}
          >
            <PanelLeft />
          </IconButton>
        )}
        <IconButton label="New conversation" onClick={createConversationInCurrentMode}>
          <Plus />
        </IconButton>
      </div>

      <div className="mc-header-center">
        <div className="mc-header-brand" aria-label="MiniCode">
          <BrandMark size={18} />
          <span>MiniCode</span>
        </div>
        <div className="mc-header-drag-region" onDoubleClick={() => desktop()?.windowControls.maximize()} />
      </div>

      <div className="mc-header-end">
        <IconButton label="Command palette" onClick={() => toggleCommandPalette()}>
          <Search />
        </IconButton>
        <span className="mc-header-theme">
          <IconButton
            label={darkThemeActive ? "Switch to light theme" : "Switch to dark theme"}
            onClick={() => setThemeMode(nextTheme)}
          >
            {darkThemeActive ? <Sun /> : <Moon />}
          </IconButton>
        </span>
        {appMode !== "code" && (
          <div className="mc-header-budget">
            <ContextBudgetIndicator />
          </div>
        )}
        {rightPanelAvailable && (
          <IconButton
            label={rightPanelOpen ? "Close right panel" : "Open right panel"}
            onClick={onToggleRightPanel}
            active={rightPanelOpen}
            ariaControls={rightPanelControls}
            expanded={rightPanelOpen}
          >
            <PanelRight />
          </IconButton>
        )}
        <span
          role="img"
          aria-label={connectionLabel}
          title={connectionLabel}
          className="mc-connection-status"
          data-connected={isConnected ? "true" : "false"}
        >
          {isConnected ? <Wifi aria-hidden="true" /> : <WifiOff aria-hidden="true" />}
          {!isConnected && <span className="mc-connection-label">Offline</span>}
        </span>

        {isDesktop() && (
          <div className="mc-window-controls">
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
        )}
      </div>
    </header>
  );
};

const IconButton = ({
  active,
  ariaControls,
  buttonRef,
  children,
  expanded,
  label,
  onClick,
}: {
  active?: boolean;
  ariaControls?: string;
  buttonRef?: Ref<HTMLButtonElement>;
  children: ReactNode;
  expanded?: boolean;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    ref={buttonRef}
    title={label}
    aria-label={label}
    aria-controls={ariaControls}
    aria-expanded={expanded}
    className="btn-ghost mc-icon-button mc-header-icon-button"
    data-active={active ? "true" : "false"}
    onClick={onClick}
  >
    {children}
  </button>
);

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
    className={`mc-window-control${danger ? " mc-window-control-danger" : ""}`}
  >
    {children}
  </button>
);
