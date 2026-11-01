import { useI18n } from "../i18n";
import { cloneElement, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import {
  Folder,
  FolderOpen,
  MoreHorizontal,
  Plus,
  SquarePen,
} from "lucide-react";
import { useAppStore } from "../stores";
import { EmptyState } from "../components/EmptyState";
import type { ConversationMeta } from "../stores/types";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";
import { showConfirm, showPrompt } from "../overlays/DialogService";
import { copyText } from "../lib/clipboard";
import * as wsOutbox from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { runtimePendingUserActionLabels } from "../lib/runtime-session";
import { SessionRow } from "./SessionRow";
import { useThreadPullRequestStatuses } from "./ThreadPullRequestBadge";
import { PinnedConversationList } from "./PinnedConversationList";
import { PinnedWorkspaceFiles } from "./WorkspaceFileTabs";
import { SidebarSortableList, type SidebarDragBinding } from "./SidebarSortableList";
import { SidebarSortMenu } from "./SidebarSortMenu";
import { orderSidebarItems } from "./sidebarOrder";
import {
  sectionHeaderRowStyle,
  sessionListWrapStyle,
} from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";
import { readableToolLabel } from "../chat/toolDisplayName";
import { pushToast } from "../overlays/ToastContainer";
import { safeJsonParse } from "../lib/safe-parse";
import { ProjectContextMenu } from "../workspace/ProjectContextMenu";
import { projectExecutionRoot, projectOwnsWorkspace } from "../lib/project-registry";
import { ProjectMarkerIcon } from "../components/ProjectMarker";
import { fromMarkdown, markdownNodeText } from "../lib/markdown";
import { setConversationArchived } from "../chat/archiveConversation";
import { getWebSocket } from "../hooks/useWebSocket";
import { useSidebarSections, type SidebarSection } from "../protocol/thread-metadata";
import { SidebarSections } from "./SidebarSections";
import { handoffConversationWorktree } from "../lib/handoff-worktree";

// The menu invocation owns its reply even after the reader changes chats.
const runMenuCommand = async (command: ClientCommand): Promise<CommandResultEvent | undefined> => {
  try {
    const result = await wsOutbox.sendClientCommandAwaitResult(command, command.type, {
      silent: true, timeoutMs: command.type.startsWith("conversation.worktree.") ? wsOutbox.LONG_COMMAND_RESULT_TIMEOUT_MS : undefined,
    });
    if (!wsOutbox.commandResultSucceeded(result)) {
      pushToast(result.message, "error", 6000);
      return;
    }
    return result;
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error", 6000);
  }
};

const CONVERSATION_UI_STATE_KEY = "minicode.sidebar.conversations.state";
const CONVERSATION_UI_PERSIST_DELAY_MS = 140;

const activitySummaryText = (summary?: string): string => {
  if (!summary) return "";
  // build_conversation_summary publishes role-delimited segments. Prefer the
  // latest real assistant segment; other summaries retain their own content.
  const segments = summary.split(/(?:^|\s+\|\s+)(?=(?:User|Assistant):\s)/);
  const assistant = [...segments].reverse().find((segment) => segment.startsWith("Assistant:"));
  const content = assistant ? assistant.slice("Assistant:".length) : summary.replace(/^User:\s*/, "");
  return markdownNodeText(fromMarkdown(content)).replace(/\s+/g, " ").trim();
};

const ActivitySummary = memo(({ summary }: { summary?: string }) => {
  const text = activitySummaryText(summary);
  return text ? <p className="mc-sidebar-activity-summary">{text}</p> : null;
});
ActivitySummary.displayName = "ActivitySummary";

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
  waitingLabel: string | null;
};

