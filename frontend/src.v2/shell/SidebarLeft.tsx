import { useEffect, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { Boxes, CalendarClock, Code2, FolderOpen, Plus, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_MAX_WIDTH, LEFT_SIDEBAR_MIN_WIDTH } from "../stores/shared-helpers";
import { FileTree } from "./FileTree";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { modeSwitchStyle, modeSwitchButtonStyle } from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";

type SidebarTab = "conversations" | "files";

export const SidebarLeft = () => {
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
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const [tab, setTab] = useState<SidebarTab>(appMode === "cowork" ? "conversations" : "files");
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const codeMode = tab === "files";
  const isOpen = leftSidebarWidth > 0;

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
  };
  const startSession = (mode: "cowork" | "code") => {
    createConversation({ appMode: mode, bindWorkspace: Boolean(workingDirectory) });
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

  const sidebarWidth = isOpen ? `${leftSidebarWidth}px` : 0;

  return (
    <aside
      className="mc-sidebar-left anim-slide-left sidebar-animate flex flex-col overflow-hidden box-border"
      data-open={isOpen ? "true" : "false"}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: "14px 10px 10px",
        boxSizing: "border-box",
        borderRight: "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
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
            {t === "conversations" ? <SlidersHorizontal size={14} /> : <Code2 size={14} />}
            {t === "conversations" ? "Cowork" : "Code"}
            {t === "conversations" && runningCount > 0 && (
              <span style={{
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
        <nav aria-label="Workspace navigation" style={{ display: "grid", gap: 3, padding: "10px 4px 12px" }}>
          <SidebarAction icon={<Plus size={15} />} label="New task" onClick={() => startSession("cowork")} />
          <SidebarAction icon={<FolderOpen size={15} />} label="Projects" onClick={() => void openWorkspaceFolder()} />
          <SidebarAction
            icon={<CalendarClock size={15} />}
            label="Scheduled"
            onClick={() => {
              window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "scheduler" }));
              toggleSettings();
            }}
          />
          <SidebarAction icon={<Boxes size={15} />} label="Live artifacts" onClick={() => toggleLiveArtifacts()} />
          <SidebarAction icon={<Sparkles size={15} />} label="Customize" onClick={() => toggleSkillsMarketplace()} />
        </nav>
      )}

      {codeMode && (
        <nav aria-label="Code navigation" style={{ display: "grid", gap: 3, padding: "10px 4px 12px" }}>
          <SidebarAction icon={<Plus size={15} />} label="New session" onClick={() => startSession("code")} />
          <SidebarAction icon={<Search size={15} />} label="Search" onClick={() => toggleCommandPalette()} />
          <SidebarAction icon={<FolderOpen size={15} />} label={workingDirectory ? "Switch folder" : "Open folder"} onClick={() => void openWorkspaceFolder()} />
        </nav>
      )}

      {tab === "conversations" && (
        <ConversationsTab
          conversationId={conversationId ?? ""}
          onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
        />
      )}

      {tab === "files" && <FileTree />}

      {isOpen && (
        <div
          className="mc-sidebar-left-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize left sidebar"
          aria-valuemin={LEFT_SIDEBAR_MIN_WIDTH}
          aria-valuemax={LEFT_SIDEBAR_MAX_WIDTH}
          aria-valuenow={Math.round(leftSidebarWidth)}
          title="Drag to resize left sidebar; double-click to reset"
          onPointerDown={startResize}
          onDoubleClick={resetSidebarWidth}
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
    className="btn-ghost"
    onClick={onClick}
    style={{
      width: "100%",
      minHeight: 32,
      display: "flex",
      alignItems: "center",
      gap: 10,
      border: 0,
      borderRadius: "var(--radius-sm, 7px)",
      color: "var(--text-secondary)",
      cursor: "pointer",
      padding: "5px 9px",
      fontFamily: "var(--font-ui)",
      fontSize: "var(--text-sm)",
      textAlign: "left",
    }}
  >
    <span style={{ display: "inline-flex", color: "var(--text-muted)" }}>{icon}</span>
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
