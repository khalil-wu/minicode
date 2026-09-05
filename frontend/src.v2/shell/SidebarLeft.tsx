import { useState } from "react";
import type { ReactNode } from "react";
import { Clock3, Code2, FolderOpen, MessageSquareText, Moon, Puzzle, Search, Settings, SquarePen, Sun } from "lucide-react";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_MIN_WIDTH } from "../stores/shared-helpers";
import { FileTree } from "./FileTree";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { Tip } from "../components/Tooltip";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openAutomations } from "../lib/automations-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { modeSwitchButtonStyle, modeSwitchLabelStyle, modeSwitchStyle } from "./sidebarStyles";

export const SidebarLeft = ({
  embedded = false,
  onNavigate,
}: {
  embedded?: boolean;
  onNavigate?: () => void;
}) => {
  const appMode = useAppStore((s) => s.appMode);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const conversationId = useAppStore((s) => s.conversationId);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const isOpen = embedded || leftSidebarWidth > 0;

  const switchAppMode = (nextMode: "cowork" | "code") => {
    setAppMode(nextMode);
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

  const openAutomationsPanel = () => navigate(openAutomations);
  const isDarkTheme = resolvedTheme === "dark";
  const nextThemeLabel = isDarkTheme ? "切换到浅色模式" : "切换到深色模式";

  // Fixed at the minimum width — the sidebar is no longer user-resizable;
  // the stored width only carries open (0 vs >0) state.
  const sidebarWidth = embedded ? "100%" : isOpen ? `${LEFT_SIDEBAR_MIN_WIDTH}px` : 0;

  return (
    <aside
      className="mc-sidebar-left anim-slide-left sidebar-animate flex flex-col"
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
        background: "var(--surface-sidebar)",
        opacity: isOpen ? 1 : 0,
        pointerEvents: isOpen ? "auto" : "none",
      }}
    >
      <div role="tablist" aria-label="工作模式" data-testid="sidebar-mode-switch" style={{ ...modeSwitchStyle, margin: "2px 0" }}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const mode = event.key === "Home" ? "cowork" : event.key === "End" ? "code" : appMode === "code" ? "cowork" : "code";
          setAppMode(mode);
          event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[mode === "code" ? 1 : 0].focus();
        }}>
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
          <span style={modeSwitchLabelStyle}>协作</span>
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
          <span style={modeSwitchLabelStyle}>代码</span>
        </button>
      </div>

      <nav className="mc-sidebar-nav" aria-label="工作区导航" style={{ display: "grid", gap: 2, padding: "8px 2px 12px" }}>
        <div className="mc-sidebar-primary-actions">
          <SidebarAction icon={<SquarePen />} label="新建任务" onClick={startSession} />
          {globalSearchEnabled && <button type="button" className="btn-ghost mc-icon-button" aria-label="搜索" title="搜索任务与命令" onClick={() => navigate(() => toggleCommandPalette())}><Search size={16} /></button>}
        </div>
        <SidebarAction icon={<Clock3 />} label="已安排" onClick={openAutomationsPanel} />
        <SidebarAction icon={<Puzzle />} label="技能" onClick={() => navigate(() => toggleSkillsMarketplace())} />
      </nav>

      <button type="button" className="mc-sidebar-project-open" onClick={() => navigate(() => void openWorkspaceFolder())} title={workingDirectory || "打开项目"}>
        <FolderOpen size={16} aria-hidden="true" /><span>{workingDirectory ? "切换项目" : "打开项目"}</span>
      </button>

      <div key={appMode} className="mc-sidebar-mode-content" data-mode={appMode}>
        {appMode === "cowork" ? (
          <ConversationsTab
            conversationId={conversationId ?? ""}
            onNavigate={onNavigate}
            onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
          />
        ) : (
          <FileTree onNavigate={onNavigate} />
        )}
      </div>

      <div className="mc-sidebar-footer">
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
      </div>

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
