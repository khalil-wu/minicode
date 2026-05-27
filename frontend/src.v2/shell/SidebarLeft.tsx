import { useEffect, useMemo, useState } from "react";
import {
  Archive,
  Box,
  ChevronDown,
  ChevronRight,
  Circle,
  Code2,
  Copy,
  FolderOpen,
  GitBranch,
  Loader,
  LogIn,
  MoreHorizontal,
  Pause,
  Plus,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  XCircle,
} from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop, revealPath } from "../desktop/runtime";
import { FileTree } from "./FileTree";
import type { ConversationMeta, SessionFilter } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { branchDisplayName, canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";

type SidebarTab = "conversations" | "files";
type ConfirmDialogState =
  | {
      title: string;
      message: string;
      confirmLabel: string;
      danger?: boolean;
      onConfirm: () => void;
    }
  | null;

const SESSION_FILTERS: { id: SessionFilter; label: string; icon: React.ReactNode }[] = [
  { id: "all", label: "All", icon: null },
  { id: "running", label: "Running", icon: <Loader size={10} className="spin" /> },
  { id: "waiting", label: "Waiting", icon: <Pause size={10} /> },
  { id: "idle", label: "Idle", icon: <Circle size={10} /> },
  { id: "archived", label: "Archived", icon: <Archive size={10} /> },
];

const modeSwitchStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: 3,
  minHeight: 38,
  padding: 3,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 10px)",
};

const modeSwitchButtonStyle: React.CSSProperties = {
  height: 30,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 8px)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  letterSpacing: 0,
};

function groupByWorkspace(conversations: ConversationMeta[]): Map<string, ConversationMeta[]> {
  const groups = new Map<string, ConversationMeta[]>();
  for (const c of conversations) {
    const basePath = c.workspaceRoot || c.worktreePath;
    const key = workspaceDisplayName(basePath, "Current workspace");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(c);
  }
  return groups;
}