export interface WorkspaceConversationGroup {
  path: string;
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

const conversationWorkspacePath = (conversation: Pick<ConversationMeta, "workspaceRoot" | "worktreePath" | "projectId">): string => {
  if (conversation.projectId !== undefined && conversation.projectId !== null) return conversation.projectId;
  const worktree = conversation.worktreePath?.trim();
  if (worktree) return worktree;
  return conversation.workspaceRoot?.trim() || "";
};

export const isWorkspaceConversation = (conversation: Pick<ConversationMeta, "workspaceRoot" | "worktreePath" | "projectId">): boolean =>
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

export function groupByWorkspace(conversations: (ConversationMeta & { sessionStatus: string })[], openedPaths: string[] = [], names = new Map<string, string>()): Map<string, WorkspaceConversationGroup> {
  const groups = new Map<string, WorkspaceConversationGroup>();
  const addWorkspace = (basePath: string) => {
    const key = workspaceGroupIdentity(basePath);
    if (!groups.has(key)) {
      const label = names.get(key) || workspaceDisplayName(canonicalWorkspacePath(basePath), "Computer");
      groups.set(key, { path: basePath, baseLabel: label, label, items: [] });
    }
    return groups.get(key)!;
  };
  for (const path of openedPaths) addWorkspace(path);
  for (const c of conversations) {
    addWorkspace(conversationWorkspacePath(c)).items.push(c as EnrichedConversation);
  }

  const groupsByLabel = new Map<string, Array<[string, WorkspaceConversationGroup]>>();
  for (const entry of groups) {
    const sameLabel = groupsByLabel.get(entry[1].baseLabel);
    if (sameLabel) sameLabel.push(entry);
    else groupsByLabel.set(entry[1].baseLabel, [entry]);
  }
  for (const duplicates of groupsByLabel.values()) {
    if (duplicates.length < 2) continue;
    const candidates = duplicates.map(([key, group]) => ({ key, group, parents: workspacePathParts(group.path).slice(0, -1) }));
    const unresolved = new Set(candidates.map(({ key, group }) => {
      group.label = `${group.baseLabel} — ${key}`;
      return key;
    }));
    const maxDepth = Math.max(...candidates.map(candidate => candidate.parents.length));
    // Resolve shortest unique parent suffixes together instead of comparing
    // and reparsing every pair of same-named projects on each sidebar update.
    for (let depth = 1; depth <= maxDepth && unresolved.size; depth += 1) {
      const qualifiers = candidates.map(candidate => candidate.parents.slice(-depth).join("/"));
      const counts = new Map<string, number>();
      for (const qualifier of qualifiers) counts.set(qualifier, (counts.get(qualifier) ?? 0) + 1);
      candidates.forEach(({ key, group, parents }, index) => {
        if (unresolved.has(key) && depth <= parents.length && counts.get(qualifiers[index]) === 1) {
          group.label = `${group.baseLabel} — ${qualifiers[index]}`;
          unresolved.delete(key);
        }
      });
    }
  }
  return groups;
}

export const ConversationsTab = ({
  conversationId,
  onNavigate,
  onSetConfirmDialog,
  showRecent = true,
  activityView = false,
}: {
  conversationId: string;
  onNavigate?: () => void;
  onSetConfirmDialog: (dialog: { title: string; message: string; confirmLabel: string; danger?: boolean; onConfirm: () => void }) => void;
  showRecent?: boolean;
  activityView?: boolean;
}) => {
  const { t: tr } = useI18n();
  const conversations = useAppStore((s) => s.conversations);
  const pinnedConversationIds = useAppStore((s) => s.pinnedConversationIds);
  const hasPinnedFiles = useAppStore(s => s.workspaceFileTabs.some(tab => tab.pinned));
  const pinnedConversationSortMode = useAppStore((s) => s.pinnedConversationSortMode);
  const sidebarOrder = useAppStore((s) => s.sidebarOrder);
  const [pendingSortMode, setPendingSortMode] = useState<{ mode: "manual" | "updated_at" } | null>(null);
  const sidebarSortMode = pendingSortMode?.mode ?? sidebarOrder?.sort_mode ?? "updated_at";
  const recentWorkspaces = useAppStore((s) => s.recentWorkspaces);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const isConnected = useAppStore((s) => s.isConnected);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationStreaming = useAppStore((s) => s.conversationStreaming);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const diffReviewQueue = useAppStore((s) => s.diffReviewQueue);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const askUserQueue = useAppStore((s) => s.askUserQueue);
  const runtimeWaitingLabels = useAppStore(useShallow((s) => runtimePendingUserActionLabels(s.runtimeSession)));
  const startConversationDraft = useAppStore((s) => s.startConversationDraft);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renamingRow, setRenamingRow] = useState<string | null>(null);
  const [recentLimit, setRecentLimit] = useState(20);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(() => new Set());
  const [renameValue, setRenameValue] = useState("");
  const initialUiState = useMemo(readConversationUiState, []);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(initialUiState.collapsedGroups);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [selectedRow, setSelectedRow] = useState<{ conversationId: string; rowId: string } | null>(null);
  const [workspaceMenu, setWorkspaceMenu] = useState<{ path: string; x: number; y: number; anchor?: HTMLElement } | null>(null);
  const { sections, mutate: mutateSections } = useSidebarSections();
  const sectionByThread = useMemo(() => new Map(sections.flatMap(section => section.item_keys.filter(key => key.startsWith("thread:")).map(key => [key.slice(7), section.id] as const))), [sections]);
  const sectionProjectKeys = useMemo(() => new Set(sections.flatMap(section => section.item_keys.filter(key => key.startsWith("project:")).map(key => workspaceGroupIdentity(key.slice(8))))), [sections]);
  const sectionAction = useCallback(async (path: string, body?: unknown, method?: string) => {
    try { return await mutateSections(path, body, method); }
    catch (error) { pushToast(String(error), "error"); return undefined; }
  }, [mutateSections]);
  const editSection = useCallback(async (section?: SidebarSection, threadId?: string) => {
    const name = await showPrompt({ title: tr(section ? "编辑分组" : "新建分组"), message: tr("按你的方式组织聊天和项目"),
      defaultValue: section?.name ?? "", placeholder: tr("分组名称"), allowEmpty: !section,
      confirmLabel: tr(section ? "保存" : "创建分组") });
    if (name === null) return;
    try {
      const result = await mutateSections<{ section: SidebarSection }>(section ? `/sections/${section.id}` : "/sections",
        { name: name.trim() || tr("新建分组"), ...(!section && threadId && sectionByThread.has(threadId)
          ? { after_section_id: sectionByThread.get(threadId) } : {}) }, section ? "PATCH" : "POST");
      if (threadId) await mutateSections("/sections/move", { kind: "thread", id: threadId, section_id: result.section.id });
    } catch (error) { pushToast(String(error), "error"); }
  }, [mutateSections, sectionByThread, tr]);
  const moveToSection = useCallback((id: string, sectionId: string | null) => {
    void sectionAction("/sections/move", { kind: "thread", id, section_id: sectionId });
  }, [sectionAction]);
  const createSectionForThread = useCallback((id: string) => { void editSection(undefined, id); }, [editSection]);

