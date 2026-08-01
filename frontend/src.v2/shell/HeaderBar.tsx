import {
  Minus,
  Folder,
  PanelLeft,
  PanelRight,
  Plus,
  Search,
  Square,
  SquareTerminal,
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
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const startNewConversation = useAppStore((s) => s.createConversation);
  const dockCollapsed = useAppStore((s) => s.dockCollapsed);
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const openBottomTab = useAppStore((s) => s.openBottomTab);
  const closeBottomDock = useAppStore((s) => s.closeBottomDock);

  const createConversationInCurrentMode = () => {
    startNewConversation({ appMode, bindWorkspace: Boolean(workingDirectory) });
  };
  const connectionLabel = isConnected ? "后端已连接" : "后端未连接";
  const projectName = workingDirectory.split(/[\\/]/).filter(Boolean).pop() || "MiniCode";

  return (
    <header className="header-bar mc-header">
      <div className="mc-header-start">
        {leftPanelAvailable && (
          <IconButton
            label={leftPanelOpen ? "收起左侧栏" : "打开左侧栏"}
            onClick={onToggleLeftPanel}
            active={leftPanelOpen}
            ariaControls={leftPanelControls}
            expanded={leftPanelOpen}
          >
            <PanelLeft />
          </IconButton>
        )}
        <IconButton label="新建任务" onClick={createConversationInCurrentMode}>
          <Plus />
        </IconButton>
      </div>

      <div className="mc-header-center">
        <div className="mc-header-brand" aria-label={projectName} title={workingDirectory || "MiniCode"}>
          {workingDirectory ? <Folder size={16} /> : <BrandMark size={18} />}
          <span>{projectName}</span>
        </div>
        <div className="mc-header-drag-region" onDoubleClick={() => desktop()?.windowControls.maximize()} />
      </div>

      <div className="mc-header-end">
        <IconButton label="命令面板" onClick={() => toggleCommandPalette()} buttonRef={sideChatFallbackButtonRef}>
          <Search />
        </IconButton>
        {appMode !== "code" && (
          <div className="mc-header-budget">
            <ContextBudgetIndicator />
          </div>
        )}
        {rightPanelAvailable && (
          <>
            <IconButton
              label={!dockCollapsed && activeBottomTab === "terminal" ? "关闭终端" : "打开终端"}
              onClick={() => {
                if (!dockCollapsed && activeBottomTab === "terminal") closeBottomDock();
                else openBottomTab("terminal");
              }}
              active={!dockCollapsed && activeBottomTab === "terminal"}
              expanded={!dockCollapsed && activeBottomTab === "terminal"}
            >
              <SquareTerminal />
            </IconButton>
            <IconButton
              label={rightPanelOpen ? "关闭右侧栏" : "打开右侧栏"}
              onClick={onToggleRightPanel}
              active={rightPanelOpen}
              ariaControls={rightPanelControls}
              expanded={rightPanelOpen}
            >
              <PanelRight />
            </IconButton>
          </>
        )}
        <span
          role="img"
          aria-label={connectionLabel}
          title={connectionLabel}
          className="mc-connection-status"
          data-connected={isConnected ? "true" : "false"}
        >
          {isConnected ? <Wifi aria-hidden="true" /> : <WifiOff aria-hidden="true" />}
          {!isConnected && <span className="mc-connection-label">离线</span>}
        </span>

        {isDesktop() && (
          <div className="mc-window-controls">
            <WindowControlButton label="最小化" onClick={() => desktop()?.windowControls.minimize()}>
              <Minus size={14} />
            </WindowControlButton>
            <WindowControlButton label="最大化" onClick={() => desktop()?.windowControls.maximize()}>
              <Square size={14} />
            </WindowControlButton>
            <WindowControlButton label="关闭" onClick={() => desktop()?.windowControls.close()} danger>
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