export const SidebarLeft = () => {
  const appMode = useAppStore((s) => s.appMode);
  const conversations = useAppStore((s) => s.conversations);
  const conversationId = useAppStore((s) => s.conversationId);
  const messages = useAppStore((s) => s.messages);
  const conversationMessages = useAppStore((s) => s.conversationMessages);
  const conversationStreaming = useAppStore((s) => s.conversationStreaming);
  const switchConversation = useAppStore((s) => s.switchConversation);
  const createConversation = useAppStore((s) => s.createConversation);
  const removeConversation = useAppStore((s) => s.removeConversation);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<SidebarTab>(appMode === "cowork" ? "conversations" : "files");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [sessionFilter, setSessionFilter] = useState<SessionFilter>("all");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());

  // Infer session status for conversations
  const enrichedConversations = useMemo(() => {
    const activeHasWaiting = Boolean(pendingApproval || pendingDiffReview || pendingAskUser);
    return conversations.map((c) => ({
      ...c,
      sessionStatus: (() => {
        const threadMessages = c.id === conversationId
          ? messages
          : conversationMessages[c.id] ?? [];
        const hasStreamingMessage = threadMessages.some((message) => message.isStreaming || message.isThinkingStreaming);
        const hasStreamingFlag = c.id === conversationId
          ? hasStreamingMessage
          : Boolean(conversationStreaming[c.id]) || hasStreamingMessage;
        if (hasStreamingFlag) return "running";
        if (c.id === conversationId && activeHasWaiting) return "waiting";
        return c.sessionStatus || "idle";
      })() as "running" | "waiting" | "idle",
    }));
  }, [
    conversations,
    conversationId,
    messages,
    conversationMessages,
    conversationStreaming,
    pendingApproval,
    pendingDiffReview,
    pendingAskUser,
  ]);

  const filterCounts = useMemo(() => {
    const counts: Record<string, number> = { all: 0, archived: 0, running: 0, waiting: 0, idle: 0 };
    for (const c of enrichedConversations) {
      if (c.archived) { counts.archived++; }
      else {
        counts.all++;
        counts[c.sessionStatus] = (counts[c.sessionStatus] || 0) + 1;
      }
    }
    return counts;
  }, [enrichedConversations]);

  const filtered = useMemo(() => {
    let list = enrichedConversations;

    // Search filter
    if (search) {
      const q = search.toLowerCase();
      list = list.filter((c) =>
        c.title.toLowerCase().includes(q) ||
        c.workspaceRoot?.toLowerCase().includes(q) ||
        c.gitBranch?.toLowerCase().includes(q)
      );
    }

    // Status filter
    if (sessionFilter === "archived") {
      list = list.filter((c) => c.archived);
    } else if (sessionFilter === "all") {
      list = list.filter((c) => !c.archived);
    } else {
      list = list.filter((c) => !c.archived && c.sessionStatus === sessionFilter);
    }

    return list;
  }, [enrichedConversations, search, sessionFilter]);

  const projectGroups = useMemo(() => groupByWorkspace(filtered), [filtered]);
  const currentWorkspaceKey = canonicalWorkspacePath(workingDirectory);
  const workspaceSessions = useMemo(() => {
    if (!currentWorkspaceKey) return [] as typeof enrichedConversations;
    return enrichedConversations.filter((c) => {
      const sessionWorkspace = canonicalWorkspacePath(c.workspaceRoot || c.worktreePath || "");
      return sessionWorkspace === currentWorkspaceKey;
    });
  }, [currentWorkspaceKey, enrichedConversations]);
  const selectedSessions = useMemo(
    () => enrichedConversations.filter((c) => selectedSessionIds.has(c.id)),
    [enrichedConversations, selectedSessionIds],
  );
  const selectableFiltered = useMemo(
    () => filtered.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting"),
    [filtered],
  );

  useEffect(() => {
    if (!menuFor) return;
    const close = () => setMenuFor(null);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", close);
    };
  }, [menuFor]);

  const deleteConversation = (id: string) => {
    const conversation = conversations.find((c) => c.id === id);
    if (!conversation) return;
    setConfirmDialog({
      title: "Delete session",
      message: conversation.gitIsolated
        ? "Delete this protected session and remove its separate workspace?"
        : "Delete this session? This removes it from the session list.",
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () => {
        const cleanup = Boolean(conversation.gitIsolated);
        sendClientCommand({ type: "conversation.delete", conversation_id: id, cleanup_worktree: cleanup });
        removeConversation(id);
      },
    });
  };

  const deleteSessionBatch = (items: ConversationMeta[], label: string) => {
    const deletable = items.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting");
    if (deletable.length === 0) return;
    const isolatedCount = deletable.filter((c) => c.gitIsolated).length;
    const skipped = items.length - deletable.length;
    setConfirmDialog({
      title: label,
      message: [
        `Delete ${deletable.length} session${deletable.length === 1 ? "" : "s"}? This removes them from the session list.`,
        isolatedCount > 0 ? `${isolatedCount} protected workspace${isolatedCount === 1 ? "" : "s"} will also be cleaned up.` : "",
        skipped > 0 ? `${skipped} running or waiting session${skipped === 1 ? "" : "s"} will be skipped.` : "",
      ].filter(Boolean).join("\n\n"),
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () => {
        for (const conversation of deletable) {
          sendClientCommand({
            type: "conversation.delete",
            conversation_id: conversation.id,
            cleanup_worktree: Boolean(conversation.gitIsolated),
          });
          removeConversation(conversation.id);
        }
        setSelectedSessionIds(new Set());
        setSelectionMode(false);
      },
    });
  };

  const toggleSessionSelected = (id: string) => {
    setSelectedSessionIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const setAllFilteredSelected = (selected: boolean) => {
    setSelectedSessionIds((prev) => {
      const next = new Set(prev);
      for (const c of selectableFiltered) {
        if (selected) next.add(c.id);
        else next.delete(c.id);
      }
      return next;
    });
  };

  const cleanupWorktree = (id: string, force = false) => {
    const conversation = conversations.find((c) => c.id === id);
    if (!conversation?.gitIsolated) return;
    setConfirmDialog({
      title: force ? "Force cleanup workspace" : "Clean up workspace",
      message: force
        ? "Force remove this protected session workspace and discard local changes?"
        : "Remove this protected session workspace? If it has local changes, MiniCode can ask for force cleanup later.",
      confirmLabel: force ? "Force cleanup" : "Clean up",
      danger: force,
      onConfirm: () => {
        sendClientCommand({
          type: "conversation.worktree.cleanup",
          conversation_id: id,
          force,
        });
      },
    });
  };

  const archiveConversation = (id: string, archived: boolean) => {
    sendClientCommand({ type: archived ? "conversation.archive" : "conversation.unarchive", conversation_id: id, archived });
    useAppStore.setState((s) => ({
      conversations: s.conversations.map((c) => (c.id === id ? { ...c, archived } : c)),
    }));
  };

  const handleSwitch = (id: string) => {
    setMenuFor(null);
    switchConversation(id);
    sendClientCommand({ type: "conversation.switch", conversation_id: id });
  };

  const revealConversationPath = (path?: string) => {
    if (!path) return;
    setMenuFor(null);
    if (isDesktop()) {
      void revealPath(path);
    }
  };

  const copyConversationPath = (path?: string) => {
    if (!path) return;
    setMenuFor(null);
    void navigator.clipboard?.writeText(path);
  };

  const startRename = (id: string, currentTitle: string) => {
    setRenaming(id);
    setRenameValue(currentTitle);
  };

  const commitRename = () => {
    if (!renaming || !renameValue.trim()) {
      setRenaming(null);
      return;
    }
    sendClientCommand({ type: "conversation.rename", conversation_id: renaming, title: renameValue.trim() });
    useAppStore.setState((s) => ({
      conversations: s.conversations.map((c) => (c.id === renaming ? { ...c, title: renameValue.trim() } : c)),
    }));
    setRenaming(null);
  };

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
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const statusDot = (status: string) => {
    const color = status === "running" ? "var(--state-info)" : status === "waiting" ? "var(--state-warning)" : "var(--text-muted)";
    return (
      <span style={{
        width: 6,
        height: 6,
        borderRadius: "50%",
        background: color,
        flexShrink: 0,
        boxShadow: status === "running" ? `0 0 4px ${color}` : "none",
        animation: status === "running" ? "thinking-pulse 1.5s ease-in-out infinite" : "none",
      }} />
    );
  };

  const runningCount = filterCounts.running || 0;

  useEffect(() => {
    setTab(appMode === "cowork" ? "conversations" : "files");
  }, [appMode]);

  const switchSidebarTab = (nextTab: SidebarTab) => {
    setTab(nextTab);
    setAppMode(nextTab === "conversations" ? "cowork" : "code");
  };

  return (
    <aside
      style={{
        width: leftSidebarWidth > 0 ? 352 : 0,
        minWidth: leftSidebarWidth > 0 ? 280 : 0,
        maxWidth: leftSidebarWidth > 0 ? 352 : 0,
        background: "var(--surface-sidebar)",
        borderRight: "1px solid var(--border-subtle)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: 8,
        boxSizing: "border-box",
        opacity: leftSidebarWidth > 0 ? 1 : 0,
        pointerEvents: leftSidebarWidth > 0 ? "auto" : "none",
      }}
    >
      {/* Tab bar */}
      <div style={modeSwitchStyle}>
        {(["conversations", "files"] as const).map((t) => (
          <button
            key={t}
            onClick={() => switchSidebarTab(t)}
            style={{
              ...modeSwitchButtonStyle,
              background: tab === t ? "var(--surface-base)" : "transparent",
              borderColor: tab === t ? "var(--border-soft)" : "transparent",
              color: tab === t ? "var(--text-primary)" : "var(--text-muted)",
              fontWeight: tab === t ? 650 : 500,
            }}
          >
            {t === "conversations" ? <SlidersHorizontal size={14} /> : <Code2 size={14} />}
            {t === "conversations" ? "Cowork" : "Code"}
            {t === "conversations" && runningCount > 0 && (
              <span style={{
                fontSize: 10,
                background: "var(--state-info)",
                color: "var(--text-on-accent)",
                borderRadius: 999,
                padding: "0 5px",
                fontWeight: 700,
                lineHeight: "16px",
                minWidth: 16,
                textAlign: "center",
              }}>
                {runningCount}
              </span>
            )}
          </button>
        ))}
      </div>

      {tab === "conversations" && (
        <>
          <div style={{ ...sessionControlStackStyle, borderBottom: 0, paddingBottom: 6 }}>
            <div style={sectionHeaderRowStyle}>
              <SectionTitle label="Sessions" />
              <span style={sectionMetaStyle}>{enrichedConversations.filter((c) => !c.archived).length}</span>
            </div>
            <div style={primaryActionGroupStyle}>
              <button onClick={createConversation} title="New session" aria-label="New session" style={primaryActionStyle}>
                <Plus size={14} />
                <span>New task</span>
              </button>
            </div>

            <div style={routineGroupStyle}>
              <button type="button" title="Routines" aria-label="Open routines" style={sidebarLinkStyle}>
                <RotateCcw size={13} />
                <span>Routines</span>
                <span style={comingSoonStyle}>soon</span>
              </button>
              <button type="button" onClick={() => useAppStore.getState().toggleSkillsMarketplace()} title="Customize agents and skills" aria-label="Customize agents and skills" style={sidebarLinkStyle}>
                <Box size={13} />
                <span>Customize</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setSelectionMode((value) => !value);
                  setSelectedSessionIds(new Set());
                }}
                title="Select multiple sessions"
                aria-label="Select multiple sessions"
                style={sidebarLinkStyle}
              >
                <SlidersHorizontal size={13} />
                <span>{selectionMode ? "Cancel selection" : "Select sessions"}</span>
              </button>
              <button
                type="button"
                onClick={() => deleteSessionBatch(workspaceSessions, "Clear workspace sessions")}
                disabled={workspaceSessions.filter((c) => c.sessionStatus !== "running" && c.sessionStatus !== "waiting").length === 0}
                title="Delete all sessions for the current workspace"
                aria-label="Delete all sessions for the current workspace"
                style={{
                  ...sidebarLinkStyle,
                  color: "var(--state-danger)",
                  opacity: workspaceSessions.length === 0 ? 0.55 : 1,
                  cursor: workspaceSessions.length === 0 ? "not-allowed" : "pointer",
                }}
              >
                <Trash2 size={13} />
                <span>Clear workspace</span>
                {workspaceSessions.length > 0 && <span style={comingSoonStyle}>{workspaceSessions.length}</span>}
              </button>
            </div>
          </div>

          {selectionMode && (
            <div style={bulkBarStyle}>
              <button
                type="button"
                onClick={() => setAllFilteredSelected(selectedSessionIds.size < selectableFiltered.length)}
                style={bulkActionStyle}
                disabled={selectableFiltered.length === 0}
              >
                {selectedSessionIds.size < selectableFiltered.length ? "Select visible" : "Clear"}
              </button>
              <span style={bulkMetaStyle}>{selectedSessions.length} selected</span>
              <button
                type="button"
                onClick={() => deleteSessionBatch(selectedSessions, "Delete selected sessions")}
                style={{ ...bulkActionStyle, color: "var(--state-danger)" }}
                disabled={selectedSessions.length === 0}
              >
                Delete
              </button>
            </div>
          )}

          <div style={{ padding: "6px 6px 0" }}>
            <SectionTitle label="Recents" />
          </div>

          <div style={{ ...searchBarWrapStyle, padding: "6px 6px 8px" }}>
            <input
              type="text"
              placeholder="Search sessions"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={searchInputStyle}
            />
          </div>

          {/* Filter pills — Claude Code style */}
          <div style={{ ...filterRowStyle, padding: "0 6px 8px" }}>
            {SESSION_FILTERS.map((f) => {
              const count = filterCounts[f.id] || 0;
              return (
                <button
                  key={f.id}
                  onClick={() => setSessionFilter(f.id)}
                  style={{
                    ...filterButtonStyle,
                    background: sessionFilter === f.id ? "var(--accent-soft)" : "transparent",
                    color: sessionFilter === f.id ? "var(--accent-primary)" : "var(--text-muted)",
                    fontWeight: sessionFilter === f.id ? 600 : 500,
                  }}
                >
                  {f.icon}
                  {f.label}
                  {count > 0 && <span style={filterCountStyle}>{count}</span>}
                </button>
              );
            })}
          </div>

          {/* Conversation list grouped by project */}
          <div style={sessionListWrapStyle}>
            {filtered.length === 0 ? (
              <div style={emptyStateStyle}>
                {search ? "No matches." : sessionFilter !== "all" ? "No sessions with this status." : "No sessions yet."}
              </div>
            ) : (
              Array.from(projectGroups.entries()).map(([project, items]) => (
                <div key={project} style={projectGroupStyle}>
                  {/* Project group header */}
                  {projectGroups.size > 1 && (
                    <button
                      onClick={() => toggleGroup(project)}
                      style={projectHeaderStyle}
                    >
                      <span style={{ fontSize: 10, opacity: 0.7, display: "inline-flex" }}>
                        {collapsedGroups.has(project) ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                      </span>
                      {project}
                      <span style={projectCountStyle}>
                        {items.length}
                      </span>
                    </button>
                  )}
                  {!collapsedGroups.has(project) && (
                    <div style={projectItemsStyle}>
                      {items.map((c) => (
                        <div
                          key={c.id}
                          onMouseEnter={(e) => (e.currentTarget.style.background = c.id === conversationId ? "var(--surface-page)" : "var(--surface-soft)")}
                          onMouseLeave={(e) => (e.currentTarget.style.background = c.id === conversationId ? "var(--surface-page)" : "transparent")}
                          style={{
                            ...sessionRowStyle,
                            background: c.id === conversationId ? "var(--surface-page)" : "transparent",
                            borderColor: c.id === conversationId ? "var(--border-subtle)" : "transparent",
                            opacity: c.archived ? 0.6 : 1,
                          }}
                        >
                          {selectionMode && (
                            <input
                              type="checkbox"
                              checked={selectedSessionIds.has(c.id)}
                              disabled={c.sessionStatus === "running" || c.sessionStatus === "waiting"}
                              onChange={() => toggleSessionSelected(c.id)}
                              onClick={(e) => e.stopPropagation()}
                              style={sessionCheckboxStyle}
                              aria-label={`Select ${c.title}`}
                            />
                          )}
                          <button
                            type="button"
                            onClick={(e) => {
                              if (selectionMode) {
                                toggleSessionSelected(c.id);
                                return;
                              }
                              if (e.ctrlKey || e.metaKey) {
                                const id = `chat-${c.id}`;
                                const state = useAppStore.getState();
                                if (!state.panelSlots.some(p => p.id === id)) {
                                  state.addPanel({ id, kind: "chat" });
                                }
                              } else {
                                handleSwitch(c.id);
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
                                  onChange={(e) => setRenameValue(e.target.value)}
                                  onBlur={commitRename}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") commitRename();
                                    if (e.key === "Escape") setRenaming(null);
                                  }}
                                  onClick={(e) => e.stopPropagation()}
                                  style={renameInputStyle}
                                />
                              ) : (
                                <div
                                  onDoubleClick={(e) => {
                                    e.stopPropagation();
                                    startRename(c.id, c.title);
                                  }}
                                  style={{
                                    ...sessionTitleStyle,
                                    fontWeight: c.id === conversationId ? 650 : 500,
                                  }}
                                >
                                  {c.title}
                                </div>
                              )}
                              <div style={sessionMetaLineStyle}>
                                <span>{relativeTime(c.updatedAt)}</span>
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
                            onClick={(e) => {
                              e.stopPropagation();
                              setMenuFor((current) => (current === c.id ? null : c.id));
                            }}
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
                              onSwitch={() => handleSwitch(c.id)}
                              onReveal={() => revealConversationPath(c.worktreePath || c.workspaceRoot)}
                              onCopy={() => copyConversationPath(c.worktreePath || c.workspaceRoot)}
                              onCleanup={() => {
                                setMenuFor(null);
                                cleanupWorktree(c.id);
                              }}
                              onArchive={() => {
                                setMenuFor(null);
                                archiveConversation(c.id, !c.archived);
                              }}
                              onDelete={() => {
                                setMenuFor(null);
                                deleteConversation(c.id);
                              }}
                            />
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>

          <div style={sessionFooterStyle}>
              <span>{enrichedConversations.filter((c) => !c.archived).length} sessions</span>
              {runningCount > 0 && (
                <span style={{ color: "var(--state-info)", display: "flex", alignItems: "center", gap: 3 }}>
                  <Loader size={10} /> {runningCount} active
                </span>
              )}
              <span style={{ flex: 1 }} />
              {enrichedConversations.filter((c) => c.archived).length > 0 && (
                <span>{enrichedConversations.filter((c) => c.archived).length} archived</span>
              )}
          </div>
        </>
      )}

      {tab === "files" && <FileTree />}
      {confirmDialog && (
        <ConfirmDialog
          dialog={confirmDialog}
          onCancel={() => setConfirmDialog(null)}
          onConfirm={() => {
            const action = confirmDialog.onConfirm;
            setConfirmDialog(null);
            action();
          }}
        />
      )}
    </aside>
  );
};

const primaryActionGroupStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: 4,
};

const primaryActionStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  height: 32,
  padding: "0 10px",
  background: "var(--surface-page)",
  color: "var(--text-primary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
};

const sessionControlStackStyle: React.CSSProperties = {
  display: "grid",
  gap: 10,
  padding: "12px 6px 8px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

const routineGroupStyle: React.CSSProperties = {
  display: "grid",
  gap: 3,
  padding: 0,
};

const sidebarLinkStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
  minHeight: 32,
  padding: "0 9px",
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 600,
  textAlign: "left",
};

const comingSoonStyle: React.CSSProperties = {
  marginLeft: "auto",
  fontSize: 10,
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
};

const bulkBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  margin: "0 6px 6px",
  padding: "6px",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const bulkActionStyle: React.CSSProperties = {
  height: 24,
  padding: "0 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const bulkMetaStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  textAlign: "center",
};

const sessionCheckboxStyle: React.CSSProperties = {
  width: 14,
  height: 14,
  flexShrink: 0,
  accentColor: "var(--accent-primary)",
};

const SectionTitle = ({ label }: { label: string }) => (
  <div style={{ color: "var(--text-muted)", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0 }}>
    {label}
  </div>
);

const sessionMainButtonStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: 0,
  background: "transparent",
  border: 0,
  cursor: "pointer",
  textAlign: "left",
};

const sectionHeaderRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
};

const sectionMetaStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: 11,
  fontFamily: "var(--font-mono)",
};

const searchBarWrapStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
};

const searchInputStyle: React.CSSProperties = {
  width: "100%",
  minWidth: 0,
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  height: 30,
  padding: "0 10px",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};

const filterRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  flexWrap: "wrap",
  overflow: "hidden",
};

const filterButtonStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  height: 24,
  padding: "0 8px",
  border: 0,
  borderRadius: 999,
  cursor: "pointer",
  fontSize: 11,
  whiteSpace: "nowrap",
  transition: "var(--transition-fast)",
};

const filterCountStyle: React.CSSProperties = {
  fontSize: 10,
  opacity: 0.7,
  fontFamily: "var(--font-mono)",
};

const sessionListWrapStyle: React.CSSProperties = {
  flex: 1,
  overflowY: "auto",
  overflowX: "hidden",
  minWidth: 0,
  padding: "2px 0 10px",
  display: "grid",
  alignContent: "start",
  gap: 10,
};

const emptyStateStyle: React.CSSProperties = {
  padding: 16,
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
  textAlign: "center",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
};

const projectGroupStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  minWidth: 0,
};

const projectHeaderStyle: React.CSSProperties = {
  width: "100%",
  display: "flex",
  alignItems: "center",
  gap: 6,
  minHeight: 24,
  padding: "0 6px",
  background: "transparent",
  border: 0,
  cursor: "pointer",
  fontSize: 11,
  color: "var(--text-muted)",
  fontWeight: 700,
  textTransform: "uppercase",
  textAlign: "left",
};

const projectCountStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontWeight: 500,
  marginLeft: "auto",
  opacity: 0.8,
};

