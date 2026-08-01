import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Circle,
  Folder,
  FolderOpen,
  Loader,
  Pause,
  SquarePen,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop, revealPath } from "../desktop/runtime";
import type { ConversationMeta, SessionFilter } from "../stores/types";
import * as wsOutbox from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import {
  hasRuntimePendingUserActionForConversation,
  runtimePendingUserActionLabelForConversation,
} from "../lib/runtime-session";
import { SessionRow } from "./SessionRow";
import {
  sectionHeaderRowStyle,
  bulkBarStyle,
  bulkActionStyle,
  bulkActionsStyle,
  bulkMetaStyle,
  searchBarWrapStyle,
  searchInputStyle,
  filterRowStyle,
  filterButtonStyle,
  filterCountStyle,
  sessionListWrapStyle,
  emptyStateStyle,
  projectCountStyle,
  projectItemsStyle,
} from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";

type SidebarTab = "conversations" | "files";

const CONVERSATION_UI_STATE_KEY = "minicode.sidebar.conversations.state";
const readConversationUiState = () => {
  try {
    const value = JSON.parse(localStorage.getItem(CONVERSATION_UI_STATE_KEY) || "{}");
    return {
      collapsedGroups: new Set<string>(Array.isArray(value.collapsedGroups) ? value.collapsedGroups : []),
      scrollTop: typeof value.scrollTop === "number" ? value.scrollTop : 0,
    };
  } catch {
    return { collapsedGroups: new Set<string>(), scrollTop: 0 };
  }
};

const SESSION_FILTERS: { id: SessionFilter; label: string; icon: React.ReactNode }[] = [
  { id: "all", label: "全部", icon: null },
  { id: "running", label: "运行中", icon: <Loader size={14} className="spin" /> },
  { id: "waiting", label: "等待中", icon: <Pause size={14} /> },
  { id: "idle", label: "空闲", icon: <Circle size={14} /> },
  { id: "archived", label: "已归档", icon: <Archive size={14} /> },
];

export type EnrichedConversation = ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" };

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

