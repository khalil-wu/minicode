import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Circle,
  Folder,
  FolderOpen,
  Loader,
  LoaderCircle,
  Pause,
  SearchX,
  SquarePen,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { EmptyState } from "../components/EmptyState";
import { isDesktop, revealPath } from "../desktop/runtime";
import type { ConversationMeta, SessionFilter } from "../stores/types";
import * as wsOutbox from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { runtimePendingUserActionLabelForConversation } from "../lib/runtime-session";
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
  projectCountStyle,
  projectItemsStyle,
} from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";
import { readableToolLabel } from "../chat/toolDisplayName";
import { pushToast } from "../overlays/ToastContainer";
import { safeJsonParse } from "../lib/safe-parse";

type SidebarTab = "conversations" | "files";

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

const SESSION_FILTERS: { id: SessionFilter; label: string; icon: React.ReactNode }[] = [
  { id: "all", label: "全部", icon: null },
  { id: "running", label: "运行中", icon: <Loader size={14} className="animate-spin" /> },
  { id: "waiting", label: "等待中", icon: <Pause size={14} /> },
  { id: "idle", label: "空闲", icon: <Circle size={14} /> },
  { id: "archived", label: "已归档", icon: <Archive size={14} /> },
];

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

const RECENT_WORKSPACE_TYPE_LABELS: Record<string, string> = {
  python: "Python",
  node: "Node.js",
  rust: "Rust",
  go: "Go",
  java: "Java",
};