const projectItemsStyle: React.CSSProperties = {
  display: "grid",
  gap: 2,
  minWidth: 0,
};

const sessionRowStyle: React.CSSProperties = {
  width: "100%",
  minWidth: 0,
  boxSizing: "border-box",
  display: "flex",
  alignItems: "center",
  minHeight: 34,
  padding: "5px 7px",
  cursor: "pointer",
  gap: 8,
  position: "relative",
  transition: "var(--transition-fast)",
  textAlign: "left",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
};

const sessionRowHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const sessionTitleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: "var(--text-sm)",
  color: "var(--text-primary)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const sessionTimeStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
};

const sessionMetaLineStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
  overflow: "hidden",
  whiteSpace: "nowrap",
  fontSize: 11,
  color: "var(--text-muted)",
  marginTop: 2,
};

const branchMetaStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 3,
  minWidth: 0,
};

const workspaceMetaStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const renameInputStyle: React.CSSProperties = {
  width: "100%",
  background: "var(--surface-page)",
  border: "1px solid var(--accent-primary)",
  borderRadius: 4,
  padding: "2px 6px",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};

const sessionFooterStyle: React.CSSProperties = {
  padding: "8px 6px 0",
  borderTop: "1px solid var(--border-subtle)",
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
  overflow: "hidden",
  fontSize: 11,
  color: "var(--text-muted)",
  background: "var(--surface-sidebar)",
};

