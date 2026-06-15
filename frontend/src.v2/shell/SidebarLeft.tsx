import { useEffect, useState } from "react";
import { CalendarClock, Code2, FolderOpen, Loader, Plus, Search, SlidersHorizontal, Sparkles } from "lucide-react";
import { useAppStore } from "../stores";
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
  const setAppMode = useAppStore((s) => s.setAppMode);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const [tab, setTab] = useState<SidebarTab>(appMode === "cowork" ? "conversations" : "files");
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const codeMode = tab === "files";

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

  return (
    <aside
      className="anim-slide-left sidebar-animate flex flex-col overflow-hidden box-border"
      style={{
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: "14px 10px 10px",
        boxSizing: "border-box",
        borderRight: "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        width: leftSidebarWidth > 0 ? "var(--sidebar-max-width)" : 0,
        minWidth: leftSidebarWidth > 0 ? "var(--sidebar-min-width)" : 0,
        maxWidth: leftSidebarWidth > 0 ? "var(--sidebar-max-width)" : 0,
        background: "color-mix(in oklch, var(--surface-page) 82%, var(--surface-base))",
        opacity: leftSidebarWidth > 0 ? 1 : 0,
        pointerEvents: leftSidebarWidth > 0 ? "auto" : "none",
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
          <SidebarAction icon={<Plus size={15} />} label="New task" onClick={() => createConversation()} />
          <SidebarAction icon={<Search size={15} />} label="Search" onClick={() => toggleCommandPalette()} />
          <SidebarAction icon={<FolderOpen size={15} />} label="Open folder" onClick={() => void openWorkspaceFolder()} />
          <SidebarAction
            icon={<CalendarClock size={15} />}
            label="Automations"
            onClick={() => {
              window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "scheduler" }));
              toggleSettings();
            }}
          />
          <SidebarAction icon={<Sparkles size={15} />} label="Customize" onClick={() => toggleSkillsMarketplace()} />
        </nav>
      )}

      {tab === "conversations" && (
        <ConversationsTab
          conversationId={conversationId ?? ""}
          onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
        />
      )}

      {tab === "files" && <FileTree />}

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
  icon: React.ReactNode;
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
