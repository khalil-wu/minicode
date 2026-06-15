import { GitBranch, MoreHorizontal } from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import { branchDisplayName } from "../lib/workspace-display";
import type { ConversationMeta } from "../stores/types";
import { IconAction, ConversationMenu } from "./sidebarComponents";
import {
  sessionRowStyle,
  sessionMainButtonStyle,
  sessionCheckboxStyle,
  sessionTitleStyle,
  sessionMetaLineStyle,
  branchMetaStyle,
  waitingReasonMetaStyle,
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
  statusDot,
  relativeTime,
  onSwitch,
  onToggleSelected,
  onSetMenuFor,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onSetRenameValue,
  onArchive,
  onDelete,
  onCleanup,
  onReveal,
  onCopy,
}: {
  conversation: ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" };
  conversationId: string;
  selectionMode: boolean;
  selectedSessionIds: Set<string>;
  menuFor: string | null;
  renaming: string | null;
  renameValue: string;
  waitingLabelForConversation: (id: string) => string | null;
  statusDot: (status: string) => React.ReactNode;
  relativeTime: (iso: string) => string;
  onSwitch: (id: string) => void;
  onToggleSelected: (id: string) => void;
  onSetMenuFor: (id: string | null) => void;
  onStartRename: (id: string, title: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
  onSetRenameValue: (value: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onDelete: (id: string) => void;
  onCleanup: (id: string) => void;
  onReveal: (path?: string) => void;
  onCopy: (path?: string) => void;
}) => {
  const c = conversation;
  return (
    <div
      className="session-row-hover"
      data-active={c.id === conversationId || undefined}
      style={{
        ...sessionRowStyle,
        borderColor: c.id === conversationId ? "var(--border-subtle)" : "transparent",
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
        {statusDot(c.sessionStatus || "idle")}
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
              style={{ ...sessionTitleStyle, fontWeight: c.id === conversationId ? 500 : 400 }}
            >
              {c.title}
            </div>
          )}
          <div style={sessionMetaLineStyle}>
            <span>{relativeTime(c.updatedAt)}</span>
            {c.sessionStatus === "waiting" && waitingLabelForConversation(c.id) && (
              <>
                <span style={{ opacity: 0.4 }}>/</span>
                <span title={waitingLabelForConversation(c.id) ?? undefined} style={waitingReasonMetaStyle}>
                  {waitingLabelForConversation(c.id)}
                </span>
              </>
            )}
            {branchDisplayName(c.gitBranch) && (
              <>
                <span style={{ opacity: 0.4 }}>/</span>
                <span
                  title={c.worktreePath || c.gitBranch}
                  style={{
                    ...branchMetaStyle,
                    color: c.gitIsolated ? "var(--accent-primary)" : "var(--text-muted)",
                  }}
                >
                  {c.gitIsolated && <GitBranch size={10} />}
                  {branchDisplayName(c.gitBranch)}
                </span>
              </>
            )}
          </div>
        </div>
      </button>

      <IconAction
        label="Session actions"
        onClick={(e) => { e.stopPropagation(); onSetMenuFor(menuFor === c.id ? null : c.id); }}
      >
        <MoreHorizontal size={14} />
      </IconAction>
      {menuFor === c.id && (
        <ConversationMenu
          archived={Boolean(c.archived)}
          isIsolated={Boolean(c.gitIsolated)}
          canDelete
          canReveal={Boolean((c.worktreePath || c.workspaceRoot) && isDesktop())}
          canCopy={Boolean(c.worktreePath || c.workspaceRoot)}
          onSwitch={() => onSwitch(c.id)}
          onReveal={() => onReveal(c.worktreePath || c.workspaceRoot)}
          onCopy={() => onCopy(c.worktreePath || c.workspaceRoot)}
          onCleanup={() => { onSetMenuFor(null); onCleanup(c.id); }}
          onArchive={() => { onSetMenuFor(null); onArchive(c.id, !c.archived); }}
          onDelete={() => { onSetMenuFor(null); onDelete(c.id); }}
        />
      )}
    </div>
  );
};
