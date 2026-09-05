import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  SquarePen,
} from "lucide-react";
import { useAppStore } from "../stores";
import { EmptyState } from "../components/EmptyState";
import { isDesktop, revealPath } from "../desktop/runtime";
import type { ConversationMeta } from "../stores/types";
import * as wsOutbox from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { runtimePendingUserActionLabelForConversation } from "../lib/runtime-session";
import { SessionRow } from "./SessionRow";
import {
  sectionHeaderRowStyle,
  sessionListWrapStyle,
  projectCountStyle,
  projectItemsStyle,
} from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";
import { readableToolLabel } from "../chat/toolDisplayName";
import { pushToast } from "../overlays/ToastContainer";
import { safeJsonParse } from "../lib/safe-parse";

const CONVERSATION_UI_STATE_KEY = "minicode.sidebar.conversations.state";
const CONVERSATION_UI_PERSIST_DELAY_MS = 140;

type ConversationUiState = {
  collapsedGroups: Set<string>;
  scrollTop: number;
};

const readConversationUiState = () => {
  try {
    const parsed = safeJsonParse<unknown>(localStorage.getItem(CONVERSATION_UI_STATE_KEY) || "{}", {});
    const value = parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as { collapsedGroups?: unknown; scrollTop?: unknown }
      : {};
    return {
      collapsedGroups: new Set<string>(Array.isArray(value.collapsedGroups) ? value.collapsedGroups : []),
      scrollTop: typeof value.scrollTop === "number" ? value.scrollTop : 0,
    };
  } catch {
    return { collapsedGroups: new Set<string>(), scrollTop: 0 };
  }
};

export type EnrichedConversation = ConversationMeta & {
  sessionStatus: "running" | "waiting" | "idle";
  isHydrating: boolean;
  waitingLabel: string | null;
};

export interface WorkspaceConversationGroup {
  baseLabel: string;
  label: string;
  items: EnrichedConversation[];
}

export interface TreeConversation extends EnrichedConversation {
  treeDepth: number;
}

/** Stable parent-first ordering used by both workspace and ordinary session lists. */
export const orderConversationTree = (items: EnrichedConversation[]): TreeConversation[] => {
  const byId = new Map(items.map((item) => [item.id, item]));
  const children = new Map<string, EnrichedConversation[]>();
  for (const item of items) {
    const parent = item.parentConversationId;
    if (!parent || !byId.has(parent)) continue;
    const siblings = children.get(parent) ?? [];
    siblings.push(item);
    children.set(parent, siblings);
  }
  const ordered: TreeConversation[] = [];
  const visited = new Set<string>();
  const visit = (item: EnrichedConversation, depth: number) => {
    if (visited.has(item.id)) return;
    visited.add(item.id);
    ordered.push({ ...item, treeDepth: depth });
    for (const child of children.get(item.id) ?? []) visit(child, Math.min(depth + 1, 6));
  };
  for (const item of items) {
    if (!item.parentConversationId || !byId.has(item.parentConversationId)) visit(item, 0);
  }
  // Cyclic or otherwise malformed metadata must remain visible, never vanish.
  for (const item of items) visit(item, 0);
  return ordered;
};

const conversationWorkspacePath = (conversation: Pick<ConversationMeta, "workspaceRoot" | "worktreePath">): string => {
  const worktree = conversation.worktreePath?.trim();
  if (worktree) return worktree;
  return conversation.workspaceRoot?.trim() || "";
};

export const isWorkspaceConversation = (conversation: Pick<ConversationMeta, "workspaceRoot" | "worktreePath">): boolean =>
  Boolean(conversationWorkspacePath(conversation));

