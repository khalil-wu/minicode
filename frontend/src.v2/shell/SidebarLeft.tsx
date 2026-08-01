import { useEffect, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { Blend, ChevronDown, Clock3, Code2, Files, FolderOpen, MessageSquareText, Moon, Search, Settings, SquarePen, Sun } from "lucide-react";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_DEFAULT_WIDTH, LEFT_SIDEBAR_MAX_WIDTH, LEFT_SIDEBAR_MIN_WIDTH } from "../stores/shared-helpers";
import { FileTree } from "./FileTree";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { isConversationRunning } from "./sessionStatus";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openAutomations } from "../lib/automations-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { modeSwitchButtonStyle, modeSwitchStyle } from "./sidebarStyles";

type SidebarTab = "conversations" | "files";

export const SidebarLeft = ({
  embedded = false,
  onNavigate,
}: {
  embedded?: boolean;
  onNavigate?: () => void;
}) => {
  const appMode = useAppStore((s) => s.appMode);
  const themeMode = useAppStore((s) => s.themeMode);
  const conversationId = useAppStore((s) => s.conversationId);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [tab, setTab] = useState<SidebarTab>(appMode === "code" ? "files" : "conversations");
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const isOpen = embedded || leftSidebarWidth > 0;

  const runningCount = useAppStore((s) => {
    let count = 0;
    for (const c of s.conversations) {
      if (!c.archived && isConversationRunning({
        conversationId: c.id,
        activeConversationId: s.conversationId,
        activeIsStreaming: s.isStreaming,
        conversationStreaming: s.conversationStreaming,
      })) {
        count++;
      }
    }
    return count;
  });

  useEffect(() => {
    setTab(appMode === "code" ? "files" : "conversations");
  }, [appMode]);

  const switchAppMode = (nextMode: "cowork" | "code") => {
    setAppMode(nextMode);
    setTab(nextMode === "code" ? "files" : "conversations");
    onNavigate?.();
  };
  const switchSidebarTab = (nextTab: SidebarTab) => {
    setTab(nextTab);
    if (nextTab === "files" && appMode !== "code") setAppMode("code");
    onNavigate?.();
  };
  const startSession = () => {
    createConversation({ appMode, bindWorkspace: appMode === "code" && Boolean(workingDirectory) });
    onNavigate?.();
  };
  const navigate = (action: () => void) => {
    action();
    onNavigate?.();
  };

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startWidth = leftSidebarWidth || LEFT_SIDEBAR_MIN_WIDTH;
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const nextWidth = startWidth + moveEvent.clientX - startX;
      setLeftSidebarWidth(nextWidth < 42 ? 0 : nextWidth);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", cleanup);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.body.classList.remove("layout-dragging");
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("layout-dragging");
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", cleanup);
  };

  const resetSidebarWidth = () => setLeftSidebarWidth(LEFT_SIDEBAR_DEFAULT_WIDTH);
  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = leftSidebarWidth - step;
    else if (event.key === "ArrowRight") nextWidth = leftSidebarWidth + step;
    else if (event.key === "Home") nextWidth = LEFT_SIDEBAR_MIN_WIDTH;
    else if (event.key === "End") nextWidth = LEFT_SIDEBAR_MAX_WIDTH;
    else if (event.key === "Enter") nextWidth = LEFT_SIDEBAR_DEFAULT_WIDTH;
    if (nextWidth == null) return;
    event.preventDefault();
    setLeftSidebarWidth(nextWidth);
  };
  const openAutomationsPanel = () => navigate(openAutomations);
  const resolvedTheme = document.documentElement.getAttribute("data-theme");
  const isDarkTheme = themeMode === "dark" || (themeMode === "system" && resolvedTheme !== "light");
  const nextThemeLabel = isDarkTheme ? "切换到浅色模式" : "切换到深色模式";

  const sidebarWidth = embedded ? "100%" : isOpen ? `${leftSidebarWidth}px` : 0;

  return (
    <aside
      className="mc-sidebar-left anim-slide-left sidebar-animate flex flex-col overflow-hidden box-border"
      data-open={isOpen ? "true" : "false"}
      data-embedded={embedded ? "true" : "false"}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: isOpen ? "14px 10px 10px" : 0,
        boxSizing: "border-box",
        borderRight: embedded || !isOpen ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        width: sidebarWidth,
        minWidth: sidebarWidth,
        maxWidth: sidebarWidth,
        background: "color-mix(in oklch, var(--surface-page) 82%, var(--surface-base))",
        opacity: isOpen ? 1 : 0,
        pointerEvents: isOpen ? "auto" : "none",
      }}
    >
      <div className="mc-sidebar-brand-row">
        <button
          type="button"
          className="mc-sidebar-brand"
          onClick={() => switchSidebarTab("conversations")}
          aria-label="返回会话列表"
        >
          <span>MiniCode</span>
          <ChevronDown className="mc-sidebar-brand-chevron" aria-hidden="true" />
        </button>
        {runningCount > 0 && (
          <span className="mc-sidebar-running-count" aria-label={`${runningCount} 个任务运行中`}>
            {runningCount} 运行中
          </span>
        )}
      </div>

      <div role="tablist" aria-label="工作模式" style={{ ...modeSwitchStyle, margin: "8px 2px 2px" }}>
        <button
          type="button"
          role="tab"
          className="mc-sidebar-mode-tab"
          aria-selected={appMode === "cowork"}
          tabIndex={appMode === "cowork" ? 0 : -1}
          onClick={() => switchAppMode("cowork")}
          style={modeSwitchButtonStyle}
        >
          <span className="mc-sidebar-mode-icon" aria-hidden="true"><MessageSquareText /></span>
          协作
        </button>
        <button
          type="button"
          role="tab"
          className="mc-sidebar-mode-tab"
          aria-selected={appMode === "code"}
          tabIndex={appMode === "code" ? 0 : -1}
          onClick={() => switchAppMode("code")}
          style={modeSwitchButtonStyle}
        >
          <span className="mc-sidebar-mode-icon" aria-hidden="true"><Code2 /></span>
          代码
        </button>
      </div>

      <nav className="mc-sidebar-nav" aria-label="工作区导航" style={{ display: "grid", gap: 2, padding: "8px 2px 12px" }}>
        <SidebarAction icon={<SquarePen />} label="新建任务" onClick={startSession} />
        {globalSearchEnabled && <SidebarAction icon={<Search />} label="搜索" onClick={() => navigate(() => toggleCommandPalette())} />}
        <SidebarAction icon={<FolderOpen />} label={workingDirectory ? "切换项目" : "打开项目"} onClick={() => navigate(() => void openWorkspaceFolder())} />
        <SidebarAction icon={<Clock3 />} label="已安排" onClick={openAutomationsPanel} />
        <SidebarAction icon={<Blend />} label="技能" onClick={() => navigate(() => toggleSkillsMarketplace())} />
        <SidebarAction
          icon={tab === "files" ? <MessageSquareText /> : <Files />}
          label={tab === "files" ? "返回会话" : "项目文件"}
          active={tab === "files"}
          onClick={() => switchSidebarTab(tab === "files" ? "conversations" : "files")}
        />
      </nav>

      {tab === "conversations" && (
        <ConversationsTab
          conversationId={conversationId ?? ""}
          onNavigate={onNavigate}
          onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
        />
      )}

      {tab === "files" && <FileTree onNavigate={onNavigate} />}

      <div className="mc-sidebar-footer">
        <div className="mc-sidebar-footer-row">
          <SidebarAction icon={<Settings />} label="设置" onClick={() => navigate(() => toggleSettings())} />
          <button
            type="button"
            className="btn-ghost mc-sidebar-theme-toggle"
            aria-label={nextThemeLabel}
            title={nextThemeLabel}
            onClick={() => setThemeMode(isDarkTheme ? "light" : "dark")}
          >
            {isDarkTheme ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
          </button>
        </div>
      </div>

      {isOpen && !embedded && (
        <div
          className="mc-sidebar-left-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整左侧栏宽度"
          aria-valuemin={LEFT_SIDEBAR_MIN_WIDTH}
          aria-valuemax={LEFT_SIDEBAR_MAX_WIDTH}
           aria-valuenow={Math.round(leftSidebarWidth)}
           aria-valuetext={`${Math.round(leftSidebarWidth)} pixels`}
           tabIndex={0}
          title="拖动调整左侧栏宽度，双击恢复默认"
          onPointerDown={startResize}
           onDoubleClick={resetSidebarWidth}
           onKeyDown={handleResizeKeyDown}
          style={leftResizeHandleStyle}
        />
      )}

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

const leftResizeHandleStyle: CSSProperties = {
  position: "absolute",
  top: 0,
  right: -3,
  bottom: 0,
  width: 7,
  cursor: "col-resize",
  zIndex: 2,
};