/** 会话等待状态标签——抽离为独立React.memo组件避免每次渲染重建 */
const ConversationWaitingLabel = memo(({
  pendingAskUser, pendingDiffReview, pendingApproval,
}: {
  pendingAskUser: boolean; pendingDiffReview: boolean; pendingApproval: { toolName: string } | null;
}) => {
  if (pendingAskUser) return <>等待回复</>;
  if (pendingDiffReview) return <>等待审阅</>;
  if (pendingApproval) return <>等待批准 {pendingApproval.toolName}</>;
  return null;
});
ConversationWaitingLabel.displayName = "ConversationWaitingLabel";

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
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const runtimeSession = useAppStore((s) => s.runtimeSession);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const appMode = useAppStore((s) => s.appMode);
  const createConversation = useAppStore((s) => s.createConversation);

  const [currentWorkspaceOnly, setCurrentWorkspaceOnly] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all");
  const initialUiState = useMemo(readConversationUiState, []);
  const [search, setSearch] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(initialUiState.collapsedGroups);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());

  const waitingLabelForConversation = useCallback((id: string) => {
    if (id === conversationId) {
      if (pendingAskUser) return "等待回复";
      if (pendingDiffReview) return "等待审阅";
      if (pendingApproval) return `等待批准 ${pendingApproval.toolName}`;
    }
    return runtimePendingUserActionLabelForConversation(runtimeSession, id);
  }, [conversationId, pendingAskUser, pendingDiffReview, pendingApproval, runtimeSession]);

  const enrichedConversations = useMemo(() => {
    return conversations.map((c) => ({
      ...c,
      sessionStatus: (() => {
        const hasStreamingFlag = isConversationRunning({
          conversationId: c.id,
          activeConversationId: conversationId,
          activeIsStreaming: isStreaming,
          conversationStreaming,
        });
        if (hasStreamingFlag) return "running";
        if (c.id === conversationId && (pendingAskUser || pendingDiffReview || pendingApproval)) return "waiting";
        if (hasRuntimePendingUserActionForConversation(runtimeSession, c.id)) return "waiting";
        return c.sessionStatus || "idle";
      })() as "running" | "waiting" | "idle",
    }));
  }, [conversations, conversationId, isStreaming, conversationStreaming, pendingApproval, pendingDiffReview, pendingAskUser, runtimeSession]);

  const filterCounts = useMemo(() => {
    const counts: Record<string, number> = { all: 0, archived: 0, running: 0, waiting: 0, idle: 0 };
    for (const c of enrichedConversations) {
      if (c.archived) { counts.archived++; }
      else { counts.all++; counts[c.sessionStatus] = (counts[c.sessionStatus] || 0) + 1; }
    }
    return counts;
  }, [enrichedConversations]);

  const currentWorkspaceDisplayPath = useMemo(() => {
    return canonicalWorkspacePath(workspaceGit?.currentPath || workingDirectory || "");
  }, [workspaceGit?.currentPath, workingDirectory]);
  const currentWorkspacePath = useMemo(() => (
    currentWorkspaceDisplayPath ? workspaceGroupIdentity(currentWorkspaceDisplayPath) : ""
  ), [currentWorkspaceDisplayPath]);

  const filtered = useMemo(() => {
    let list = enrichedConversations;
    if (search) {
      const q = search.toLowerCase();
      list = list.filter((c) =>
        c.title.toLowerCase().includes(q) ||
        c.workspaceRoot?.toLowerCase().includes(q) ||
        c.worktreePath?.toLowerCase().includes(q) ||
        c.gitBranch?.toLowerCase().includes(q) ||
        c.goal?.text.toLowerCase().includes(q)
      );
    }
    if (currentWorkspaceOnly && currentWorkspacePath) {
      list = list.filter((c) => {
        const conversationWorkspace = workspaceGroupIdentity(conversationWorkspacePath(c));
        const conversationWorktree = workspaceGroupIdentity(c.worktreePath || "");
        return conversationWorkspace === currentWorkspacePath || conversationWorktree === currentWorkspacePath;
      });
    }
    if (sessionFilter === "archived") list = list.filter((c) => c.archived);
    else if (sessionFilter === "all") list = list.filter((c) => !c.archived);
    else list = list.filter((c) => !c.archived && c.sessionStatus === sessionFilter);
    return list;
  }, [enrichedConversations, search, currentWorkspaceOnly, currentWorkspacePath, sessionFilter]);

  const workspaceConversations = useMemo(() => filtered.filter(isWorkspaceConversation), [filtered]);
  const ordinaryConversations = useMemo(() => filtered.filter((conversation) => !isWorkspaceConversation(conversation)), [filtered]);
  const projectGroups = useMemo(() => groupByWorkspace(workspaceConversations), [workspaceConversations]);
  const selectedSessions = useMemo(() => enrichedConversations.filter((c) => selectedSessionIds.has(c.id)), [enrichedConversations, selectedSessionIds]);
  const selectableFiltered = useMemo(() => filtered.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting"), [filtered]);
  const allFilteredSelected = selectableFiltered.length > 0 && selectableFiltered.every((c) => selectedSessionIds.has(c.id));

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

  const sendConversationDelete = wsOutbox.sendConversationDeleteCommand;

  const deleteConversation = (id: string) => {
    const conversation = conversations.find((c) => c.id === id);
    if (!conversation) return;
    onSetConfirmDialog({
      title: "删除会话",
      message: conversation.gitIsolated
        ? "删除此受保护会话并移除它的独立工作区？"
        : "删除此会话？它将从会话列表中移除。",
      confirmLabel: "删除",
      danger: true,
      onConfirm: () => {
        void sendConversationDelete({
          type: "conversation.delete",
          conversation_id: id,
          cleanup_worktree: Boolean(conversation.gitIsolated),
        });
      },
    });
  };

  const deleteSessionBatch = (items: typeof enrichedConversations, label: string) => {
    const deletable = items.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting");
    if (deletable.length === 0) return;
    const isolatedCount = deletable.filter((c) => c.gitIsolated).length;
    const skipped = items.length - deletable.length;
    onSetConfirmDialog({
      title: label,
      message: [
        `删除 ${deletable.length} 个会话？它们将从会话列表中移除。`,
        isolatedCount > 0 ? `同时清理 ${isolatedCount} 个受保护工作区。` : "",
        skipped > 0 ? `跳过 ${skipped} 个运行中或等待中的会话。` : "",
      ].filter(Boolean).join("\n\n"),
      confirmLabel: "删除",
      danger: true,
      onConfirm: () => {
        for (const c of deletable) {
          void sendConversationDelete({
            type: "conversation.delete",
            conversation_id: c.id,
            cleanup_worktree: Boolean(c.gitIsolated),
          });
        }
        setSelectedSessionIds(new Set());
        setSelectionMode(false);
      },
    });
  };

  const toggleSessionSelected = (id: string) => {
    setSelectedSessionIds((prev) => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next; });
  };

  const setAllFilteredSelected = (selected: boolean) => {
    setSelectedSessionIds((prev) => {
      const next = new Set(prev);
      for (const c of selectableFiltered) { selected ? next.add(c.id) : next.delete(c.id); }
      return next;
    });
  };

  const cleanupWorktree = (id: string, force = false) => {
    const conversation = conversations.find((c) => c.id === id);
    if (!conversation?.gitIsolated) return;
    onSetConfirmDialog({
      title: force ? "强制清理工作区" : "清理工作区",
      message: force
        ? "强制移除这个隔离会话工作区并丢弃本地更改吗？"
        : "移除这个隔离会话工作区吗？如果存在本地更改，MiniCode 会再询问是否强制清理。",
      confirmLabel: force ? "强制清理" : "清理",
      danger: force,
      onConfirm: () => { wsOutbox.sendClientCommand({ type: "conversation.worktree.cleanup", conversation_id: id, force }); },
    });
  };

  const archiveConversation = (id: string, archived: boolean) => {
    wsOutbox.sendClientCommand({ type: archived ? "conversation.archive" : "conversation.unarchive", conversation_id: id, archived });
    useAppStore.setState((s) => ({ conversations: s.conversations.map((c) => (c.id === id ? { ...c, archived } : c)) }));
  };

  const handleSwitch = (id: string) => {
    setMenuFor(null);
    requestConversationSwitch(id);
    onNavigate?.();
  };

  const handoffWorktree = (id: string, target: "local" | "worktree") => {
    wsOutbox.sendClientCommand({
      type: "conversation.worktree.handoff.preflight",
      conversation_id: id,
      target,
    });
  };
  const revealConversationPath = (path?: string) => { if (!path) return; setMenuFor(null); if (isDesktop()) void revealPath(path); };
  const copyConversationPath = (path?: string) => { if (!path) return; setMenuFor(null); void navigator.clipboard?.writeText(path); };
  const startRename = (id: string, currentTitle: string) => { setRenaming(id); setRenameValue(currentTitle); };
  const commitRename = () => {
    if (!renaming || !renameValue.trim()) { setRenaming(null); return; }
    wsOutbox.sendClientCommand({ type: "conversation.rename", conversation_id: renaming, title: renameValue.trim() });
    useAppStore.setState((s) => ({ conversations: s.conversations.map((c) => (c.id === renaming ? { ...c, title: renameValue.trim() } : c)) }));
    setRenaming(null);
  };
  const cancelRename = () => { setRenaming(null); };

  const toggleGroup = (key: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      try {
        localStorage.setItem(CONVERSATION_UI_STATE_KEY, JSON.stringify({
          collapsedGroups: Array.from(next),
          scrollTop: listRef.current?.scrollTop ?? 0,
        }));
      } catch { /* noop */ }
      return next;
    });
  };
  const cloneConversation = (id: string) => {
    wsOutbox.sendClientCommand({ type: "conversation.clone", conversation_id: id, activate: false });
  };
  const mergeConversation = (id: string) => {
    const conversation = conversations.find((item) => item.id === id);
    const parent = conversation?.parentConversationId
      ? conversations.find((item) => item.id === conversation.parentConversationId)
      : undefined;
    if (!conversation || !parent) return;
    onSetConfirmDialog({
      title: "合并会话分支",
      message: `将“${conversation.title}”的新增消息快速合并到“${parent.title}”。如果父会话已分叉修改，后端会拒绝合并并保留两边内容。`,
      confirmLabel: "合并",
      onConfirm: () => wsOutbox.sendClientCommand({
        type: "conversation.merge",
        conversation_id: id,
        target_conversation_id: parent.id,
      }),
    });
  };
  const exportConversation = (id: string) => {
    wsOutbox.sendClientCommand({ type: "conversation.export", conversation_id: id, include_descendants: true });
  };

  const startWorkspaceConversation = (projectKey: string, group: WorkspaceConversationGroup) => {
    const workspaceRoot = conversationWorkspacePath(group.items[0]);
    if (!workspaceRoot) return;
    setCollapsedGroups((prev) => {
      if (!prev.has(projectKey)) return prev;
      const next = new Set(prev);
      next.delete(projectKey);
      return next;
    });
    createConversation({ bindWorkspace: true, workspaceRoot, appMode });
    onNavigate?.();
  };

  const renderSessionRow = (conversation: TreeConversation) => (
    <SessionRow
      key={conversation.id}
      conversation={conversation}
      conversationId={conversationId}
      selectionMode={selectionMode}
      selectedSessionIds={selectedSessionIds}
      menuFor={menuFor}
      renaming={renaming}
      renameValue={renameValue}
      waitingLabelForConversation={waitingLabelForConversation}
      onSwitch={handleSwitch}
      onToggleSelected={toggleSessionSelected}
      onSetMenuFor={setMenuFor}
      onStartRename={startRename}
      onCommitRename={commitRename}
      onCancelRename={cancelRename}
      onSetRenameValue={setRenameValue}
      onArchive={archiveConversation}
      onClone={cloneConversation}
      onMerge={mergeConversation}
      onExport={exportConversation}
      onDelete={deleteConversation}
      onCleanup={cleanupWorktree}
      onHandoff={handoffWorktree}
      onReveal={revealConversationPath}
      onCopy={copyConversationPath}
      treeDepth={conversation.treeDepth}
    />
  );

  if (enrichedConversations.length === 0) return null;

  return (
    <>
      {selectionMode && (
        <div style={bulkBarStyle} role="toolbar" aria-label="会话选择操作">
          <span style={bulkMetaStyle} aria-live="polite">已选择 {selectedSessions.length} 个</span>
          <div style={bulkActionsStyle}>
            <button type="button" onClick={() => setAllFilteredSelected(!allFilteredSelected)} style={bulkActionStyle} disabled={selectableFiltered.length === 0}>
              {allFilteredSelected ? "清除当前选择" : "选择当前结果"}
            </button>
            <button
              type="button"
              onClick={() => deleteSessionBatch(selectedSessions, "删除所选会话")}
              className="mc-icon-button mc-icon-button-danger"
              style={{ color: "var(--state-danger)" }}
              disabled={selectedSessions.length === 0}
              title="删除所选会话"
              aria-label="删除所选会话"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      )}

      <div className="mc-sidebar-project-heading" style={{ ...sectionHeaderRowStyle, padding: "9px 10px 5px" }}>
        <span className="mc-sidebar-project-label">项目</span>
        <button
          type="button"
          onClick={() => { setSelectionMode((v) => !v); setSelectedSessionIds(new Set()); }}
          title={selectionMode ? "取消选择" : "选择会话"}
          aria-label={selectionMode ? "取消选择" : "选择会话"}
          style={recentsActionStyle(selectionMode)}
        >
          {selectionMode ? <X size={14} /> : <SlidersHorizontal size={14} />}
        </button>
      </div>
      {selectionMode && (
        <div style={{ ...searchBarWrapStyle, padding: "3px 6px 8px" }}>
          <input type="text" placeholder="搜索会话" value={search} onChange={(e) => setSearch(e.target.value)} style={searchInputStyle} />
        </div>
      )}

      {selectionMode && (currentWorkspacePath || sessionFilter !== "all" || filterCounts.running > 0 || filterCounts.waiting > 0 || filterCounts.archived > 0) && (
        <div style={{ ...filterRowStyle, padding: "0 6px 8px" }}>
          {currentWorkspacePath && (
            <button
              type="button"
              onClick={() => setCurrentWorkspaceOnly((value) => !value)}
              style={{
                ...filterButtonStyle,
                background: currentWorkspaceOnly ? "var(--accent-soft)" : "transparent",
                color: currentWorkspaceOnly ? "var(--accent-primary)" : "var(--text-muted)",
                fontWeight: currentWorkspaceOnly ? 600 : 500,
              }}
              title={currentWorkspaceDisplayPath}
            >
              当前工作区
            </button>
          )}
          {SESSION_FILTERS.filter((f) => f.id === "all" || f.id === sessionFilter || (filterCounts[f.id] || 0) > 0).map((f) => {
            const count = filterCounts[f.id] || 0;
            return (
              <button key={f.id} onClick={() => setSessionFilter(f.id)}
                style={{ ...filterButtonStyle, background: sessionFilter === f.id ? "var(--accent-soft)" : "transparent", color: sessionFilter === f.id ? "var(--accent-primary)" : "var(--text-muted)", fontWeight: sessionFilter === f.id ? 600 : 500 }}>
                {f.icon}{f.label}{count > 0 && <span style={filterCountStyle}>{count}</span>}
              </button>
            );
          })}
        </div>
      )}

      <div
        ref={listRef}
        style={sessionListWrapStyle}
        onScroll={(event) => {
          try {
            localStorage.setItem(CONVERSATION_UI_STATE_KEY, JSON.stringify({
              collapsedGroups: Array.from(collapsedGroups),
              scrollTop: event.currentTarget.scrollTop,
            }));
          } catch { /* noop */ }
        }}
      >
        {filtered.length === 0 ? (
          <div style={emptyStateStyle}>
            当前筛选下暂无会话
          </div>
        ) : (
          <>
            {Array.from(projectGroups.entries()).map(([projectKey, group]) => (
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
                        ? <Folder className="mc-workspace-folder-glyph" size={17} data-testid={`workspace-folder-closed-${projectKey}`} />
                        : <FolderOpen className="mc-workspace-folder-glyph" size={17} data-testid={`workspace-folder-open-${projectKey}`} />}
                    </span>
                    <span className="mc-workspace-label">{group.label}</span>
                    <span className="mc-workspace-session-count" aria-hidden="true">{group.items.length}</span>
                  </button>
                  {!selectionMode && (
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
                  )}
                </div>
                {!collapsedGroups.has(projectKey) && (
                  <div className="mc-workspace-group-body">
                    <div className="mc-workspace-group-body-inner" style={taskSectionBodyStyle}>
                      {orderConversationTree(group.items).map(renderSessionRow)}
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
                      {orderConversationTree(ordinaryConversations).map(renderSessionRow)}
                    </div>
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </div>

    </>
  );
};

const recentsActionStyle = (active: boolean): React.CSSProperties => ({
  width: 26,
  height: 26,
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  background: active ? "var(--surface-active)" : "transparent",
  color: active ? "var(--text-primary)" : "var(--text-muted)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: "pointer",
  padding: 0,
});

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
  fontWeight: 500,
  textAlign: "left",
};

const taskSectionBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  minWidth: 0,
  paddingLeft: 0,
};
