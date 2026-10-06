import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode } from "react";
import { Bell, Clock3, Moon, Puzzle, Search, Settings, SquarePen, Sun } from "../lib/icons";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_DEFAULT_WIDTH, LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_MAX_WIDTH } from "../stores/shared-helpers";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { Tip } from "../components/Tooltip";
import { openAutomations } from "../lib/automations-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";

export const SidebarLeft = ({
  embedded = false,
  withGlobalRail = false,
  onNavigate,
}: {
  embedded?: boolean;
  withGlobalRail?: boolean;
  onNavigate?: () => void;
}) => {
  const appMode = useAppStore((s) => s.appMode);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const conversationId = useAppStore((s) => s.conversationId);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const leftSidebarExpandedWidth = useAppStore((s) => s.leftSidebarExpandedWidth);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const [activityView, setActivityView] = useState(false);
  const isOpen = embedded || leftSidebarWidth > 0;
  const sidebarRef = useRef<HTMLElement>(null);
  const resizeRef = useRef<{ x: number; width: number } | null>(null);
  useEffect(() => { sidebarRef.current!.inert = !isOpen; }, [isOpen]);
  useEffect(() => () => { if (resizeRef.current) document.body.classList.remove("layout-dragging"); }, []);

  const finishResize = () => {
    resizeRef.current = null;
    document.body.classList.remove("layout-dragging");
  };
  const startResize = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resizeRef.current = { x: event.clientX, width: leftSidebarWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
    document.body.classList.add("layout-dragging");
  };
  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 40 : 10;
    const next = event.key === "ArrowLeft" ? leftSidebarWidth - step
      : event.key === "ArrowRight" ? leftSidebarWidth + step
      : event.key === "Home" ? LEFT_SIDEBAR_MIN_WIDTH
      : event.key === "End" ? LEFT_SIDEBAR_MAX_WIDTH
      : event.key === "Enter" ? LEFT_SIDEBAR_DEFAULT_WIDTH : null;
    if (next === null) return;
    event.preventDefault();
    setLeftSidebarWidth(next);
  };

  const startSession = () => {
    leaveMarketplace();
    createConversation({ appMode: "cowork", bindWorkspace: appMode === "code" && Boolean(workingDirectory) });
    onNavigate?.();
  };
  const navigate = (action: () => void) => {
    action();
    onNavigate?.();
  };
  const leaveMarketplace = () => {
    useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
  };
  const navigateToContent = () => {
    leaveMarketplace();
    onNavigate?.();
  };

  const openAutomationsPanel = () => navigate(openAutomations);
  const isDarkTheme = resolvedTheme === "dark";
  const nextThemeLabel = isDarkTheme ? "切换到浅色模式" : "切换到深色模式";

  const sidebarWidth = embedded ? "100%" : `${leftSidebarWidth}px`;

  return (
    <aside
      ref={sidebarRef}
      className="mc-sidebar-left flex flex-col"
      data-open={isOpen ? "true" : "false"}
      data-embedded={embedded ? "true" : "false"}
      aria-hidden={!isOpen}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: 0,
        boxSizing: "border-box",
        borderRight: embedded || !isOpen ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        width: sidebarWidth,
        minWidth: sidebarWidth,
        maxWidth: sidebarWidth,
        background: "var(--surface-sidebar)",
        opacity: isOpen ? 1 : 0,
        pointerEvents: isOpen ? "auto" : "none",
      }}
    >
      <div className="mc-sidebar-left-inner" style={{ width: embedded ? "100%" : `${leftSidebarWidth || leftSidebarExpandedWidth}px` }}>
      <div className="mc-sidebar-heading">
      <span className="mc-sidebar-brand">MiniCode</span>
      <div className="mc-sidebar-heading-actions">
        <button type="button" className="btn-ghost mc-icon-button" aria-label={activityView ? "返回项目与最近" : "查看任务状态"} title={activityView ? "返回项目与最近" : "查看任务状态"} aria-pressed={activityView} onClick={() => setActivityView((current) => !current)}><Bell size={16} /></button>
        {globalSearchEnabled && <button type="button" className="btn-ghost mc-icon-button" aria-label="搜索" title="搜索任务与命令" onClick={() => navigate(() => toggleCommandPalette())}><Search size={16} /></button>}
      </div>
      </div>

      <nav className="mc-sidebar-nav" aria-label="工作区导航" style={{ display: "grid", gap: 2, padding: "8px 2px 12px" }}>
        <div className="mc-sidebar-primary-actions">
          <SidebarAction icon={<SquarePen />} label="新聊天" onClick={startSession} />
        </div>
        {!withGlobalRail && <SidebarAction icon={<Clock3 />} label="已安排" onClick={openAutomationsPanel} />}
        {!withGlobalRail && <SidebarAction icon={<Puzzle />} label="插件" active={skillsMarketplaceOpen} onClick={() => navigate(() => {
          if (skillsMarketplaceOpen) useAppStore.setState({ skillsMarketplaceTab: "plugins" });
          else toggleSkillsMarketplace("app", "plugins");
        })} />}
      </nav>

      <div className="mc-sidebar-mode-content" data-mode={appMode}>
          <ConversationsTab
            conversationId={conversationId ?? ""}
            onNavigate={navigateToContent}
            onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
            showRecent
            activityView={activityView}
          />
      </div>

      {!withGlobalRail && <div className="mc-sidebar-footer">
        <div className="mc-sidebar-footer-row">
          <SidebarAction icon={<Settings />} label="设置" onClick={() => navigate(() => toggleSettings())} />
          <Tip content={nextThemeLabel}>
            <button
              type="button"
              className="btn-ghost mc-sidebar-theme-toggle"
              aria-label={nextThemeLabel}
              onClick={() => setThemeMode(isDarkTheme ? "light" : "dark")}
            >
              {isDarkTheme ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
            </button>
          </Tip>
        </div>
      </div>}
      </div>
      {!embedded && <div className="mc-sidebar-left-resize-handle" role="separator" aria-orientation="vertical"
        aria-label="调整左侧栏宽度" aria-valuemin={LEFT_SIDEBAR_MIN_WIDTH} aria-valuemax={LEFT_SIDEBAR_MAX_WIDTH}
        aria-valuenow={leftSidebarWidth} tabIndex={isOpen ? 0 : -1} title="拖动调整侧栏宽度，双击恢复默认"
        onPointerDown={startResize}
        onPointerMove={(event) => {
          if (resizeRef.current) setLeftSidebarWidth(resizeRef.current.width + event.clientX - resizeRef.current.x);
        }}
        onPointerUp={finishResize} onPointerCancel={finishResize} onLostPointerCapture={finishResize}
        onDoubleClick={() => setLeftSidebarWidth(LEFT_SIDEBAR_DEFAULT_WIDTH)} onKeyDown={resizeWithKeyboard} />}

      {confirmDialog && (
        <ConfirmDialog
          dialog={confirmDialog}
          onCancel={() => setConfirmDialog(null)}
          onConfirm={() => {
            const action = confirmDialog.onConfirm;
            setConfirmDialog(null);
            action();
          }}
        />
      )}
    </aside>
  );
};

const SidebarAction = ({
  icon,
  label,
  active = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
}) => (
  <button
    type="button"
    className="btn-ghost mc-sidebar-action"
    data-active={active ? "true" : undefined}
    onClick={onClick}
  >
    <span className="mc-sidebar-action-icon" aria-hidden="true">{icon}</span>
    <span>{label}</span>
  </button>
);
