import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Circle,
  Loader,
  Pause,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop, revealPath } from "../desktop/runtime";
import type { ConversationMeta, SessionFilter } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import {
  hasRuntimePendingUserActionForConversation,
  runtimePendingUserActionLabelForConversation,
} from "../lib/runtime-session";
import { SectionTitle } from "./sidebarComponents";
import { SessionRow } from "./SessionRow";
import {
  sectionHeaderRowStyle,
  sectionMetaStyle,
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
  projectGroupStyle,
  projectHeaderStyle,
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
  { id: "all", label: "All", icon: null },
  { id: "running", label: "Running", icon: <Loader size={10} className="spin" /> },
  { id: "waiting", label: "Waiting", icon: <Pause size={10} /> },
  { id: "idle", label: "Idle", icon: <Circle size={10} /> },
  { id: "archived", label: "Archived", icon: <Archive size={10} /> },
];

export type EnrichedConversation = ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" };

export interface WorkspaceConversationGroup {
  baseLabel: string;
  label: string;
  items: EnrichedConversation[];
}

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
    const basePath = c.workspaceRoot || c.worktreePath;
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
    const parts = workspacePathParts(first?.workspaceRoot || first?.worktreePath);
    let qualifier = key;
    for (let depth = 1; depth < parts.length; depth += 1) {
      const candidateQualifier = parts.slice(Math.max(0, parts.length - 1 - depth), -1).join("/");
      const isUnique = duplicateGroups.every(([candidateKey, candidate]) => {
        if (candidateKey === key) return true;
        const candidateFirst = candidate.items[0];
        const candidateParts = workspacePathParts(candidateFirst?.workspaceRoot || candidateFirst?.worktreePath);
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
  if (pendingAskUser) return <>Waiting for reply</>;
  if (pendingDiffReview) return <>Waiting for review</>;
  if (pendingApproval) return <>Waiting for {pendingApproval.toolName}</>;
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

  const [search, setSearch] = useState("");
  const [currentWorkspaceOnly, setCurrentWorkspaceOnly] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all");
  const initialUiState = useMemo(readConversationUiState, []);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(initialUiState.collapsedGroups);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());

  const waitingLabelForConversation = useCallback((id: string) => {
    if (id === conversationId) {
      if (pendingAskUser) return "Waiting for reply";
      if (pendingDiffReview) return "Waiting for review";
      if (pendingApproval) return `Waiting for ${pendingApproval.toolName}`;
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
        const conversationWorkspace = workspaceGroupIdentity(c.workspaceRoot || c.worktreePath || "");
        const conversationWorktree = workspaceGroupIdentity(c.worktreePath || "");
        return conversationWorkspace === currentWorkspacePath || conversationWorktree === currentWorkspacePath;
      });
    }
    if (sessionFilter === "archived") list = list.filter((c) => c.archived);
    else if (sessionFilter === "all") list = list.filter((c) => !c.archived);
    else list = list.filter((c) => !c.archived && c.sessionStatus === sessionFilter);
    return list;
  }, [enrichedConversations, search, currentWorkspaceOnly, currentWorkspacePath, sessionFilter]);

  const projectGroups = useMemo(() => groupByWorkspace(filtered), [filtered]);
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

  const deleteConversation = (id: string) => {
    const conversation = conversations.find((c) => c.id === id);
    if (!conversation) return;
    onSetConfirmDialog({
      title: "Delete session",
      message: conversation.gitIsolated
        ? "Delete this protected session and remove its separate workspace?"
        : "Delete this session? This removes it from the session list.",
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () => {
        sendClientCommand({ type: "conversation.delete", conversation_id: id, cleanup_worktree: Boolean(conversation.gitIsolated) });
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
        `Delete ${deletable.length} session${deletable.length === 1 ? "" : "s"}? This removes them from the session list.`,
        isolatedCount > 0 ? `${isolatedCount} protected workspace${isolatedCount === 1 ? "" : "s"} will also be cleaned up.` : "",
        skipped > 0 ? `${skipped} running or waiting session${skipped === 1 ? "" : "s"} will be skipped.` : "",
      ].filter(Boolean).join("\n\n"),
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () => {
        for (const c of deletable) {
          sendClientCommand({ type: "conversation.delete", conversation_id: c.id, cleanup_worktree: Boolean(c.gitIsolated) });
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
      title: force ? "Force cleanup workspace" : "Clean up workspace",
      message: force
        ? "Force remove this protected session workspace and discard local changes?"
        : "Remove this protected session workspace? If it has local changes, MiniCode can ask for force cleanup later.",
      confirmLabel: force ? "Force cleanup" : "Clean up",
      danger: force,
      onConfirm: () => { sendClientCommand({ type: "conversation.worktree.cleanup", conversation_id: id, force }); },
    });
  };

  const archiveConversation = (id: string, archived: boolean) => {
    sendClientCommand({ type: archived ? "conversation.archive" : "conversation.unarchive", conversation_id: id, archived });
    useAppStore.setState((s) => ({ conversations: s.conversations.map((c) => (c.id === id ? { ...c, archived } : c)) }));
  };

  const handleSwitch = (id: string) => {
    setMenuFor(null);
    requestConversationSwitch(id);
    onNavigate?.();
  };

  const handoffWorktree = (id: string, target: "local" | "worktree") => {
    sendClientCommand({
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
    sendClientCommand({ type: "conversation.rename", conversation_id: renaming, title: renameValue.trim() });
    useAppStore.setState((s) => ({ conversations: s.conversations.map((c) => (c.id === renaming ? { ...c, title: renameValue.trim() } : c)) }));
    setRenaming(null);
  };
  const cancelRename = () => { setRenaming(null); };

  const relativeTime = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime();
    if (diff < 60_000) return "just now";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`;
    return `${Math.floor(diff / 86_400_000)}d`;
  };

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

  if (enrichedConversations.length === 0) return null;

  return (
    <>
      {selectionMode && (
        <div style={bulkBarStyle} role="toolbar" aria-label="Session selection actions">
          <span style={bulkMetaStyle} aria-live="polite">{selectedSessions.length} selected</span>
          <div style={bulkActionsStyle}>
            <button type="button" onClick={() => setAllFilteredSelected(!allFilteredSelected)} style={bulkActionStyle} disabled={selectableFiltered.length === 0}>
              {allFilteredSelected ? "Clear shown" : "Select shown"}
            </button>
            <button
              type="button"
              onClick={() => deleteSessionBatch(selectedSessions, "Delete selected sessions")}
              className="mc-icon-button mc-icon-button-danger"
              style={{ color: "var(--state-danger)" }}
              disabled={selectedSessions.length === 0}
              title="Delete selected sessions"
              aria-label="Delete selected sessions"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      )}

      <div style={{ ...sectionHeaderRowStyle, padding: "8px 10px 2px" }}>
        <SectionTitle label="Recents" />
        <span style={sectionMetaStyle}>{enrichedConversations.filter((c) => !c.archived).length}</span>
        <button
          type="button"
          onClick={() => { setSelectionMode((v) => !v); setSelectedSessionIds(new Set()); }}
          title={selectionMode ? "Cancel selection" : "Select sessions"}
          aria-label={selectionMode ? "Cancel selection" : "Select sessions"}
          style={recentsActionStyle(selectionMode)}
        >
          {selectionMode ? <X size={14} /> : <SlidersHorizontal size={13} />}
        </button>
      </div>
      <div style={{ ...searchBarWrapStyle, padding: "6px 6px 8px" }}>
        <input type="text" placeholder="Search sessions" value={search} onChange={(e) => setSearch(e.target.value)} style={searchInputStyle} />
      </div>

      {(currentWorkspacePath || sessionFilter !== "all" || filterCounts.running > 0 || filterCounts.waiting > 0 || filterCounts.archived > 0) && (
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
              Current workspace
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
            {search ? "No matches." : "No sessions with this filter."}
          </div>
        ) : (
          Array.from(projectGroups.entries()).map(([projectKey, group]) => (
            <div key={projectKey} style={projectGroupStyle}>
              {projectGroups.size > 1 && (
                <button onClick={() => toggleGroup(projectKey)} style={projectHeaderStyle}>
                  <span style={{ fontSize: 10, opacity: 0.7, display: "inline-flex" }}>
                    {collapsedGroups.has(projectKey) ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  </span>
                  {group.label}
                  <span style={projectCountStyle}>{group.items.length}</span>
                </button>
              )}
              {!collapsedGroups.has(projectKey) && (
                <div style={projectItemsStyle}>
                  {group.items.map((c) => (
                    <SessionRow
                      key={c.id}
                      conversation={c}
                      conversationId={conversationId}
                      selectionMode={selectionMode}
                      selectedSessionIds={selectedSessionIds}
                      menuFor={menuFor}
                      renaming={renaming}
                      renameValue={renameValue}
                      waitingLabelForConversation={waitingLabelForConversation}
                      relativeTime={relativeTime}
                      onSwitch={handleSwitch}
                      onToggleSelected={toggleSessionSelected}
                      onSetMenuFor={setMenuFor}
                      onStartRename={startRename}
                      onCommitRename={commitRename}
                      onCancelRename={cancelRename}
                      onSetRenameValue={setRenameValue}
                      onArchive={archiveConversation}
                      onDelete={deleteConversation}
                      onCleanup={cleanupWorktree}
                      onHandoff={handoffWorktree}
                      onReveal={revealConversationPath}
                      onCopy={copyConversationPath}
                    />
                  ))}
                </div>
              )}
            </div>
          ))
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