  useEffect(() => {
    if (isConnected) wsOutbox.sendClientCommand({ type: "workspace.recent" });
  }, [isConnected]);

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
      if (id && !labels.has(id)) labels.set(id, tr("等待回复"));
    }
    for (const prompt of [pendingDiffReview, ...diffReviewQueue]) {
      const id = prompt?.conversationId?.trim();
      if (id && !labels.has(id)) labels.set(id, tr("等待审阅"));
    }
    for (const prompt of [pendingApproval, ...approvalQueue]) {
      const id = prompt?.conversationId?.trim();
      if (id && !labels.has(id)) {
        labels.set(id, tr("等待批准 {tool}", { tool: readableToolLabel(prompt?.toolName || "") }));
      }
    }
    return labels;
  }, [pendingAskUser, askUserQueue, pendingDiffReview, diffReviewQueue, pendingApproval, approvalQueue, tr]);

  const enrichedConversations = useMemo(() => {
    return conversations.map((conversation) => {
      const waitingLabel = waitingLabelsByConversation.get(conversation.id)
        ?? runtimeWaitingLabels.get(conversation.id) ?? runtimeWaitingLabels.get("") ?? null;
      let sessionStatus: EnrichedConversation["sessionStatus"] = "idle";
      if (waitingLabel) {
        sessionStatus = "waiting";
      } else if (isConversationRunning({
        conversationId: conversation.id,
        activeConversationId: conversationId,
        activeIsStreaming: isStreaming,
        conversationStreaming,
      })) {
        sessionStatus = "running";
      }
      return {
        ...conversation,
        waitingLabel,
        sessionStatus,
      };
    });
  }, [conversations, conversationId, isStreaming, conversationStreaming, waitingLabelsByConversation, runtimeWaitingLabels]);

  const filtered = useMemo(() => enrichedConversations.filter((conversation) => !conversation.archived), [enrichedConversations]);
  const sidebarProjects = useMemo(() => recentWorkspaces.filter(project => project.pinned !== false
    || sectionProjectKeys.has(workspaceGroupIdentity(project.path))), [recentWorkspaces, sectionProjectKeys]);

  // Legacy chats have no explicit project id. Resolve their source-folder
  // membership once for every occurrence, including pinned and recent rows.
  const projectConversations = useMemo(() => filtered.map(conversation => {
      if (conversation.projectId !== undefined && conversation.projectId !== null) return conversation;
      const project = recentWorkspaces.find(project => project.named && projectOwnsWorkspace(project, conversation.workspaceRoot || conversation.worktreePath || ""));
      return project ? { ...conversation, projectId: project.path } : conversation;
    }), [filtered, recentWorkspaces]);
  const projectMemberships = useMemo(() => new Map(projectConversations.map(conversation => {
    const path = conversationWorkspacePath(conversation);
    return [conversation.id, path ? workspaceGroupIdentity(path) : ""];
  })), [projectConversations]);
  const workspaceConversations = useMemo(() => {
    const opened = new Set(sidebarProjects.map((workspace) => workspaceGroupIdentity(workspace.path)));
    return projectConversations.filter((conversation) => !conversation.pinned && !sectionByThread.has(conversation.id) && isWorkspaceConversation(conversation)
      && opened.has(workspaceGroupIdentity(conversationWorkspacePath(conversation))));
  }, [projectConversations, sidebarProjects, sectionByThread]);
  const allProjectGroups = useMemo(() => groupByWorkspace(workspaceConversations, sidebarProjects.map((workspace) => workspace.path),
    new Map(recentWorkspaces.filter(project => project.named).map(project => [workspaceGroupIdentity(project.path), project.name]))), [workspaceConversations, sidebarProjects, recentWorkspaces]);
  const projectGroups = useMemo(() => new Map([...allProjectGroups].filter(([, group]) => !sectionProjectKeys.has(workspaceGroupIdentity(group.path)))), [allProjectGroups, sectionProjectKeys]);
  const allOrderedProjectGroups = useMemo(() => (
    orderSidebarItems(Array.from(allProjectGroups.entries(), ([projectKey, group]) => ({
      projectKey,
      group,
      conversations: sidebarSortMode === "manual"
        ? orderSidebarItems(orderConversationTree(group.items), sidebarOrder?.project_thread_orders[projectKey] ?? [], item => item.id)
        : orderConversationTree([...group.items].sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || ""))),
    })), sidebarOrder?.project_order ?? [], item => item.projectKey)
  ), [allProjectGroups, sidebarOrder, sidebarSortMode]);
  const orderedProjectGroups = useMemo(() => allOrderedProjectGroups.filter(({ projectKey }) => !sectionProjectKeys.has(projectKey)), [allOrderedProjectGroups, sectionProjectKeys]);
  const pinnedConversations = useMemo(() => {
    const positions = new Map(pinnedConversationIds.map((identifier, index) => [identifier, index]));
    return filtered.filter(conversation => conversation.pinned)
      .sort((left, right) => pinnedConversationSortMode === "updated_at"
        ? (right.updatedAt || "").localeCompare(left.updatedAt || "")
        : (positions.get(left.id) ?? pinnedConversationIds.length) - (positions.get(right.id) ?? pinnedConversationIds.length)
          || (left.pinnedAt || "").localeCompare(right.pinnedAt || ""))
      .map(conversation => ({ ...conversation, treeDepth: 0 }));
  }, [filtered, pinnedConversationIds, pinnedConversationSortMode]);
  const projectOptions = useMemo(() => {
    const groups = groupByWorkspace([], recentWorkspaces.map(workspace => workspace.path));
    return recentWorkspaces.map(workspace => ({ id: workspaceGroupIdentity(workspace.path),
      name: workspace.named ? workspace.name : groups.get(workspaceGroupIdentity(workspace.path))!.label }));
  }, [recentWorkspaces]);
  const recentConversations = useMemo(() => {
    const recent = filtered.filter(conversation => !conversation.pinned && !sectionByThread.has(conversation.id)).sort((a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || ""));
    return (sidebarSortMode === "manual" ? orderSidebarItems(recent, sidebarOrder?.recent_thread_ids ?? [], item => item.id) : recent)
      .slice(0, recentLimit).map((conversation) => ({ ...conversation, treeDepth: 0 }));
  }, [filtered, recentLimit, sidebarOrder, sidebarSortMode, sectionByThread]);
  const activityGroups = useMemo(() => {
    if (!activityView) return [];
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const groups: { key: string; label: string; items: TreeConversation[] }[] = [
      { key: "priority", label: tr("优先级"), items: [] },
      { key: "today", label: tr("今天"), items: [] },
      { key: "yesterday", label: tr("昨天"), items: [] },
      { key: "earlier", label: tr("更早"), items: [] },
    ];
    const ordered = [...filtered].sort((a, b) =>
      Number(b.sessionStatus !== "idle") - Number(a.sessionStatus !== "idle")
      || new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
    for (const conversation of ordered.slice(0, recentLimit)) {
      const date = new Date(conversation.updatedAt).toDateString();
      const group = conversation.sessionStatus !== "idle" ? 0
        : date === today.toDateString() ? 1 : date === yesterday.toDateString() ? 2 : 3;
      groups[group].items.push({ ...conversation, treeDepth: 0 });
    }
    return groups.filter((group) => group.items.length > 0);
  }, [activityView, filtered, recentLimit, tr]);
  const conversationsById = useMemo(
    () => new Map(conversations.map((conversation) => [conversation.id, conversation])),
    [conversations],
  );
  const visibleRows = useMemo(() => activityView
    ? activityGroups.flatMap(group => group.items.map(item => ({ id: item.id, rowId: `activity-${group.key}:${item.id}` })))
    : [
      ...sections.flatMap(section => collapsedGroups.has(`section:${section.id}`) ? [] : section.item_keys.flatMap(key => {
        if (key.startsWith("thread:")) return filtered.some(item => item.id === key.slice(7)) ? [{ id: key.slice(7), rowId: `section-${section.id}:${key.slice(7)}` }] : [];
        const project = allOrderedProjectGroups.find(group => group.projectKey === workspaceGroupIdentity(key.slice(8)));
        return project && !collapsedGroups.has(project.projectKey)
          ? (expandedProjects.has(project.projectKey) ? project.conversations : project.conversations.slice(0, 5))
            .map(item => ({ id: item.id, rowId: `project:${item.id}` })) : [];
      })),
      ...pinnedConversations.map(item => ({ id: item.id, rowId: `pinned:${item.id}` })),
      ...orderedProjectGroups.filter(group => !collapsedGroups.has(group.projectKey)).flatMap(group =>
        (expandedProjects.has(group.projectKey) ? group.conversations : group.conversations.slice(0, 5)).map(item => ({ id: item.id, rowId: `project:${item.id}` }))),
      ...(showRecent ? recentConversations.map(item => ({ id: item.id, rowId: `recent:${item.id}` })) : []),
    ], [activityView, activityGroups, sections, collapsedGroups, filtered, allOrderedProjectGroups, expandedProjects,
      pinnedConversations, orderedProjectGroups, showRecent, recentConversations]);
  const pullRequestStatuses = useThreadPullRequestStatuses(visibleRows.map(row => conversationsById.get(row.id)!), isConnected);
  const conversationsRef = useRef(conversations);
  // Read the active id through a ref so switching chats does not recreate the
  // row renderer and force every session row to rerender.
  const conversationIdRef = useRef(conversationId);
  const selectedRowIdRef = useRef<string | null>(null);
  const renameStateRef = useRef({ renaming, renameValue });
  const onNavigateRef = useRef(onNavigate);
  conversationsRef.current = conversations;
  conversationIdRef.current = pendingConversationSwitchId || conversationId;
  // A thread can be listed under its project and Recents. Selection routes
  // to the occurrence that opened it (sidebarThreadLocationId).
  // A new/restored thread starts in its visible project row, or in Recents.
  const selectedConversationId = conversationIdRef.current;
  const visibleProject = !activityView && allOrderedProjectGroups.some(({ projectKey, conversations: projectConversations }) =>
    !sections.some(section => collapsedGroups.has(`section:${section.id}`)
      && section.item_keys.some(key => key.startsWith("project:") && workspaceGroupIdentity(key.slice(8)) === projectKey))
    && !collapsedGroups.has(projectKey)
    && (expandedProjects.has(projectKey) ? projectConversations : projectConversations.slice(0, 5))
      .some((item) => item.id === selectedConversationId));
  const defaultSelectedRowId = activityView
    ? activityGroups.flatMap((group) => group.items.some((item) => item.id === selectedConversationId)
      ? [`activity-${group.key}:${selectedConversationId}`] : [])[0] ?? null
    : sectionByThread.has(selectedConversationId) ? `section-${sectionByThread.get(selectedConversationId)}:${selectedConversationId}`
      : pinnedConversations.some(item => item.id === selectedConversationId) ? `pinned:${selectedConversationId}`
      : visibleProject ? `project:${selectedConversationId}`
      : showRecent && recentConversations.some((item) => item.id === selectedConversationId) ? `recent:${selectedConversationId}` : null;
  selectedRowIdRef.current = selectedRow?.conversationId === selectedConversationId && !activityView
    && (pinnedConversations.some(item => item.id === selectedConversationId) ? selectedRow.rowId.startsWith("pinned:")
      : selectedRow.rowId.startsWith("project:") || selectedRow.rowId.startsWith("recent:") || selectedRow.rowId.startsWith("section-"))
    ? selectedRow.rowId : defaultSelectedRowId;
  renameStateRef.current = { renaming, renameValue };
  onNavigateRef.current = onNavigate;

  useEffect(() => {
    setSelectedRow((previous) => previous?.conversationId === selectedConversationId ? previous : null);
  }, [selectedConversationId]);

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

  const archiveConversation = useCallback(async (id: string, archived: boolean) => {
    const state = useAppStore.getState();
    const owner = state.conversations.find((conversation) => conversation.id === id)!;
    const running = owner.sessionStatus === "running" || owner.sessionStatus === "waiting" || isConversationRunning({
      conversationId: id, activeConversationId: state.conversationId, activeIsStreaming: state.isStreaming,
      conversationStreaming: state.conversationStreaming,
    });
    if (archived && running && !await showConfirm({ title: tr("停止并归档此聊天？"),
      message: tr("归档会停止所有正在进行的工作。你可以稍后在设置中恢复该聊天。"), confirmLabel: tr("停止并归档"), danger: true })) return;
    try {
      const result = await setConversationArchived(id, archived);
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
  }, [tr]);

  const handleSwitch = useCallback((id: string, rowId: string) => {
    setMenuFor(null);
    setSelectedRow({ conversationId: id, rowId });
    requestConversationSwitch(id);
    onNavigateRef.current?.();
  }, [requestConversationSwitch]);

  useEffect(() => {
    const select = (event: Event) => {
      const { index, conversationId: owner } = (event as CustomEvent<{ index: number; conversationId: string | null }>).detail;
      if (useAppStore.getState().conversationId !== owner) return;
      const seen = new Set<string>();
      const target = visibleRows.filter(row => { if (seen.has(row.id)) return false; seen.add(row.id); return true; })[index];
      if (target) handleSwitch(target.id, target.rowId);
    };
    window.addEventListener("select-numbered-chat", select);
    return () => window.removeEventListener("select-numbered-chat", select);
  }, [visibleRows, handleSwitch]);

  const handoffWorktree = useCallback(async (id: string, target: "local" | "worktree") => {
    const taskName = conversationsRef.current.find(conversation => conversation.id === id)?.title || id;
    await handoffConversationWorktree(id, taskName, target);
  }, []);
  const copyConversationPath = useCallback((path?: string) => {
    if (!path) return;
    setMenuFor(null);
    void copyText(path, tr("工作区路径"), tr("已复制工作区路径"));
  }, [tr]);
  const startRename = useCallback((id: string, currentTitle: string, rowId = id) => {
    renameStateRef.current = { renaming: id, renameValue: currentTitle };
    setRenaming(id);
    setRenamingRow(rowId);
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
    const title = current.renameValue;
    if (!current.renaming) return;
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
  const toggleProjectExpansion = useCallback((key: string) => {
    setExpandedProjects((current) => {
      const next = new Set(current);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }, []);
  const addProject = async () => {
    useAppStore.setState({ projectEditTarget: "create" });
    onNavigateRef.current?.();
  };
  const copyConversationLink = useCallback((id: string) => {
    void copyText(`minicode://conversation?conversation_id=${encodeURIComponent(id)}`, tr("复制深度链接"));
  }, [tr]);
  const setPinned = useCallback((id: string, pinned: boolean) => {
    void runMenuCommand({ type: "conversation.pin.set", conversation_id: id, pinned });
  }, []);
  const setReadState = useCallback((id: string, hasUnreadTurn: boolean) => {
    void runMenuCommand({ type: "conversation.read_state.set", conversation_id: id, has_unread_turn: hasUnreadTurn });
  }, []);
  const setProject = useCallback((id: string, projectId: string) => {
    void runMenuCommand({ type: "conversation.project.set", conversation_id: id, project_id: projectId });
  }, []);
  const copyConversationMarkdown = useCallback(async (id: string) => {
    const state = useAppStore.getState();
    const owner = state.conversations.find((conversation) => conversation.id === id)!;
    const captured = { id: owner.id, title: owner.title, workspaceRoot: owner.executionCwd || owner.worktreePath || owner.workspaceRoot || "",
      messages: state.getVisibleMessages(owner.id), historyPage: state.conversationHistoryPages[owner.id],
      sessionId: getWebSocket()?.sessionId ?? "", messageCount: owner.messageCount };
    try {
      const { loadConversationMarkdown } = await import("../chat/conversationMarkdown");
      const markdown = await loadConversationMarkdown(captured);
      if (markdown !== null) await copyText(markdown, tr("复制为 Markdown"));
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    }
  }, [tr]);

  const startWorkspaceConversation = (projectKey: string, group: WorkspaceConversationGroup) => {
    const project = recentWorkspaces.find(project => project.path === group.path);
    const workspaceRoot = project ? projectExecutionRoot(project) : group.path;
    if (!workspaceRoot) return;
    if (collapsedGroupsRef.current.has(projectKey)) {
      const next = new Set(collapsedGroupsRef.current);
      next.delete(projectKey);
      collapsedGroupsRef.current = next;
      setCollapsedGroups(next);
      scheduleConversationUiStatePersist(next, listRef.current?.scrollTop ?? 0);
    }
    startConversationDraft({ bindWorkspace: true, workspaceRoot, appMode: "cowork" });
    onNavigate?.();
  };

  const renderSessionRow = useCallback((conversation: TreeConversation, scene = "project") => (
    <SessionRow
      key={`${scene}:${conversation.id}`}
      rowId={`${scene}:${conversation.id}`}
      conversation={conversationsById.get(conversation.id) ?? conversation}
      sessionStatus={conversation.sessionStatus}
      active={`${scene}:${conversation.id}` === selectedRowIdRef.current}
      menuOpen={menuFor === `${scene}:${conversation.id}`}
      renaming={renaming === conversation.id && renamingRow === `${scene}:${conversation.id}`}
      renameValue={renaming === conversation.id ? renameValue : ""}
      waitingLabel={conversation.waitingLabel}
      onSwitch={handleSwitch}
      onSetMenuFor={setMenuFor}
      onStartRename={startRename}
      onCommitRename={commitRename}
      onCancelRename={cancelRename}
      onSetRenameValue={handleRenameValueChange}
      onArchive={archiveConversation}
      onCopyLink={copyConversationLink}
      onCopyMarkdown={copyConversationMarkdown}
      onHandoff={handoffWorktree}
      onCopy={copyConversationPath}
      onPin={setPinned} onReadState={setReadState} onProjectChange={setProject}
      projectOptions={projectOptions}
      sectionOptions={sections} currentSection={sectionByThread.get(conversation.id)} onSectionChange={moveToSection}
      onNewSection={createSectionForThread}
      pullRequest={pullRequestStatuses[conversation.id]?.pr}
      currentProject={projectMemberships.get(conversation.id) ?? ""}
      treeDepth={conversation.treeDepth}
    />
  ), [
    archiveConversation,
    cancelRename,
    copyConversationLink,
    copyConversationMarkdown,
    commitRename,
    conversationsById,
    copyConversationPath,
    handleRenameValueChange,
    handleSwitch,
    handoffWorktree,
    menuFor,
    renameValue,
    renaming,
    renamingRow,
    startRename,
    setPinned, setReadState, setProject, projectOptions, projectMemberships, sections, sectionByThread, pullRequestStatuses,
    moveToSection, createSectionForThread,
  ]);

  const selectSortMode = async (mode: "manual" | "updated_at") => {
    const pending = { mode };
    setPendingSortMode(pending);
    try {
      await runMenuCommand({ type: "sidebar.sort.set", mode,
        project_thread_seeds: Object.fromEntries(orderedProjectGroups.slice(0, 5).map(({ group, conversations: items }) =>
          [group.path, items.slice(0, 5).map(item => item.id)])) });
    } finally { setPendingSortMode(current => current === pending ? null : current); }
  };
  const renderSortableThreads = (items: TreeConversation[], scope: "project" | "recent", projectId?: string) => {
    const byId = new Map(items.map(item => [item.id, item]));
    return <SidebarSortableList identifiers={items.map(item => item.id)} scope={scope}
      ariaLabel={scope === "project" ? tr("项目中的聊天") : tr("最近聊天")}
      disabled={sidebarSortMode !== "manual" || pendingSortMode !== null} renamingId={renaming}
      renderRow={(identifier, binding) => cloneElement(renderSessionRow(byId.get(identifier)!, scope), binding)}
      renderOverlay={identifier => renderSessionRow(byId.get(identifier)!, `${scope}-overlay`)}
      onDragStart={() => setMenuFor(null)} onReorder={async (movedId, beforeId, visibleIds) => Boolean(await runMenuCommand({
        type: projectId === undefined ? "sidebar.recent.reorder" : "sidebar.project.thread.reorder",
        ...(projectId === undefined ? {} : { project_id: projectId }),
        moved_conversation_id: movedId, before_conversation_id: beforeId, visible_conversation_ids: visibleIds,
      }))} />;
  };
  const renderWorkspaceHeader = (projectKey: string, group: WorkspaceConversationGroup, binding?: SidebarDragBinding, overlay = false) => (
    <div className="mc-workspace-group-header" data-active={!(pendingConversationSwitchId || conversationId) && workspaceGroupIdentity(group.path) === workspaceGroupIdentity(workingDirectory) ? "true" : undefined}
      onContextMenu={overlay ? undefined : event => { event.preventDefault(); setWorkspaceMenu({ path: group.path, x: event.clientX, y: event.clientY }); }}>
      <button type="button" ref={binding?.dragActivatorRef} {...binding?.dragAttributes} aria-label={group.label}
        aria-expanded={!collapsedGroups.has(projectKey)} onClick={() => toggleGroup(projectKey)}
        className="mc-workspace-group-toggle" style={taskSectionHeaderStyle}>
        <span className="mc-workspace-folder-icon" aria-hidden="true">{recentWorkspaces.find(project => project.path === group.path)?.appearance
          ? <ProjectMarkerIcon appearance={recentWorkspaces.find(project => project.path === group.path)!.appearance} size={16} /> : collapsedGroups.has(projectKey)
          ? <Folder className="mc-workspace-folder-glyph" size={16} data-testid={`workspace-folder-closed-${projectKey}`} />
          : <FolderOpen className="mc-workspace-folder-glyph" size={16} data-testid={`workspace-folder-open-${projectKey}`} />}</span>
        <span className="mc-workspace-label" title={group.path}>{group.label}</span>
      </button>
      {!overlay && <div className="mc-workspace-actions" data-menu-open={workspaceMenu?.path === group.path || undefined}>
      <button type="button" className="mc-workspace-menu" aria-label={tr("项目操作：{name}", { name: group.label })}
        title={tr("项目操作：{name}", { name: group.label })} aria-haspopup="menu" aria-expanded={workspaceMenu?.path === group.path}
        onClick={event => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setWorkspaceMenu(workspaceMenu?.path === group.path ? null : { path: group.path, x: bounds.left, y: bounds.bottom + 4, anchor: event.currentTarget });
        }}><MoreHorizontal size={16} aria-hidden="true" /></button>
      <button type="button" className="mc-workspace-new-session" aria-label={tr("在 {name} 中新建任务", { name: group.label })}
        title={tr("在 {name} 中新建任务", { name: group.label })} data-testid={`workspace-new-session-${projectKey}`}
        onClick={() => startWorkspaceConversation(projectKey, group)}><SquarePen size={15} aria-hidden="true" /></button>
      </div>}
    </div>
  );
  const renderWorkspaceGroup = (projectKey: string, group: WorkspaceConversationGroup, orderedConversations: TreeConversation[], binding?: SidebarDragBinding) => (
    <section aria-label={tr("工作区 {name}", { name: group.label })} style={taskSectionStyle}>
      {renderWorkspaceHeader(projectKey, group, binding)}
      {!collapsedGroups.has(projectKey) && <div className="mc-workspace-group-body">
        <div className="mc-workspace-group-body-inner" style={taskSectionBodyStyle}>
          {renderSortableThreads(expandedProjects.has(projectKey) ? orderedConversations : orderedConversations.slice(0, 5), "project", group.path)}
          {orderedConversations.length > 5 && <button type="button" className="mc-sidebar-more-recents mc-sidebar-more-project-sessions"
            aria-expanded={expandedProjects.has(projectKey)} onClick={() => toggleProjectExpansion(projectKey)}>
            {expandedProjects.has(projectKey) ? tr("收起") : tr("展开显示")}</button>}
        </div>
      </div>}
    </section>
  );
  const sectionThreadIds = (section: SidebarSection) => [...new Set(section.item_keys.flatMap(key => key.startsWith("thread:")
    ? filtered.some(item => item.id === key.slice(7)) ? [key.slice(7)] : []
    : allProjectGroups.get(workspaceGroupIdentity(key.slice(8)))?.items.map(item => item.id) ?? []))];
  const projectGroupsById = new Map(orderedProjectGroups.map(group => [group.projectKey, group]));
  const isEmpty = activityView ? filtered.length === 0
    : !hasPinnedFiles && pinnedConversations.length === 0 && projectGroups.size === 0 && sections.length === 0 && (!showRecent || recentConversations.length === 0);

  return (
    <>
      <div className="mc-sidebar-project-heading" style={{ ...sectionHeaderRowStyle, padding: "9px 10px 5px" }}>
        <span className="mc-sidebar-project-label">{activityView ? tr("活动") : tr("项目")}</span>
        {!activityView && <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <SidebarSortMenu section="projects" mode={sidebarSortMode} onSelect={mode => { void selectSortMode(mode); }} onNewSection={() => { void editSection(); }} />
          <button
          type="button"
          className="btn-ghost mc-icon-button mc-sidebar-project-add"
          aria-label={tr("添加项目")}
          title={tr("添加项目")}
          onClick={() => void addProject()}
        ><Plus size={15} aria-hidden="true" /></button></div>}
      </div>
      <div
        ref={listRef}
        data-testid="conversation-list"
        style={sessionListWrapStyle}
        onScroll={handleConversationListScroll}
      >
        {!activityView && <SidebarSections sections={sections} collapsed={collapsedGroups} onToggle={toggleGroup}
          onNew={() => { void editSection(); }} onEdit={section => { void editSection(section); }}
          onRemove={id => { void sectionAction(`/sections/${id}`, undefined, "DELETE"); }}
          onRead={section => { for (const id of sectionThreadIds(section)) setReadState(id, false); }}
          onArchive={section => { void (async () => {
            if (!await showConfirm({ title: tr("归档分组中的聊天？"), message: section.name, confirmLabel: tr("归档") })) return;
            for (const id of sectionThreadIds(section)) {
              const result = await setConversationArchived(id, true);
              if (!wsOutbox.commandResultSucceeded(result)) throw new Error(result.message);
            }
          })().catch(error => pushToast(String(error), "error")); }}
          onNewChat={section => {
            startConversationDraft({ bindWorkspace: false, appMode: "cowork", sidebarSectionId: section.id });
          }}
          visibleItemKeys={section => section.item_keys.filter(key => key.startsWith("thread:")
            ? filtered.some(item => item.id === key.slice(7))
            : [...allProjectGroups.values()].some(group => workspaceGroupIdentity(group.path) === workspaceGroupIdentity(key.slice(8))))}
          renderItem={(section, key, binding) => {
            if (key.startsWith("thread:")) {
              const item = filtered.find(conversation => conversation.id === key.slice(7));
              return item ? cloneElement(renderSessionRow({ ...item, treeDepth: 0 }, `section-${section.id}`), binding) : null;
            }
            const match = allOrderedProjectGroups.find(item => item.projectKey === workspaceGroupIdentity(key.slice(8)));
            return match ? renderWorkspaceGroup(match.projectKey, match.group, match.conversations, binding) : null;
          }} onReorder={async (section, moved, before, visible) => (await sectionAction("/sections/reorder", {
            section_id: section?.id ?? null, moved_id: moved, before_id: before, visible_ids: visible,
          })) !== undefined} />}
        {isEmpty && <EmptyState
          icon={<SquarePen size={22} />}
          title={activityView ? tr("暂无任务活动") : tr("开始你的第一个任务")}
          action={<button type="button" onClick={() => {
            const state = useAppStore.getState();
            startConversationDraft({ appMode: "cowork", bindWorkspace: state.appMode === "code" && Boolean(state.workingDirectory) });
            onNavigate?.();
          }}>{tr("新建任务")}</button>}
        />}
        {!activityView && (pinnedConversations.length > 0 || hasPinnedFiles) && <section aria-label={tr("已固定")} className="mc-sidebar-pinned">
          <div className="mc-sidebar-project-heading"><span className="mc-sidebar-project-label">{tr("已固定")}</span></div>
          <PinnedWorkspaceFiles />
          <PinnedConversationList conversations={pinnedConversations} renderRow={renderSessionRow} renamingId={renaming}
            onDragStart={() => setMenuFor(null)} onReorder={async (movedId, beforeId, visibleIds) => Boolean(await runMenuCommand({
              type: "conversation.pin.reorder", moved_conversation_id: movedId, before_conversation_id: beforeId, visible_conversation_ids: visibleIds,
            }))} />
        </section>}
        {!activityView && <SidebarSortableList identifiers={orderedProjectGroups.map(item => item.projectKey)} scope="projects" ariaLabel={tr("项目顺序")}
          onDragStart={() => { setMenuFor(null); setWorkspaceMenu(null); }}
          onReorder={async (movedId, beforeId, visibleIds) => Boolean(await runMenuCommand({ type: "sidebar.project.reorder",
            moved_project_id: projectGroupsById.get(movedId)!.group.path,
            before_project_id: beforeId === null ? null : projectGroupsById.get(beforeId)!.group.path,
            visible_project_ids: visibleIds.map(identifier => projectGroupsById.get(identifier)!.group.path),
          }))}
          renderOverlay={identifier => {
            const { projectKey, group } = projectGroupsById.get(identifier)!;
            return renderWorkspaceHeader(projectKey, group, undefined, true);
          }} renderRow={(identifier, binding) => {
            const { projectKey, group, conversations: orderedConversations } = projectGroupsById.get(identifier)!;
            return renderWorkspaceGroup(projectKey, group, orderedConversations, binding);
          }} />}


        {activityView && activityGroups.map((group) => <section key={group.key} aria-label={group.label} className="mc-sidebar-activity-group">
          <div className="mc-sidebar-project-heading"><span className="mc-sidebar-project-label">{group.label}</span></div>
          {group.items.map((conversation) => {
            return <div key={conversation.id} className="mc-sidebar-activity-task">
              {renderSessionRow(conversation, `activity-${group.key}`)}
              <ActivitySummary summary={conversation.summary} />
            </div>;
          })}
        </section>)}
        {activityView && filtered.length > recentLimit && <button type="button" className="mc-sidebar-more-recents" onClick={() => setRecentLimit((count) => count + 20)}>{tr("显示更多")}</button>}
        {!activityView && showRecent && recentConversations.length > 0 && <section aria-label={tr("最近")} className="mc-sidebar-recents">
          <div className="mc-sidebar-project-heading"><span className="mc-sidebar-project-label">{tr("最近")}</span>
            <SidebarSortMenu section="chats" mode={sidebarSortMode} onSelect={mode => { void selectSortMode(mode); }} onNewSection={() => { void editSection(); }} /></div>
          <div className="mc-sidebar-recent-list">{renderSortableThreads(recentConversations, "recent")}</div>
          {filtered.filter(conversation => !conversation.pinned).length > recentLimit && <button type="button" className="mc-sidebar-more-recents" onClick={() => setRecentLimit((count) => count + 20)}>{tr("显示更多")}</button>}
        </section>}
      </div>
      {workspaceMenu && <ProjectContextMenu path={workspaceMenu.path} position={workspaceMenu} anchor={workspaceMenu.anchor} onClose={() => setWorkspaceMenu(null)} />}
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
