import { memo, useRef } from "react";
import { Circle, Clock3, GitBranch, LoaderCircle, MoreHorizontal } from "lucide-react";
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

export type SessionRowProps = {
  conversation: ConversationMeta;
  sessionStatus: "running" | "waiting" | "idle";
  isHydrating: boolean;
  active: boolean;
  deleting?: boolean;
  selectionMode: boolean;
  selected: boolean;
  menuOpen: boolean;
  renaming: boolean;
  renameValue: string;
  waitingLabel: string | null;
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
  onCleanup: (id: string) => void;
  onHandoff: (id: string, target: "local" | "worktree") => void;
  onReveal: (path?: string) => void;
  onCopy: (path?: string) => void;
  treeDepth?: number;
};

const SessionRowComponent = ({
  conversation,
  sessionStatus,
  isHydrating,
  active,
  deleting,
  selectionMode,
  selected,
  menuOpen,
  renaming,
  renameValue,
  waitingLabel,
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
  onCleanup,
  onHandoff,
  onReveal,
  onCopy,
  treeDepth = 0,
}: SessionRowProps) => {
  const c = conversation;
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const menuId = `conversation-actions-${c.id}`;
  return (
    <div
      className="session-row-hover"
      data-session-row="true"
      data-active={active || undefined}
      style={{
        ...sessionRowStyle,
        paddingLeft: 40 + Math.min(treeDepth, 6) * 16,
        borderColor: "transparent",
        background: active ? "var(--surface-active)" : "transparent",
        opacity: c.archived || deleting ? 0.6 : 1,
      }}
    >
      {selectionMode && (
        <input
          type="checkbox"
          checked={selected}
          disabled={deleting || sessionStatus === "running" || sessionStatus === "waiting"}
          onChange={() => onToggleSelected(c.id)}
          onClick={(e) => e.stopPropagation()}
          style={sessionCheckboxStyle}
          aria-label={`Select ${c.title}`}
        />
      )}
      {renaming ? (
        <div style={sessionMainButtonStyle}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <input
              autoFocus
              value={renameValue}
              aria-label={`重命名 ${c.title}`}
              onChange={(e) => onSetRenameValue(e.target.value)}
              onBlur={onCommitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  e.stopPropagation();
                  onCommitRename();
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation();
                  onCancelRename();
                }
              }}
              style={renameInputStyle}
            />
          </div>
        </div>
      ) : (
        <button
          type="button"
          aria-current={active ? "page" : undefined}
          disabled={c.archived || deleting}
          onClick={(e) => {
            if (selectionMode) { onToggleSelected(c.id); return; }
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
            <div
              onDoubleClick={(e) => { e.stopPropagation(); onStartRename(c.id, c.title); }}
              style={{ ...sessionTitleStyle, fontWeight: active ? 550 : 430 }}
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
          </div>
        </button>
      )}

      {!deleting && sessionStatus === "running" && !active && (
        <LoaderCircle
          size={14}
          className="session-status-spinner mc-session-status-icon"
          aria-label={isHydrating ? "正在恢复会话上下文" : "任务运行中"}
        />
      )}
      {!deleting && sessionStatus === "running" && active && (
        <Circle size={8} fill="currentColor" className="mc-session-status-icon mc-session-status-active" aria-label={isHydrating ? "正在恢复会话上下文" : "任务运行中"} />
      )}
      {!deleting && sessionStatus === "waiting" && (
        <>
          <Clock3 size={14} className="mc-session-status-icon" aria-label={waitingLabel || "任务等待中"} />
          {waitingLabel && <span className="sr-only">{waitingLabel}</span>}
        </>
      )}

      <span className="session-row-actions" data-open={menuOpen || undefined}>
        <IconAction
          label="会话操作"
          buttonRef={menuButtonRef}
          expanded={menuOpen}
          controls={menuOpen ? menuId : undefined}
          disabled={deleting}
          onClick={(e) => { e.stopPropagation(); onSetMenuFor(menuOpen ? null : c.id); }}
        >
          <MoreHorizontal size={14} />
        </IconAction>
      </span>
      {!deleting && menuOpen && (
        <ConversationMenu
          anchor={menuButtonRef.current}
          menuId={menuId}
          archived={Boolean(c.archived)}
          isIsolated={Boolean(c.gitIsolated)}
          canReveal={Boolean((c.worktreePath || c.workspaceRoot) && isDesktop())}
          canCopy={Boolean(c.worktreePath || c.workspaceRoot)}
          canMerge={Boolean(c.parentConversationId && !c.mergedIntoConversationId && !c.archived)}
          onRename={() => { onSetMenuFor(null); onStartRename(c.id, c.title); }}
          onClone={() => { onSetMenuFor(null); onClone(c.id); }}
          onMerge={() => { onSetMenuFor(null); onMerge(c.id); }}
          onExport={() => { onSetMenuFor(null); onExport(c.id); }}
          onReveal={() => onReveal(c.worktreePath || c.workspaceRoot)}
          onCopy={() => onCopy(c.worktreePath || c.workspaceRoot)}
          onCleanup={() => { onSetMenuFor(null); onCleanup(c.id); }}
          onHandoff={() => { onSetMenuFor(null); onHandoff(c.id, c.gitIsolated ? "local" : "worktree"); }}
          onArchive={() => { onSetMenuFor(null); onArchive(c.id, !c.archived); }}
          onClose={() => onSetMenuFor(null)}
        />
      )}
    </div>
  );
};

SessionRowComponent.displayName = "SessionRow";

export const SessionRow = memo(SessionRowComponent);
