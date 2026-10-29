import { useI18n } from "../i18n";
import { memo, useRef } from "react";
import type { Ref } from "react";
import type { DraggableAttributes } from "@dnd-kit/core";
import { Clock3, GitBranch, LoaderCircle, MoreHorizontal } from "lucide-react";
import type { ConversationMeta } from "../stores/types";
import { ThreadPullRequestBadge, type LinkedPullRequest } from "./ThreadPullRequestBadge";
import { IconAction, ConversationMenu, threadMenuIcon } from "./sidebarComponents";
import { canOpenThreadWindow, openThreadWindow, threadWindowModifier } from "./threadWindowActions";
import {
  sessionRowStyle,
  sessionMainButtonStyle,
  sessionTitleStyle,
  renameInputStyle,
} from "./sidebarStyles";

export type SessionRowProps = {
  conversation: ConversationMeta;
  rowId?: string;
  sessionStatus: "running" | "waiting" | "idle";
  active: boolean;
  deleting?: boolean;
  menuOpen: boolean;
  renaming: boolean;
  renameValue: string;
  waitingLabel: string | null;
  onSwitch: (id: string, rowId: string) => void;
  onSetMenuFor: (id: string | null) => void;
  onStartRename: (id: string, title: string, rowId?: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
  onSetRenameValue: (value: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onCopyLink: (id: string) => void;
  onCopyMarkdown: (id: string) => void;
  onHandoff: (id: string, target: "local" | "worktree") => void;
  onCopy: (path?: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onReadState: (id: string, hasUnreadTurn: boolean) => void;
  onProjectChange: (id: string, projectId: string) => void;
  currentProject: string;
  projectOptions: { id: string; name: string }[];
  sectionOptions?: { id: string; name: string }[];
  currentSection?: string;
  onSectionChange?: (id: string, sectionId: string | null) => void;
  onNewSection?: (id: string) => void;
  pullRequest?: LinkedPullRequest | null;
  treeDepth?: number;
  dragActivatorRef?: Ref<HTMLButtonElement>;
  dragAttributes?: DraggableAttributes;
};

const SessionRowComponent = ({
  conversation,
  rowId,
  sessionStatus,
  active,
  deleting,
  menuOpen,
  renaming,
  renameValue,
  waitingLabel,
  onSwitch,
  onSetMenuFor,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onSetRenameValue,
  onArchive,
  onCopyLink,
  onCopyMarkdown,
  onHandoff,
  onCopy,
  onPin, onReadState, onProjectChange, currentProject, projectOptions,
  sectionOptions, currentSection, onSectionChange, onNewSection, pullRequest,
  treeDepth = 0,
  dragActivatorRef, dragAttributes,
}: SessionRowProps) => {
  const { t: tr } = useI18n();
  const c = conversation;
  const rowRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const presentationId = rowId || c.id;
  const menuId = `conversation-actions-${presentationId}`;
  return (
    <div
      ref={rowRef}
      className="session-row-hover"
      data-session-row="true"
      data-session-presentation={presentationId}
      data-active={active || undefined}
      style={{
        ...sessionRowStyle,
        paddingLeft: `calc(var(--conversation-row-indent, 40px) + ${Math.min(treeDepth, 6) * 16}px)`,
        borderColor: "transparent",
        background: active ? "var(--surface-active)" : "transparent",
        opacity: c.archived || deleting ? 0.6 : 1,
      }}
    >
      {renaming ? (
        <div style={sessionMainButtonStyle}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <input
              autoFocus
              value={renameValue}
              aria-label={tr("重命名 {name}", { name: c.title })}
              onChange={(e) => onSetRenameValue(e.target.value)}
              onBlur={onCommitRename}
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                if (e.key === "Enter") {
                  e.preventDefault();
                  e.stopPropagation();
                  onCommitRename();
                  queueMicrotask(() => rowRef.current?.querySelector<HTMLButtonElement>(":scope > button")?.focus({ preventScroll: true }));
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation();
                  onCancelRename();
                  queueMicrotask(() => rowRef.current?.querySelector<HTMLButtonElement>(":scope > button")?.focus({ preventScroll: true }));
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
          ref={dragActivatorRef}
          {...dragAttributes}
          disabled={c.archived || deleting}
          onClick={event => {
            if (canOpenThreadWindow(c) && threadWindowModifier(event)) {
              event.preventDefault(); event.stopPropagation();
              void openThreadWindow(c.id);
            } else onSwitch(c.id, presentationId);
          }}
          style={sessionMainButtonStyle}
        >
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              onDoubleClick={(e) => { e.stopPropagation(); onStartRename(c.id, c.title, presentationId); }}
              style={{ ...sessionTitleStyle, fontWeight: 400 }}
            >
              {c.title}
              {c.branchKind === "context_fork" && (
                <span
                  title={tr("上下文分支")}
                  aria-label={tr("上下文分支")}
                  style={{ display: "inline-flex", marginLeft: 6, color: "var(--text-muted)", verticalAlign: "-2px" }}
                >
                  <GitBranch size={14} />
                </span>
              )}
              {c.branchKind === "clone" && (
                <span title={tr("会话副本")} aria-label={tr("会话副本")} style={{ marginLeft: 6, color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>
                  {tr("副本")}</span>
              )}
              {c.mergedIntoConversationId && (
                <span title={tr("已合并")} aria-label={tr("已合并")} style={{ marginLeft: 6, color: "var(--state-success)", fontSize: "var(--text-3xs)" }}>
                  {tr("已合并")}</span>
              )}
            </div>
          </div>
        </button>
      )}

      {!deleting && pullRequest && <ThreadPullRequestBadge pr={pullRequest} />}
      {!deleting && sessionStatus === "running" && (
        <LoaderCircle
          size={14}
          className="session-status-spinner mc-session-status-icon"
          aria-label={tr("任务运行中")}
        />
      )}
      {!deleting && sessionStatus === "waiting" && (
        <>
          <Clock3 size={14} className="mc-session-status-icon" aria-label={waitingLabel || tr("任务等待中")} />
          {waitingLabel && <span className="sr-only">{waitingLabel}</span>}
        </>
      )}

      {c.hasUnreadTurn && !active && !deleting && <span className="mc-session-unread-dot" aria-label={tr("未读")} />}
      {!c.hasUnreadTurn && !deleting && <span className="session-row-pin" data-pinned={c.pinned || undefined}>
        <IconAction hasPopup={false} label={tr(c.pinned ? "取消固定" : "固定")} onClick={event => { event.stopPropagation(); onPin(c.id, !c.pinned); }}>
          {threadMenuIcon(c.pinned ? "pin-row-filled" : "pin-row")}
        </IconAction>
      </span>}
      <span className="session-row-actions" data-open={menuOpen || undefined}>
        <IconAction
          label={tr("会话操作")}
          buttonRef={menuButtonRef}
          expanded={menuOpen}
          controls={menuOpen ? menuId : undefined}
          disabled={deleting}
          onClick={(e) => { e.stopPropagation(); onSetMenuFor(menuOpen ? null : presentationId); }}
        >
          <MoreHorizontal size={14} />
        </IconAction>
      </span>
      {!deleting && menuOpen && (
        <ConversationMenu
          anchor={menuButtonRef.current}
          menuId={menuId}
          conversationId={c.id}
          workspaceRoot={c.executionCwd || c.worktreePath || c.workspaceRoot}
          conversationType={c.conversationType}
          environment={c.environment}
          archived={Boolean(c.archived)}
          isIsolated={Boolean(c.gitIsolated)}
          canCopy={Boolean(c.executionCwd || c.worktreePath || c.workspaceRoot)}
          canHandoff={Boolean(c.gitBranch || c.gitIsolated || c.worktreePath)}
          pinned={c.pinned} hasUnreadTurn={c.hasUnreadTurn} currentProject={currentProject} projectOptions={projectOptions}
          onPin={() => onPin(c.id, !c.pinned)}
          onReadState={() => onReadState(c.id, !c.hasUnreadTurn)}
          onProjectChange={projectId => onProjectChange(c.id, projectId)}
          sectionOptions={sectionOptions} currentSection={currentSection}
          onSectionChange={sectionId => onSectionChange?.(c.id, sectionId)} onNewSection={onNewSection ? () => onNewSection(c.id) : undefined}
          onRename={() => { onSetMenuFor(null); onStartRename(c.id, c.title, presentationId); }}
          onCopyLink={() => onCopyLink(c.id)}
          onCopyMarkdown={() => onCopyMarkdown(c.id)}
          onCopy={() => onCopy(c.executionCwd || c.worktreePath || c.workspaceRoot)}
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