const IconAction = ({
  children,
  label,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
}) => (
  <button
    onClick={onClick}
    title={label}
    aria-label={label}
    style={{
      background: "transparent",
      border: 0,
      color: "var(--text-muted)",
      cursor: "pointer",
      padding: "2px 4px",
      opacity: 0.7,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      borderRadius: "var(--radius-sm, 4px)",
    }}
  >
    {children}
  </button>
);

const ConversationMenu = ({
  archived,
  isIsolated,
  canDelete,
  canReveal,
  canCopy,
  onSwitch,
  onReveal,
  onCopy,
  onCleanup,
  onArchive,
  onDelete,
}: {
  archived: boolean;
  isIsolated: boolean;
  canDelete: boolean;
  canReveal: boolean;
  canCopy: boolean;
  onSwitch: () => void;
  onReveal: () => void;
  onCopy: () => void;
  onCleanup: () => void;
  onArchive: () => void;
  onDelete: () => void;
}) => (
  <div
    onClick={(e) => e.stopPropagation()}
    style={{
      position: "absolute",
      right: 8,
      top: 34,
      zIndex: 20,
      minWidth: 190,
      padding: 4,
      background: "var(--surface-raised)",
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-sm, 6px)",
      boxShadow: "var(--shadow-md)",
    }}
  >
    <MenuItem icon={<LogIn size={13} />} label="Switch session" onClick={onSwitch} />
    <MenuItem icon={<FolderOpen size={13} />} label="Reveal workspace" onClick={onReveal} disabled={!canReveal} />
    <MenuItem icon={<Copy size={13} />} label="Copy workspace path" onClick={onCopy} disabled={!canCopy} />
    {isIsolated && (
      <MenuItem icon={<XCircle size={13} />} label="Clean up workspace" onClick={onCleanup} />
    )}
    <MenuDivider />
    <MenuItem
      icon={archived ? <RotateCcw size={13} /> : <Archive size={13} />}
      label={archived ? "Unarchive" : "Archive"}
      onClick={onArchive}
    />
    {canDelete && <MenuItem danger icon={<Trash2 size={13} />} label="Delete" onClick={onDelete} />}
  </div>
);

