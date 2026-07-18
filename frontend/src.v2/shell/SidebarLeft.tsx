import { useEffect, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { CalendarClock, Code2, FolderOpen, Layers, MessageSquareText, Plus, Search, WandSparkles } from "lucide-react";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_MAX_WIDTH, LEFT_SIDEBAR_MIN_WIDTH } from "../stores/shared-helpers";
import { FileTree } from "./FileTree";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { modeSwitchStyle, modeSwitchButtonStyle } from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { openAutomations } from "../lib/automations-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";

type SidebarTab = "conversations" | "files";

export const SidebarLeft = ({
  embedded = false,
  onNavigate,
}: {
  embedded?: boolean;
  onNavigate?: () => void;
}) => {
  const appMode = useAppStore((s) => s.appMode);
  const conversationId = useAppStore((s) => s.conversationId);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleLiveArtifacts = useAppStore((s) => s.toggleLiveArtifacts);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [tab, setTab] = useState<SidebarTab>(appMode === "cowork" ? "conversations" : "files");
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const codeMode = tab === "files";
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
    setTab(appMode === "cowork" ? "conversations" : "files");
  }, [appMode]);

  const switchSidebarTab = (nextTab: SidebarTab) => {
    setTab(nextTab);
    setAppMode(nextTab === "conversations" ? "cowork" : "code");
    onNavigate?.();
  };
  const startSession = (mode: "cowork" | "code") => {
    createConversation({ appMode: mode, bindWorkspace: Boolean(workingDirectory) });
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

  const resetSidebarWidth = () => setLeftSidebarWidth(320);
  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = leftSidebarWidth - step;
    else if (event.key === "ArrowRight") nextWidth = leftSidebarWidth + step;
    else if (event.key === "Home") nextWidth = LEFT_SIDEBAR_MIN_WIDTH;
    else if (event.key === "End") nextWidth = LEFT_SIDEBAR_MAX_WIDTH;
    else if (event.key === "Enter") nextWidth = 320;
    if (nextWidth == null) return;
    event.preventDefault();
    setLeftSidebarWidth(nextWidth);
  };
  const openAutomationsPanel = () => navigate(openAutomations);

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
        padding: "14px 10px 10px",
        boxSizing: "border-box",
        borderRight: embedded ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        width: sidebarWidth,
        minWidth: sidebarWidth,
        maxWidth: sidebarWidth,
        background: "color-mix(in oklch, var(--surface-page) 82%, var(--surface-base))",
        opacity: isOpen ? 1 : 0,
        pointerEvents: isOpen ? "auto" : "none",
      }}
    >
      {/* Tab bar */}
      <div role="tablist" aria-label="Mode switch" style={modeSwitchStyle}>
        {(["conversations", "files"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            className="mc-sidebar-mode-tab"
            aria-selected={tab === t}
            tabIndex={tab === t ? 0 : -1}
            onClick={() => switchSidebarTab(t)}
            style={{
              ...modeSwitchButtonStyle,
              background: tab === t ? "var(--surface-base)" : "transparent",
              borderColor: tab === t ? "var(--border-soft)" : "transparent",
              color: tab === t ? "var(--text-primary)" : "var(--text-muted)",
              fontWeight: tab === t ? 650 : 500,
            }}
          >
            <span className="mc-sidebar-mode-icon" aria-hidden="true">
              {t === "conversations" ? <MessageSquareText /> : <Code2 />}
            </span>
            {t === "conversations" ? "Cowork" : "Code"}
            {t === "conversations" && runningCount > 0 && (
              <span
                aria-label={`${runningCount} running tasks`}
                title={`${runningCount} running tasks`}
                style={{
                fontSize: 10,
                background: "var(--state-info)",
                color: "var(--text-on-accent)",
                borderRadius: 999,
                padding: "0 5px",
                fontWeight: 700,
                lineHeight: "16px",
                minWidth: 16,
                textAlign: "center",
              }}>
                {runningCount}
              </span>
            )}
          </button>
        ))}
      </div>

      {!codeMode && (
        <nav className="mc-sidebar-nav" aria-label="Workspace navigation" style={{ display: "grid", gap: 3, padding: "10px 4px 12px" }}>
          <SidebarAction icon={<Plus />} label="New task" onClick={() => startSession("cowork")} />
          <SidebarAction icon={<FolderOpen />} label="Projects" onClick={() => navigate(() => void openWorkspaceFolder())} />
          <SidebarAction
            icon={<CalendarClock />}
            label="Automations"
            onClick={openAutomationsPanel}
          />
          <SidebarAction icon={<Layers />} label="Live artifacts" onClick={() => navigate(() => toggleLiveArtifacts())} />
          <SidebarAction icon={<WandSparkles />} label="Customize" onClick={() => navigate(() => toggleSkillsMarketplace())} />
        </nav>
      )}

      {codeMode && (
        <nav className="mc-sidebar-nav" aria-label="Code navigation" style={{ display: "grid", gap: 3, padding: "10px 4px 12px" }}>
          <SidebarAction icon={<Plus />} label="New session" onClick={() => startSession("code")} />
          {globalSearchEnabled && <SidebarAction icon={<Search />} label="Search" onClick={() => navigate(() => toggleCommandPalette())} />}
          <SidebarAction icon={<FolderOpen />} label={workingDirectory ? "Switch folder" : "Open folder"} onClick={() => navigate(() => void openWorkspaceFolder())} />
        </nav>
      )}

      {tab === "conversations" && (
        <ConversationsTab
          conversationId={conversationId ?? ""}
          onNavigate={onNavigate}
          onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
        />
      )}

      {tab === "files" && <FileTree onNavigate={onNavigate} />}

      {isOpen && !embedded && (
        <div
          className="mc-sidebar-left-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize left sidebar"
          aria-valuemin={LEFT_SIDEBAR_MIN_WIDTH}
          aria-valuemax={LEFT_SIDEBAR_MAX_WIDTH}
           aria-valuenow={Math.round(leftSidebarWidth)}
           aria-valuetext={`${Math.round(leftSidebarWidth)} pixels`}
           tabIndex={0}
          title="Drag to resize left sidebar; double-click to reset"
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
  onClick,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    className="btn-ghost mc-sidebar-action"
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
