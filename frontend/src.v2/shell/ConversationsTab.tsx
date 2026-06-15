import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Circle,
  Loader,
  MoreHorizontal,
  Pause,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop, revealPath } from "../desktop/runtime";
import type { ConversationMeta, SessionFilter } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { workspaceDisplayName } from "../lib/workspace-display";
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
  sessionFooterStyle,
} from "./sidebarStyles";
import { isConversationRunning } from "./sessionStatus";

type SidebarTab = "conversations" | "files";

const SESSION_FILTERS: { id: SessionFilter; label: string; icon: React.ReactNode }[] = [
  { id: "all", label: "All", icon: null },
  { id: "running", label: "Running", icon: <Loader size={10} className="spin" /> },
  { id: "waiting", label: "Waiting", icon: <Pause size={10} /> },
  { id: "idle", label: "Idle", icon: <Circle size={10} /> },
  { id: "archived", label: "Archived", icon: <Archive size={10} /> },
];

function groupByWorkspace(conversations: (ConversationMeta & { sessionStatus: string })[]): Map<string, (ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" })[]> {
  const groups = new Map<string, (ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" })[]>();
  for (const c of conversations) {
    const basePath = c.workspaceRoot || c.worktreePath;
    const key = workspaceDisplayName(basePath, "Computer");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(c as ConversationMeta & { sessionStatus: "running" | "waiting" | "idle" });
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
  onSetConfirmDialog,
}: {
  conversationId: string;
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

  const [search, setSearch] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
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

  const filtered = useMemo(() => {
    let list = enrichedConversations;
    if (search) {
      const q = search.toLowerCase();
      list = list.filter((c) => c.title.toLowerCase().includes(q) || c.workspaceRoot?.toLowerCase().includes(q) || c.gitBranch?.toLowerCase().includes(q));
    }
    if (sessionFilter === "archived") list = list.filter((c) => c.archived);
    else if (sessionFilter === "all") list = list.filter((c) => !c.archived);
    else list = list.filter((c) => !c.archived && c.sessionStatus === sessionFilter);
    return list;
  }, [enrichedConversations, search, sessionFilter]);

  const projectGroups = useMemo(() => groupByWorkspace(filtered), [filtered]);
  const selectedSessions = useMemo(() => enrichedConversations.filter((c) => selectedSessionIds.has(c.id)), [enrichedConversations, selectedSessionIds]);
  const selectableFiltered = useMemo(() => filtered.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting"), [filtered]);

  useEffect(() => {
    if (!menuFor) return;
    const close = () => setMenuFor(null);
    const closeOnEsc = (e: KeyboardEvent) => { if (e.key === "Escape") { close(); } };
    window.addEventListener("click", close);
    window.addEventListener("keydown", closeOnEsc);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", closeOnEsc); };
  }, [menuFor]);

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

  const handleSwitch = (id: string) => { setMenuFor(null); requestConversationSwitch(id); };
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

  const toggleGroup = (key: string) => { setCollapsedGroups((prev) => { const next = new Set(prev); next.has(key) ? next.delete(key) : next.add(key); return next; }); };

  const statusDot = (status: string) => {
    const color = status === "running" ? "var(--state-info)" : status === "waiting" ? "var(--state-warning)" : "var(--text-muted)";
    return (
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, flexShrink: 0, boxShadow: status === "running" ? `0 0 4px ${color}` : "none", animation: status === "running" ? "thinking-pulse 1.5s ease-in-out infinite" : "none" }} />
    );
  };

  const runningCount = filterCounts.running || 0;

  return (
    <>
      {selectionMode && (
        <div style={bulkBarStyle}>
          <button type="button" onClick={() => setAllFilteredSelected(selectedSessionIds.size < selectableFiltered.length)} style={bulkActionStyle} disabled={selectableFiltered.length === 0}>
            {selectedSessionIds.size < selectableFiltered.length ? "Select visible" : "Clear"}
          </button>
          <span style={bulkMetaStyle}>{selectedSessions.length} selected</span>
          <button type="button" onClick={() => deleteSessionBatch(selectedSessions, "Delete selected sessions")} style={{ ...bulkActionStyle, color: "var(--state-danger)" }} disabled={selectedSessions.length === 0}>
            Delete
          </button>
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
          <SlidersHorizontal size={13} />
        </button>
      </div>
      <div style={{ ...searchBarWrapStyle, padding: "6px 6px 8px" }}>
        <input type="text" placeholder="Search sessions" value={search} onChange={(e) => setSearch(e.target.value)} style={searchInputStyle} />
      </div>

      <div style={{ ...filterRowStyle, padding: "0 6px 8px" }}>
        {SESSION_FILTERS.map((f) => {
          const count = filterCounts[f.id] || 0;
          return (
            <button key={f.id} onClick={() => setSessionFilter(f.id)}
              style={{ ...filterButtonStyle, background: sessionFilter === f.id ? "var(--accent-soft)" : "transparent", color: sessionFilter === f.id ? "var(--accent-primary)" : "var(--text-muted)", fontWeight: sessionFilter === f.id ? 600 : 500 }}>
              {f.icon}{f.label}{count > 0 && <span style={filterCountStyle}>{count}</span>}
            </button>
          );
        })}
      </div>

      <div style={sessionListWrapStyle}>
        {filtered.length === 0 ? (
          <div style={emptyStateStyle}>
            {search ? "No matches." : sessionFilter !== "all" ? "No sessions with this status." : "No sessions yet."}
          </div>
        ) : (
          Array.from(projectGroups.entries()).map(([project, items]) => (
            <div key={project} style={projectGroupStyle}>
              {projectGroups.size > 1 && (
                <button onClick={() => toggleGroup(project)} style={projectHeaderStyle}>
                  <span style={{ fontSize: 10, opacity: 0.7, display: "inline-flex" }}>
                    {collapsedGroups.has(project) ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  </span>
                  {project}
                  <span style={projectCountStyle}>{items.length}</span>
                </button>
              )}
              {!collapsedGroups.has(project) && (
                <div style={projectItemsStyle}>
                  {items.map((c) => (
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
                      statusDot={statusDot}
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

      <div style={sessionFooterStyle}>
        <span>{enrichedConversations.filter((c) => !c.archived).length} sessions</span>
        {runningCount > 0 && <span style={{ color: "var(--state-info)", display: "flex", alignItems: "center", gap: 3 }}><Loader size={10} /> {runningCount} active</span>}
        <span style={{ flex: 1 }} />
        {enrichedConversations.filter((c) => c.archived).length > 0 && <span>{enrichedConversations.filter((c) => c.archived).length} archived</span>}
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