const workspaceGroupIdentity = (path: string | null | undefined): string => {
  const canonical = canonicalWorkspacePath(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (!canonical) return "__computer__";
  const windowsStyle = /^[A-Za-z]:\//.test(canonical) || canonical.startsWith("//");
  return windowsStyle ? canonical.toLowerCase() : canonical;
};

const workspacePathParts = (path: string | null | undefined): string[] => {
  const canonical = canonicalWorkspacePath(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
  return canonical.split("/").filter(Boolean);
};

export function groupByWorkspace(conversations: (ConversationMeta & { sessionStatus: string })[]): Map<string, WorkspaceConversationGroup> {
  const groups = new Map<string, WorkspaceConversationGroup>();
  for (const c of conversations) {
    const basePath = conversationWorkspacePath(c);
    const key = workspaceGroupIdentity(basePath);
    if (!groups.has(key)) {
      const label = workspaceDisplayName(basePath, "Computer");
      groups.set(key, { baseLabel: label, label, items: [] });
    }
    groups.get(key)!.items.push(c as EnrichedConversation);
  }

  const labelCounts = new Map<string, number>();
  for (const group of groups.values()) {
    labelCounts.set(group.baseLabel, (labelCounts.get(group.baseLabel) ?? 0) + 1);
  }
  for (const [key, group] of groups) {
    if ((labelCounts.get(group.baseLabel) ?? 0) < 2) continue;
    const duplicateGroups = Array.from(groups.entries()).filter(([, candidate]) => candidate.baseLabel === group.baseLabel);
    const first = group.items[0];
    const parts = workspacePathParts(first ? conversationWorkspacePath(first) : "");
    let qualifier = key;
    for (let depth = 1; depth < parts.length; depth += 1) {
      const candidateQualifier = parts.slice(Math.max(0, parts.length - 1 - depth), -1).join("/");
      const isUnique = duplicateGroups.every(([candidateKey, candidate]) => {
        if (candidateKey === key) return true;
        const candidateFirst = candidate.items[0];
        const candidateParts = workspacePathParts(candidateFirst ? conversationWorkspacePath(candidateFirst) : "");
        const otherQualifier = candidateParts.slice(Math.max(0, candidateParts.length - 1 - depth), -1).join("/");
        return otherQualifier !== candidateQualifier;
      });
      if (isUnique) {
        qualifier = candidateQualifier;
        break;
      }
    }
    group.label = `${group.baseLabel} — ${qualifier}`;
  }
  return groups;
}

export const ConversationsTab = ({
  conversationId,
  onNavigate,
  onSetConfirmDialog,
}: {
  conversationId: string;
  onNavigate?: () => void;
  onSetConfirmDialog: (dialog: { title: string; message: string; confirmLabel: string; danger?: boolean; onConfirm: () => void }) => void;
}) => {
  const conversations = useAppStore((s) => s.conversations);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationStreaming = useAppStore((s) => s.conversationStreaming);
  const conversationHydration = useAppStore((s) => s.conversationHydration);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const diffReviewQueue = useAppStore((s) => s.diffReviewQueue);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const askUserQueue = useAppStore((s) => s.askUserQueue);
  const runtimeSession = useAppStore((s) => s.runtimeSession);
  const appMode = useAppStore((s) => s.appMode);
  const createConversation = useAppStore((s) => s.createConversation);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const initialUiState = useMemo(readConversationUiState, []);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(initialUiState.collapsedGroups);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);

  const collapsedGroupsRef = useRef(collapsedGroups);
  const pendingUiStateRef = useRef<ConversationUiState>({
    collapsedGroups: initialUiState.collapsedGroups,
    scrollTop: initialUiState.scrollTop,
  });
  const persistUiStateTimerRef = useRef<number | null>(null);
  const pendingUiStateDirtyRef = useRef(false);
  collapsedGroupsRef.current = collapsedGroups;

  const persistPendingConversationUiState = useCallback(() => {
    if (!pendingUiStateDirtyRef.current) return;
    pendingUiStateDirtyRef.current = false;
    const snapshot = pendingUiStateRef.current;
    try {
      localStorage.setItem(CONVERSATION_UI_STATE_KEY, JSON.stringify({
        collapsedGroups: Array.from(snapshot.collapsedGroups),
        scrollTop: snapshot.scrollTop,
      }));
    } catch { /* Storage can be unavailable in hardened renderer contexts. */ }
  }, []);

  const scheduleConversationUiStatePersist = useCallback((
    nextCollapsedGroups: Set<string>,
    scrollTop: number,
  ) => {
    pendingUiStateRef.current = {
      collapsedGroups: nextCollapsedGroups,
      scrollTop,
    };
    pendingUiStateDirtyRef.current = true;
    if (persistUiStateTimerRef.current !== null) {
      window.clearTimeout(persistUiStateTimerRef.current);
    }
    persistUiStateTimerRef.current = window.setTimeout(() => {
      persistUiStateTimerRef.current = null;
      persistPendingConversationUiState();
    }, CONVERSATION_UI_PERSIST_DELAY_MS);
  }, [persistPendingConversationUiState]);

  const handleConversationListScroll = useCallback((event: React.UIEvent<HTMLDivElement>) => {
    scheduleConversationUiStatePersist(collapsedGroupsRef.current, event.currentTarget.scrollTop);
  }, [scheduleConversationUiStatePersist]);

  const waitingLabelsByConversation = useMemo(() => {
    const labels = new Map<string, string>();
    for (const prompt of [pendingAskUser, ...askUserQueue]) {
      const id = prompt?.conversationId?.trim();
      if (id && !labels.has(id)) labels.set(id, "等待回复");
    }
    for (const prompt of [pendingDiffReview, ...diffReviewQueue]) {
      const id = prompt?.conversationId?.trim();
      if (id && !labels.has(id)) labels.set(id, "等待审阅");
    }
    for (const prompt of [pendingApproval, ...approvalQueue]) {
      const id = prompt?.conversationId?.trim();
      if (id && !labels.has(id)) {
        labels.set(id, `等待批准 ${readableToolLabel(prompt?.toolName || "")}`);
      }
    }
    return labels;
  }, [pendingAskUser, askUserQueue, pendingDiffReview, diffReviewQueue, pendingApproval, approvalQueue]);

  const enrichedConversations = useMemo(() => {
    return conversations.map((conversation) => {
      const isHydrating = conversationHydration[conversation.id]?.isHydrating === true;
      const waitingLabel = waitingLabelsByConversation.get(conversation.id)
        ?? runtimePendingUserActionLabelForConversation(runtimeSession, conversation.id);
      let sessionStatus: EnrichedConversation["sessionStatus"] = conversation.sessionStatus || "idle";
      if (isHydrating || isConversationRunning({
        conversationId: conversation.id,
        activeConversationId: conversationId,
        activeIsStreaming: isStreaming,
        conversationStreaming,
      })) {
        sessionStatus = "running";
      } else if (waitingLabel) {
        sessionStatus = "waiting";
      }
      return {
        ...conversation,
        isHydrating,
        waitingLabel,
        sessionStatus,
      };
    });
  }, [conversations, conversationId, isStreaming, conversationStreaming, conversationHydration, waitingLabelsByConversation, runtimeSession]);

  const filtered = useMemo(() => enrichedConversations.filter((conversation) => !conversation.archived), [enrichedConversations]);

  const workspaceConversations = useMemo(() => filtered.filter(isWorkspaceConversation), [filtered]);
  const ordinaryConversations = useMemo(() => filtered.filter((conversation) => !isWorkspaceConversation(conversation)), [filtered]);
  const projectGroups = useMemo(() => groupByWorkspace(workspaceConversations), [workspaceConversations]);
  const orderedProjectGroups = useMemo(() => (
    Array.from(projectGroups.entries(), ([projectKey, group]) => ({
      projectKey,
      group,
      conversations: orderConversationTree(group.items),
    }))
  ), [projectGroups]);
  const orderedOrdinaryConversations = useMemo(
    () => orderConversationTree(ordinaryConversations),
    [ordinaryConversations],
  );
  const conversationsById = useMemo(
    () => new Map(conversations.map((conversation) => [conversation.id, conversation])),
    [conversations],
  );
  const conversationsRef = useRef(conversations);
  // Read the active id through a ref so switching chats does not recreate the
  // row renderer and force every session row to rerender.
  const conversationIdRef = useRef(conversationId);
  const renameStateRef = useRef({ renaming, renameValue });
  const onSetConfirmDialogRef = useRef(onSetConfirmDialog);
  const onNavigateRef = useRef(onNavigate);
  conversationsRef.current = conversations;
  conversationIdRef.current = conversationId;
  renameStateRef.current = { renaming, renameValue };
  onSetConfirmDialogRef.current = onSetConfirmDialog;
  onNavigateRef.current = onNavigate;

  useEffect(() => {
    if (!menuFor) return;
    const close = () => setMenuFor(null);
    const closeOnEsc = (e: KeyboardEvent) => { if (e.key === "Escape") { close(); } };
    window.addEventListener("click", close);
    window.addEventListener("keydown", closeOnEsc);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", closeOnEsc); };
  }, [menuFor]);

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = initialUiState.scrollTop;
  }, [initialUiState.scrollTop]);

  useEffect(() => () => {
    if (persistUiStateTimerRef.current !== null) {
      window.clearTimeout(persistUiStateTimerRef.current);
      persistUiStateTimerRef.current = null;
    }
    persistPendingConversationUiState();
  }, [persistPendingConversationUiState]);

  const cleanupWorktree = useCallback((id: string, force = false) => {
    const conversation = conversationsRef.current.find((candidate) => candidate.id === id);
    if (!conversation?.gitIsolated) return;
    onSetConfirmDialogRef.current({
      title: force ? "强制清理工作区" : "清理工作区",
      message: force
        ? "强制移除这个隔离会话工作区并丢弃本地更改吗？"
        : "移除这个隔离会话工作区吗？如果存在本地更改，MiniCode 会再询问是否强制清理。",
      confirmLabel: force ? "强制清理" : "清理",
      danger: force,
      onConfirm: () => { wsOutbox.sendClientCommand({ type: "conversation.worktree.cleanup", conversation_id: id, force }); },
    });
  }, []);

  const archiveConversation = useCallback(async (id: string, archived: boolean) => {
    try {
      const command = archived ? "conversation.archive" : "conversation.unarchive";
      const result = await wsOutbox.sendClientCommandAwaitResult({
        type: command,
        conversation_id: id,
        archived,
      }, command);
      if (!wsOutbox.commandResultSucceeded(result)) {
        pushToast(result.message || "Unable to update the conversation archive state.", "error", 6000);
      }
    } catch (error) {
      pushToast(
        error instanceof Error ? error.message : "Unable to update the conversation archive state.",
        "error",
        6000,
      );
    }
  }, []);

  const handleSwitch = useCallback((id: string) => {
    setMenuFor(null);
    requestConversationSwitch(id);
    onNavigateRef.current?.();
  }, [requestConversationSwitch]);

  const handoffWorktree = useCallback((id: string, target: "local" | "worktree") => {
    wsOutbox.sendClientCommand({
      type: "conversation.worktree.handoff.preflight",
      conversation_id: id,
      target,
    });
  }, []);
  const revealConversationPath = useCallback((path?: string) => {
    if (!path) return;
    setMenuFor(null);
    if (isDesktop()) void revealPath(path);
  }, []);
  const copyConversationPath = useCallback((path?: string) => {
    if (!path) return;
    setMenuFor(null);
    void navigator.clipboard?.writeText(path);
  }, []);
  const startRename = useCallback((id: string, currentTitle: string) => {
    renameStateRef.current = { renaming: id, renameValue: currentTitle };
    setRenaming(id);
    setRenameValue(currentTitle);
  }, []);
  const handleRenameValueChange = useCallback((value: string) => {
    renameStateRef.current = { ...renameStateRef.current, renameValue: value };
    setRenameValue(value);
  }, []);
  const cancelRename = useCallback(() => {
    renameStateRef.current = { ...renameStateRef.current, renaming: null };
    setRenaming(null);
  }, []);
  const commitRename = useCallback(() => {
    const current = renameStateRef.current;
    const title = current.renameValue.trim();
    if (!current.renaming || !title) {
      cancelRename();
      return;
    }
    const renamedConversationId = current.renaming;
    renameStateRef.current = { ...current, renaming: null };
    setRenaming(null);
    void wsOutbox.sendClientCommandAwaitResult({
      type: "conversation.rename",
      conversation_id: renamedConversationId,
      title,
    }, "conversation.rename").then((result) => {
      if (!wsOutbox.commandResultSucceeded(result)) {
        pushToast(result.message || "Unable to rename the conversation.", "error", 6000);
      }
    }).catch((error) => {
      pushToast(
        error instanceof Error ? error.message : "Unable to rename the conversation.",
        "error",
        6000,
      );
    });
  }, [cancelRename]);

  const toggleGroup = useCallback((key: string) => {
    const next = new Set(collapsedGroupsRef.current);
    next.has(key) ? next.delete(key) : next.add(key);
    collapsedGroupsRef.current = next;
    setCollapsedGroups(next);
    scheduleConversationUiStatePersist(next, listRef.current?.scrollTop ?? 0);
  }, [scheduleConversationUiStatePersist]);
  const cloneConversation = useCallback((id: string) => {
    wsOutbox.sendClientCommand({ type: "conversation.clone", conversation_id: id, activate: false });
  }, []);
  const mergeConversation = useCallback((id: string) => {
    const currentConversations = conversationsRef.current;
    const conversation = currentConversations.find((item) => item.id === id);
    const parent = conversation?.parentConversationId
      ? currentConversations.find((item) => item.id === conversation.parentConversationId)
      : undefined;
    if (!conversation || !parent) return;
    onSetConfirmDialogRef.current({
      title: "合并会话分支",
      message: `将“${conversation.title}”的新增消息快速合并到“${parent.title}”。如果父会话已分叉修改，后端会拒绝合并并保留两边内容。`,
      confirmLabel: "合并",
      onConfirm: () => wsOutbox.sendClientCommand({
        type: "conversation.merge",
        conversation_id: id,
        target_conversation_id: parent.id,
      }),
    });
  }, []);
  const exportConversation = useCallback((id: string) => {
    wsOutbox.sendClientCommand({ type: "conversation.export", conversation_id: id, include_descendants: true });
  }, []);

  const startWorkspaceConversation = (projectKey: string, group: WorkspaceConversationGroup) => {
    const workspaceRoot = conversationWorkspacePath(group.items[0]);
    if (!workspaceRoot) return;
    if (collapsedGroupsRef.current.has(projectKey)) {
      const next = new Set(collapsedGroupsRef.current);
      next.delete(projectKey);
      collapsedGroupsRef.current = next;
      setCollapsedGroups(next);
      scheduleConversationUiStatePersist(next, listRef.current?.scrollTop ?? 0);
    }
    createConversation({ bindWorkspace: true, workspaceRoot, appMode });
    onNavigate?.();
  };

  const renderSessionRow = useCallback((conversation: TreeConversation) => (
    <SessionRow
      key={conversation.id}
      conversation={conversationsById.get(conversation.id) ?? conversation}
      sessionStatus={conversation.sessionStatus}
      isHydrating={conversation.isHydrating}
      active={conversation.id === conversationIdRef.current}
      menuOpen={menuFor === conversation.id}
      renaming={renaming === conversation.id}
      renameValue={renaming === conversation.id ? renameValue : ""}
      waitingLabel={conversation.waitingLabel}
      onSwitch={handleSwitch}
      onSetMenuFor={setMenuFor}
      onStartRename={startRename}
      onCommitRename={commitRename}
      onCancelRename={cancelRename}
      onSetRenameValue={handleRenameValueChange}
      onArchive={archiveConversation}
      onClone={cloneConversation}
      onMerge={mergeConversation}
      onExport={exportConversation}
      onCleanup={cleanupWorktree}
      onHandoff={handoffWorktree}
      onReveal={revealConversationPath}
      onCopy={copyConversationPath}
      treeDepth={conversation.treeDepth}
    />
  ), [
    archiveConversation,
    cancelRename,
    cleanupWorktree,
    cloneConversation,
    commitRename,
    conversationsById,
    copyConversationPath,
    exportConversation,
    handleRenameValueChange,
    handleSwitch,
    handoffWorktree,
    menuFor,
    mergeConversation,
    renameValue,
    renaming,
    revealConversationPath,
    startRename,
  ]);

  if (filtered.length === 0) {
    return (
      <EmptyState
        icon={<SquarePen size={22} />}
        title="开始你的第一个任务"
        action={
          <button
            type="button"
            onClick={() => {
              createConversation({ appMode });
              onNavigate?.();
            }}
          >
            新建任务
          </button>
        }
      />
    );
  }

  return (
    <>
      <div className="mc-sidebar-project-heading" style={{ ...sectionHeaderRowStyle, padding: "9px 10px 5px" }}>
        <span className="mc-sidebar-project-label">项目</span>
      </div>
      <div
        ref={listRef}
        data-testid="conversation-list"
        style={sessionListWrapStyle}
        onScroll={handleConversationListScroll}
      >
        {orderedProjectGroups.map(({ projectKey, group, conversations: orderedConversations }) => (
          <section key={projectKey} aria-label={`工作区 ${group.label}`} style={taskSectionStyle}>
            <div className="mc-workspace-group-header">
              <button
                type="button"
                aria-label={group.label}
                aria-expanded={!collapsedGroups.has(projectKey)}
                onClick={() => toggleGroup(projectKey)}
                className="mc-workspace-group-toggle"
                style={taskSectionHeaderStyle}
              >
                <span className="mc-workspace-folder-icon" aria-hidden="true">
                  {collapsedGroups.has(projectKey)
                    ? <Folder className="mc-workspace-folder-glyph" size={16} data-testid={`workspace-folder-closed-${projectKey}`} />
                    : <FolderOpen className="mc-workspace-folder-glyph" size={16} data-testid={`workspace-folder-open-${projectKey}`} />}
                </span>
                <span className="mc-workspace-label">{group.label}</span>
                <span className="mc-workspace-session-count" aria-hidden="true">{group.items.length}</span>
              </button>
              <button
                  type="button"
                  className="mc-workspace-new-session"
                  aria-label={`在 ${group.label} 中新建任务`}
                  title={`在 ${group.label} 中新建任务`}
                  data-testid={`workspace-new-session-${projectKey}`}
                  onClick={() => startWorkspaceConversation(projectKey, group)}
                >
                  <SquarePen size={15} aria-hidden="true" />
              </button>
            </div>
            {!collapsedGroups.has(projectKey) && (
              <div className="mc-workspace-group-body">
                <div className="mc-workspace-group-body-inner" style={taskSectionBodyStyle}>
                  {orderedConversations.map(renderSessionRow)}
                </div>
              </div>
            )}
          </section>
        ))}

        {ordinaryConversations.length > 0 && (
          <section aria-label="普通任务" style={taskSectionStyle}>
            <button
              type="button"
              aria-expanded={!collapsedGroups.has("__ordinary_tasks__")}
              onClick={() => toggleGroup("__ordinary_tasks__")}
              style={taskSectionHeaderStyle}
            >
              <span aria-hidden="true">{collapsedGroups.has("__ordinary_tasks__") ? <ChevronRight size={14} /> : <ChevronDown size={14} />}</span>
              <span>普通任务</span>
              <span style={projectCountStyle}>{ordinaryConversations.length}</span>
            </button>
            {!collapsedGroups.has("__ordinary_tasks__") && (
              <div className="mc-workspace-group-body">
                <div className="mc-workspace-group-body-inner" style={projectItemsStyle}>
                  {orderedOrdinaryConversations.map(renderSessionRow)}
                </div>
              </div>
            )}
          </section>
        )}
      </div>

    </>
  );
};

const taskSectionStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  minWidth: 0,
};

const taskSectionHeaderStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 34,
  display: "flex",
  alignItems: "center",
  gap: 9,
  padding: "0 10px",
  border: 0,
  borderRadius: "var(--radius-sm, 6px)",
  background: "transparent",
  color: "var(--text-primary)",
  cursor: "pointer",
  fontSize: "var(--text-chrome)",
  fontWeight: "var(--fw-medium)",
  textAlign: "left",
};

const taskSectionBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  minWidth: 0,
  paddingLeft: 0,
};
