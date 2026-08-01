import { useRef } from "react";
import { Clock3, GitBranch, LoaderCircle, MoreHorizontal } from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import type { ConversationMeta } from "../stores/types";
import { IconAction, ConversationMenu } from "./sidebarComponents";
import {
  sessionRowStyle,
  sessionMainButtonStyle,
  sessionCheckboxStyle,
  sessionTitleStyle,
  renameInputStyle,
} from "./sidebarStyles";

export const SessionRow = ({
  conversation,
  conversationId,
  selectionMode,
  selectedSessionIds,
  menuFor,
  renaming,
  renameValue,
  waitingLabelForConversation,
  onSwitch,
  onToggleSelected,
  onSetMenuFor,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onSetRenameValue,
  onArchive,
  onClone,
  onMerge,
  onExport,
  onDelete,
  onCleanup,
  onHandoff,
  onReveal,
  onCopy,
  treeDepth = 0,
}: {
  conversation: ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" };
  conversationId: string;
  selectionMode: boolean;
  selectedSessionIds: Set<string>;
  menuFor: string | null;
  renaming: string | null;
  renameValue: string;
  waitingLabelForConversation: (id: string) => string | null;
  onSwitch: (id: string) => void;
  onToggleSelected: (id: string) => void;
  onSetMenuFor: (id: string | null) => void;
  onStartRename: (id: string, title: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
  onSetRenameValue: (value: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onClone: (id: string) => void;
  onMerge: (id: string) => void;
  onExport: (id: string) => void;
  onDelete: (id: string) => void;
  onCleanup: (id: string) => void;
  onHandoff: (id: string, target: "local" | "worktree") => void;
  onReveal: (path?: string) => void;
  onCopy: (path?: string) => void;
  treeDepth?: number;
}) => {
  const c = conversation;
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const menuId = `conversation-actions-${c.id}`;
  return (
    <div
      className="session-row-hover"
      data-session-row="true"
      data-active={c.id === conversationId || undefined}
      style={{
        ...sessionRowStyle,
        paddingLeft: 40 + Math.min(treeDepth, 6) * 16,
        borderColor: "transparent",
        background: c.id === conversationId ? "var(--surface-active)" : "transparent",
        opacity: c.archived ? 0.6 : 1,
      }}
    >
      {selectionMode && (
        <input
          type="checkbox"
          checked={selectedSessionIds.has(c.id)}
          disabled={c.sessionStatus === "running" || c.sessionStatus === "waiting"}
          onChange={() => onToggleSelected(c.id)}
          onClick={(e) => e.stopPropagation()}
          style={sessionCheckboxStyle}
          aria-label={`Select ${c.title}`}
        />
      )}
      <button
        type="button"
        aria-current={c.id === conversationId ? "page" : undefined}
        onClick={(e) => {
          if (selectionMode) { onToggleSelected(c.id); return; }
          if (c.archived) return;
          if (e.ctrlKey || e.metaKey) {
            const id = `chat-${c.id}`;
            const state = useAppStore.getState();
            if (!state.panelSlots.some((p: { id: string }) => p.id === id)) {
              state.addPanel({ id, kind: "chat" });
            }
          } else {
            onSwitch(c.id);
          }
        }}
        style={sessionMainButtonStyle}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          {renaming === c.id ? (
            <input
              autoFocus
              value={renameValue}
              onChange={(e) => onSetRenameValue(e.target.value)}
              onBlur={onCommitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") onCommitRename();
                if (e.key === "Escape") onCancelRename();
              }}
              onClick={(e) => e.stopPropagation()}
              style={renameInputStyle}
            />
          ) : (
            <div
              onDoubleClick={(e) => { e.stopPropagation(); onStartRename(c.id, c.title); }}
              style={{ ...sessionTitleStyle, fontWeight: c.id === conversationId ? 550 : 430 }}
            >
              {c.title}
              {c.branchKind === "context_fork" && (
                <span
                  title="上下文分支"
                  aria-label="上下文分支"
                  style={{ display: "inline-flex", marginLeft: 6, color: "var(--text-muted)", verticalAlign: "-2px" }}
                >
                  <GitBranch size={14} />
                </span>
              )}
              {c.branchKind === "clone" && (
                <span title="会话副本" aria-label="会话副本" style={{ marginLeft: 6, color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>
                  副本
                </span>
              )}
              {c.mergedIntoConversationId && (
                <span title="已合并" aria-label="已合并" style={{ marginLeft: 6, color: "var(--state-success)", fontSize: "var(--text-3xs)" }}>
                  已合并
                </span>
              )}
            </div>
          )}
        </div>
      </button>

      {c.sessionStatus === "running" && (
        <LoaderCircle size={14} className="session-status-spinner mc-session-status-icon" aria-label="任务运行中" />
      )}
      {c.sessionStatus === "waiting" && (
        <>
          <Clock3 size={14} className="mc-session-status-icon" aria-label={waitingLabelForConversation(c.id) || "任务等待中"} />
          {waitingLabelForConversation(c.id) && <span className="sr-only">{waitingLabelForConversation(c.id)}</span>}
        </>
      )}

      <span className="session-row-actions" data-open={menuFor === c.id || undefined}>
        <IconAction
          label="会话操作"
          buttonRef={menuButtonRef}
          expanded={menuFor === c.id}
          controls={menuFor === c.id ? menuId : undefined}
          onClick={(e) => { e.stopPropagation(); onSetMenuFor(menuFor === c.id ? null : c.id); }}
        >
          <MoreHorizontal size={14} />
        </IconAction>
      </span>
      {menuFor === c.id && (
        <ConversationMenu
          anchor={menuButtonRef.current}
          menuId={menuId}
          archived={Boolean(c.archived)}
          isIsolated={Boolean(c.gitIsolated)}
          canDelete
          canReveal={Boolean((c.worktreePath || c.workspaceRoot) && isDesktop())}
          canCopy={Boolean(c.worktreePath || c.workspaceRoot)}
          canSwitch={!c.archived}
          canMerge={Boolean(c.parentConversationId && !c.mergedIntoConversationId && !c.archived)}
          onSwitch={() => onSwitch(c.id)}
          onClone={() => { onSetMenuFor(null); onClone(c.id); }}
          onMerge={() => { onSetMenuFor(null); onMerge(c.id); }}
          onExport={() => { onSetMenuFor(null); onExport(c.id); }}
          onReveal={() => onReveal(c.worktreePath || c.workspaceRoot)}
          onCopy={() => onCopy(c.worktreePath || c.workspaceRoot)}
          onCleanup={() => { onSetMenuFor(null); onCleanup(c.id); }}
          onHandoff={() => { onSetMenuFor(null); onHandoff(c.id, c.gitIsolated ? "local" : "worktree"); }}
          onArchive={() => { onSetMenuFor(null); onArchive(c.id, !c.archived); }}
          onDelete={() => { onSetMenuFor(null); onDelete(c.id); }}
        />
      )}
    </div>
  );
};
