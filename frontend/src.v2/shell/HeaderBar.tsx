import { useI18n } from "../i18n";
import {
  ArrowLeft,
  ArrowRight,
  Minus,
  LoaderCircle,
  Monitor,
  PanelLeft,
  PanelRight,
  Search,
  Square,
  Copy,
  SquareTerminal,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { desktop, isDesktop, runtime, type DesktopMenuKey } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { getConnectionPresentation } from "./connectionPresentation";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { pushToast } from "../overlays/ToastContainer";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";

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
  const { t: tr } = useI18n();
  const { preferences } = useDesktopPreferences();
  const isConnected = useAppStore((s) => s.isConnected);
  const connectionPhase = useAppStore((s) => s.connectionPhase);
  const reconnectAttempt = useAppStore((s) => s.reconnectAttempt);
  const reconnectMaxAttempts = useAppStore((s) => s.reconnectMaxAttempts);
  const connectionError = useAppStore((s) => s.connectionError);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const appMode = useAppStore((state) => state.appMode);
  const settingsOpen = useAppStore((state) => state.settingsOpen);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const dockCollapsed = useAppStore((s) => s.dockCollapsed);
  const rightPanelExpanded = useAppStore((s) => s.rightPanelExpanded);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const activeCodeSlot = panelSlots.find((slot) => slot.focused)
    ?? panelSlots.find((slot) => slot.kind !== "chat") ?? panelSlots.find((slot) => slot.kind === "chat");
  const mainPanelMaximized = Boolean(appMode === "code" && activeCodeSlot?.maximized && activeCodeSlot.kind !== "chat");
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const openBottomTab = useAppStore((s) => s.openBottomTab);
  const closeBottomDock = useAppStore((s) => s.closeBottomDock);
  const terminalVisible = !rightPanelExpanded && !mainPanelMaximized && !dockCollapsed && activeBottomTab === "terminal";

  const connection = getConnectionPresentation({
    isConnected,
    isDesktop: isDesktop(),
    hasRuntimeToken: Boolean(runtime()?.runtimeToken?.trim()),
    connectionPhase,
    reconnectAttempt,
    reconnectMaxAttempts,
    connectionError,
  }, tr);
  const projectName = workingDirectory.split(/[\\/]/).filter(Boolean).pop() || "MiniCode";
  const conversationId = useAppStore((state) => state.conversationId);
  const pendingSwitch = useAppStore((state) => state.pendingConversationSwitchId);
  const [history, setHistory] = useState<{ ids: string[]; index: number }>({ ids: [], index: -1 });
  const requestedHistory = useRef<{ id: string; index: number } | null>(null);
  useEffect(() => {
    if (!conversationId) return;
    const requested = requestedHistory.current;
    requestedHistory.current = null;
    setHistory((previous) => {
      if (requested?.id === conversationId) return { ...previous, index: requested.index };
      if (previous.ids[previous.index] === conversationId) return previous;
      const ids = [...previous.ids.slice(0, previous.index + 1), conversationId];
      return { ids, index: ids.length - 1 };
    });
  }, [conversationId]);
  const navigateHistory = (index: number) => {
    const id = history.ids[index];
    requestedHistory.current = { id, index };
    useAppStore.getState().requestConversationSwitch(id);
  };
  useEffect(() => {
    const back = () => { if (!pendingSwitch && history.index > 0) navigateHistory(history.index - 1); };
    const forward = () => { if (!pendingSwitch && history.index + 1 < history.ids.length) navigateHistory(history.index + 1); };
    window.addEventListener("navigate-back", back);
    window.addEventListener("navigate-forward", forward);
    return () => { window.removeEventListener("navigate-back", back); window.removeEventListener("navigate-forward", forward); };
  }, [history, pendingSwitch]);
  const [menu, setMenu] = useState<{ key: DesktopMenuKey; x: number; y: number } | null>(null);
  const [windowMaximized, setWindowMaximized] = useState(false);
  useEffect(() => {
    if (!isDesktop()) return;
    const controls = desktop()!.windowControls;
    const unsubscribe = controls.onStateChanged(state => setWindowMaximized(state.isMaximized));
    void controls.getState().then(state => setWindowMaximized(state.isMaximized));
    return unsubscribe;
  }, []);
  const browserEditTarget = useRef<HTMLElement | null>(null);
  const editSelection = (command: string) => {
    browserEditTarget.current?.focus({ preventScroll: true });
    document.execCommand(command);
  };
  const browserMenuItems = (key: DesktopMenuKey): ContextMenuItem[] => {
    const state = useAppStore.getState();
    if (key === "file") return [
      { label: tr("新聊天"), onClick: () => state.createConversation({ appMode: "cowork", bindWorkspace: state.appMode === "code" && Boolean(state.workingDirectory) }) },
      { label: tr("打开文件夹…"), onClick: () => { void openWorkspaceFolder(); } },
      { label: tr("设置…"), onClick: () => { if (!state.settingsOpen) state.toggleSettings(); } },
    ];
    if (key === "edit") return [
      { label: tr("撤销"), onClick: () => editSelection("undo") },
      { label: tr("重做"), onClick: () => editSelection("redo") },
      { label: tr("复制"), onClick: () => editSelection("copy") },
      { label: tr("全选"), onClick: () => editSelection("selectAll") },
    ];
    if (key === "view") return [
      { label: leftPanelOpen ? tr("收起左侧栏") : tr("打开左侧栏"), onClick: onToggleLeftPanel },
      { label: rightPanelOpen ? tr("关闭右侧栏") : tr("打开右侧栏"), onClick: onToggleRightPanel },
      { label: tr("打开终端"), onClick: () => state.openTerminal() },
    ];
    return [{ label: tr("键盘快捷键"), onClick: () => { if (!state.shortcutsHelpOpen) state.toggleShortcutsHelp(); } }];
  };

  return (
    <header className="header-bar mc-header">
      <div className="mc-header-start">
        <button type="button" className="mc-titlebar-navigation" aria-label={tr("上一条访问的对话")} title={tr("后退")} disabled={Boolean(pendingSwitch) || history.index <= 0} onClick={() => navigateHistory(history.index - 1)}><ArrowLeft size={17} /></button>
        <button type="button" className="mc-titlebar-navigation" aria-label={tr("下一条访问的对话")} title={tr("前进")} disabled={Boolean(pendingSwitch) || history.index >= history.ids.length - 1} onClick={() => navigateHistory(history.index + 1)}><ArrowRight size={17} /></button>
        {leftPanelAvailable && (
          <IconButton
            label={leftPanelOpen ? tr("收起左侧栏") : tr("打开左侧栏")}
            onClick={onToggleLeftPanel}
            active={leftPanelOpen}
            ariaControls={leftPanelControls}
            expanded={leftPanelOpen}
          >
            <PanelLeft />
          </IconButton>
        )}
      </div>

      <div className="mc-header-center">
        <nav className="mc-titlebar-menus" aria-label={tr("应用菜单")}>
          {([['file', tr("文件")], ['edit', tr("编辑")], ['view', tr("视图")], ['help', tr("帮助")]] as const).map(([key, label]) => <button key={key} type="button" aria-haspopup="menu" aria-expanded={menu?.key === key} onMouseDown={(event) => event.preventDefault()} onClick={(event) => {
            const native = desktop();
            if (native) {
              setMenu(null);
              void native.menu.popup(key).catch((error: Error) => pushToast(error.message, "error"));
            } else {
              browserEditTarget.current = document.activeElement as HTMLElement;
              const bounds = event.currentTarget.getBoundingClientRect();
              setMenu(menu?.key === key ? null : { key, x: bounds.left, y: bounds.bottom + 4 });
            }
          }}>{label}</button>)}
        </nav>
        <span className="mc-header-project-accessible" aria-label={projectName} title={workingDirectory || "MiniCode"} />
        <div className="mc-header-drag-region" onDoubleClick={() => desktop()?.windowControls.maximize()} />
      </div>

      <div className="mc-header-end">
        {appMode === "code" && !settingsOpen && <>
        <IconButton label={tr("命令面板")} onClick={() => toggleCommandPalette()} buttonRef={sideChatFallbackButtonRef}>
          <Search />
        </IconButton>
        {rightPanelAvailable && (
          <>
            {preferences?.showBottomPanel !== false && <IconButton
              label={terminalVisible ? tr("关闭终端") : tr("打开终端")}
              onClick={() => {
                if (terminalVisible) closeBottomDock();
                else openBottomTab("terminal");
              }}
              active={terminalVisible}
              expanded={terminalVisible}
            >
              <SquareTerminal />
            </IconButton>}
            <IconButton
              label={rightPanelOpen ? tr("关闭右侧栏") : tr("打开右侧栏")}
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
          aria-label={connection.accessibleLabel}
          title={connection.accessibleLabel}
          className={`mc-connection-status${isConnected ? " mc-connected-status" : ""}`}
          data-connected={isConnected ? "true" : "false"}
          data-kind={connection.kind}
        >
          {connection.kind === "connected" && <Wifi aria-hidden="true" />}
          {connection.kind === "preview" && <Monitor aria-hidden="true" />}
          {(connection.kind === "connecting" || connection.kind === "reconnecting") && <LoaderCircle className="mc-connection-status-spinner" aria-hidden="true" />}
          {(connection.kind === "warning" || connection.kind === "failed") && <WifiOff aria-hidden="true" />}
          {connection.shortLabel && <span className="mc-connection-label">{connection.shortLabel}</span>}
        </span>
        </>}

        {isDesktop() && (
          <div className="mc-window-controls">
            <WindowControlButton label={tr("最小化")} onClick={() => desktop()?.windowControls.minimize()}>
              <Minus size={14} />
            </WindowControlButton>
            <WindowControlButton label={windowMaximized ? tr("还原窗口") : tr("最大化")} onClick={() => desktop()?.windowControls.maximize()}>
              {windowMaximized ? <Copy size={14} /> : <Square size={14} />}
            </WindowControlButton>
            <WindowControlButton label={tr("关闭")} onClick={() => desktop()?.windowControls.close()} danger>
              <X size={14} />
            </WindowControlButton>
          </div>
        )}
      </div>
      {menu && <ContextMenu items={browserMenuItems(menu.key)} position={{ x: menu.x, y: menu.y }} onClose={() => setMenu(null)} />}
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