export const recentWorkspaceMetadata = (projectType: string, lastOpened: number): string => {
  const normalizedType = String(projectType || "").trim().toLowerCase();
  const typeLabel = normalizedType && normalizedType !== "unknown"
    ? RECENT_WORKSPACE_TYPE_LABELS[normalizedType] || projectType.trim()
    : "类型未知";
  const openedAt = Number.isFinite(lastOpened) && lastOpened > 0
    ? new Date(lastOpened * 1000)
    : null;
  const openedLabel = openedAt && Number.isFinite(openedAt.getTime())
    ? `上次打开 ${openedAt.toLocaleString()}`
    : "打开时间未知";
  return `${typeLabel} · ${openedLabel}`;
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
  const recentWorkspaces = useAppStore((s) => s.recentWorkspaces);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const diffReviewQueue = useAppStore((s) => s.diffReviewQueue);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const askUserQueue = useAppStore((s) => s.askUserQueue);
  const runtimeSession = useAppStore((s) => s.runtimeSession);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const appMode = useAppStore((s) => s.appMode);
  const createConversation = useAppStore((s) => s.createConversation);
  const isConnected = useAppStore((s) => s.isConnected);

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
  const [removingRecentPaths, setRemovingRecentPaths] = useState<Set<string>>(new Set());
  const [clearingRecentWorkspaces, setClearingRecentWorkspaces] = useState(false);

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
  const visibleRecentWorkspaces = useMemo(() => {
    const represented = new Set(
      enrichedConversations
        .map((conversation) => workspaceGroupIdentity(conversationWorkspacePath(conversation)))
        .filter(Boolean),
    );
    return recentWorkspaces
      .filter((workspace) => !represented.has(workspaceGroupIdentity(workspace.path)))
      .slice(0, 5);
  }, [enrichedConversations, recentWorkspaces]);
  const selectedSessions = useMemo(() => enrichedConversations.filter((c) => selectedSessionIds.has(c.id)), [enrichedConversations, selectedSessionIds]);
  const conversationsById = useMemo(
    () => new Map(conversations.map((conversation) => [conversation.id, conversation])),
    [conversations],
  );
  const selectableFiltered = useMemo(
    () => filtered.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting"),
    [filtered],
  );
  const allFilteredSelected = selectableFiltered.length > 0 && selectableFiltered.every((c) => selectedSessionIds.has(c.id));

  const conversationsRef = useRef(conversations);
  const enrichedConversationsRef = useRef(enrichedConversations);
  // Read through a ref like every other row callback in this file: listing
  // Read the active id through a ref so switching chats does not recreate the
  // row renderer and force every session row to rerender.
  const conversationIdRef = useRef(conversationId);
  const renameStateRef = useRef({ renaming, renameValue });
  const onSetConfirmDialogRef = useRef(onSetConfirmDialog);
  const onNavigateRef = useRef(onNavigate);
  conversationsRef.current = conversations;
  enrichedConversationsRef.current = enrichedConversations;
  conversationIdRef.current = conversationId;
  renameStateRef.current = { renaming, renameValue };
  onSetConfirmDialogRef.current = onSetConfirmDialog;
  onNavigateRef.current = onNavigate;

  useEffect(() => {
    if (isConnected) wsOutbox.sendClientCommand({ type: "workspace.recent" });
  }, [isConnected]);

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

  const toggleSessionSelected = useCallback((id: string) => {
    setSelectedSessionIds((prev) => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next; });
  }, []);

  const setAllFilteredSelected = useCallback((selected: boolean) => {
    setSelectedSessionIds((prev) => {
      const next = new Set(prev);
      for (const c of selectableFiltered) { selected ? next.add(c.id) : next.delete(c.id); }
      return next;
    });
  }, [selectableFiltered]);

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
      selectionMode={selectionMode}
      selected={selectedSessionIds.has(conversation.id)}
      menuOpen={menuFor === conversation.id}
      renaming={renaming === conversation.id}
      renameValue={renaming === conversation.id ? renameValue : ""}
      waitingLabel={conversation.waitingLabel}
      onSwitch={handleSwitch}
      onToggleSelected={toggleSessionSelected}
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
    selectedSessionIds,
    selectionMode,
    startRename,
    toggleSessionSelected,
  ]);

  const openRecentWorkspace = (path: string) => {
    if (!path || !wsOutbox.sendClientCommand({ type: "workspace.set", path })) return;
    useAppStore.getState().setAppMode("code");
    pushToast(`正在打开工作区：${path}`, "info", 2600);
  };

  const removeRecentWorkspace = async (path: string) => {
    const identity = workspaceGroupIdentity(path);
    if (!path || removingRecentPaths.has(identity) || clearingRecentWorkspaces) return;
    setRemovingRecentPaths((current) => new Set(current).add(identity));
    try {
      const result = await wsOutbox.sendClientCommandAwaitResult({
        type: "workspace.recent.remove",
        path,
      }, "workspace.recent.remove");
      if (!wsOutbox.commandResultSucceeded(result)) {
        pushToast(result.message || "无法删除最近工作区记录。", "error", 5000);
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "无法删除最近工作区记录。", "error", 5000);
    } finally {
      setRemovingRecentPaths((current) => {
        const next = new Set(current);
        next.delete(identity);
        return next;
      });
    }
  };

  const clearRecentWorkspaces = () => {
    if (clearingRecentWorkspaces || recentWorkspaces.length === 0) return;
    onSetConfirmDialog({
      title: "清空最近工作区",
      message: "只清除最近工作区列表，不会删除任何文件或项目目录。",
      confirmLabel: "清空列表",
      danger: true,
      onConfirm: () => {
        void (async () => {
          setClearingRecentWorkspaces(true);
          try {
            const result = await wsOutbox.sendClientCommandAwaitResult(
              { type: "workspace.recent.clear" },
              "workspace.recent.clear",
            );
            if (!wsOutbox.commandResultSucceeded(result)) {
              pushToast(result.message || "无法清空最近工作区列表。", "error", 5000);
            }
          } catch (error) {
            pushToast(error instanceof Error ? error.message : "无法清空最近工作区列表。", "error", 5000);
          } finally {
            setClearingRecentWorkspaces(false);
          }
        })();
      },
    });
  };

  if (enrichedConversations.length === 0 && recentWorkspaces.length === 0) {
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
      {selectionMode && (
        <div style={bulkBarStyle} role="toolbar" aria-label="会话选择操作">
          <span style={bulkMetaStyle} aria-live="polite">已选择 {selectedSessions.length} 个</span>
          <div style={bulkActionsStyle}>
            <button type="button" onClick={() => setAllFilteredSelected(!allFilteredSelected)} style={bulkActionStyle} disabled={selectableFiltered.length === 0}>
              {allFilteredSelected ? "清除当前选择" : "选择当前结果"}
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
                 background: currentWorkspaceOnly ? "var(--surface-active)" : "transparent",
                 color: currentWorkspaceOnly ? "var(--text-primary)" : "var(--text-muted)",
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
                 style={{ ...filterButtonStyle, background: sessionFilter === f.id ? "var(--surface-active)" : "transparent", color: sessionFilter === f.id ? "var(--text-primary)" : "var(--text-muted)", fontWeight: sessionFilter === f.id ? 600 : 500 }}>
                {f.icon}{f.label}{count > 0 && <span style={filterCountStyle}>{count}</span>}
              </button>
            );
          })}
        </div>
      )}

      {recentWorkspaces.length > 0 && !selectionMode && (
        <section aria-label="最近工作区" style={{ padding: "0 8px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, color: "var(--text-muted)", fontSize: "var(--text-3xs)", padding: "2px 4px 5px" }}>
            <span>最近工作区</span>
            <button
              type="button"
              onClick={clearRecentWorkspaces}
              disabled={clearingRecentWorkspaces}
              aria-label="清空最近工作区"
              title="只清除最近工作区列表，不会删除项目文件"
              style={{ ...recentsActionStyle(false), width: "auto", padding: "0 5px", gap: 4, fontSize: "var(--text-3xs)" }}
            >
              {clearingRecentWorkspaces && <LoaderCircle size={12} className="animate-spin" aria-hidden="true" />}
              清空
            </button>
          </div>
          <div style={{ display: "grid", gap: 2 }}>
            {visibleRecentWorkspaces.length === 0 && (
              <span style={{ padding: "5px 7px", color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>
                当前记录均已显示在项目列表中
              </span>
            )}
            {visibleRecentWorkspaces.map((workspace) => {
              const identity = workspaceGroupIdentity(workspace.path);
              const removing = removingRecentPaths.has(identity);
              return (
                <div key={identity} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 28px", alignItems: "stretch", gap: 2 }}>
                  <button
                    type="button"
                    onClick={() => openRecentWorkspace(workspace.path)}
                    disabled={removing || clearingRecentWorkspaces}
                    title={`${workspace.path}\n${recentWorkspaceMetadata(workspace.projectType, workspace.lastOpened)}`}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "18px minmax(0, 1fr)",
                      alignItems: "center",
                      gap: 7,
                      width: "100%",
                      padding: "6px 7px",
                      border: 0,
                      borderRadius: "var(--radius-sm, 5px)",
                      background: "transparent",
                      color: "var(--text-secondary)",
                      textAlign: "left",
                      cursor: removing ? "wait" : "pointer",
                    }}
                  >
                    <Folder size={15} aria-hidden="true" />
                    <span style={{ minWidth: 0 }}>
                      <strong style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: "var(--text-xs)" }}>
                        {workspace.name || workspaceDisplayName(workspace.path, "工作区")}
                      </strong>
                      <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>
                        {workspace.path}
                      </span>
                      <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-muted)", fontSize: "var(--text-3xs)" }}>
                        {recentWorkspaceMetadata(workspace.projectType, workspace.lastOpened)}
                      </span>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => void removeRecentWorkspace(workspace.path)}
                    disabled={removing || clearingRecentWorkspaces}
                    aria-label={`删除最近工作区记录 ${workspace.name || workspace.path}`}
                    title="仅删除最近记录，不删除项目文件"
                    className="mc-icon-button mc-icon-button-danger"
                    style={{ alignSelf: "center", width: 26, height: 26 }}
                  >
                    {removing ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <X size={13} aria-hidden="true" />}
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <div
        ref={listRef}
        data-testid="conversation-list"
        style={sessionListWrapStyle}
        onScroll={handleConversationListScroll}
      >
        {filtered.length === 0 ? (
          <EmptyState compact icon={<SearchX size={20} />} title="当前筛选下暂无会话" hint="换个关键词，或新建一个任务。" />
        ) : (
          <>
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
  fontWeight: "var(--fw-medium)",
  textAlign: "left",
};

const taskSectionBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  minWidth: 0,
  paddingLeft: 0,
};