const MenuItem = ({
  icon,
  label,
  onClick,
  disabled,
  danger,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    style={{
      width: "100%",
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "6px 8px",
      border: 0,
      borderRadius: "var(--radius-sm, 4px)",
      background: "transparent",
      color: disabled
        ? "var(--text-muted)"
        : danger
          ? "var(--state-danger)"
          : "var(--text-secondary)",
      cursor: disabled ? "not-allowed" : "pointer",
      fontSize: "var(--text-xs)",
      textAlign: "left",
      opacity: disabled ? 0.55 : 1,
    }}
  >
    {icon}
    <span>{label}</span>
  </button>
);

const MenuDivider = () => (
  <div
    style={{
      height: 1,
      margin: "4px 2px",
      background: "var(--border-subtle)",
    }}
  />
);

const ConfirmDialog = ({
  dialog,
  onCancel,
  onConfirm,
}: {
  dialog: NonNullable<ConfirmDialogState>;
  onCancel: () => void;
  onConfirm: () => void;
}) => (
  <div
    role="presentation"
    onClick={onCancel}
    style={{
      position: "fixed",
      inset: 0,
      zIndex: 1200,
      display: "grid",
      placeItems: "center",
      background: "rgba(0,0,0,0.48)",
      padding: 16,
    }}
  >
    <div
      role="dialog"
      aria-modal="true"
      aria-label={dialog.title}
      onClick={(e) => e.stopPropagation()}
      style={{
        width: "min(360px, 100%)",
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md, 8px)",
        boxShadow: "var(--shadow-strong, var(--shadow-md))",
        padding: 14,
      }}
    >
      <div style={{ fontSize: "var(--text-md)", color: "var(--text-primary)", fontWeight: 700 }}>
        {dialog.title}
      </div>
      <div style={{ marginTop: 8, color: "var(--text-secondary)", fontSize: "var(--text-sm)", lineHeight: 1.45 }}>
        {dialog.message}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button type="button" onClick={onCancel} style={dialogCancelStyle}>
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          style={{
            ...dialogConfirmStyle,
            background: dialog.danger ? "var(--state-danger)" : "var(--accent-primary)",
          }}
        >
          {dialog.confirmLabel}
        </button>
      </div>
    </div>
  </div>
);

const dialogCancelStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 10px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const dialogConfirmStyle: React.CSSProperties = {
  border: 0,
  color: "var(--text-on-accent)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 10px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};
