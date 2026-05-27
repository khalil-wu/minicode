import {
  Activity,
  CheckCircle,
  Copy,
  ExternalLink,
  FolderOpen,
  GitBranch,
  Globe,
  MonitorPlay,
  MoreHorizontal,
  PanelRightClose,
  RefreshCw,
  Search,
  TerminalSquare,
  Users,
  Wrench,
} from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { apiBase, authHeaders } from "../protocol/api";
import { isDesktop, revealPath } from "../desktop/runtime";
import { fetchWorkspaceGitStatus, type WorkspaceGitStatusResponse } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { BrowserPanel } from "../panels/BrowserPanel";
import { PreviewPanel } from "../panels/PreviewPanel";
import { AgentProgressTrace } from "../panels/AgentProgressTrace";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { PanelSkeleton } from "./PanelSkeleton";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";

type StackTab = "preview" | "browser" | "terminal" | "tasks" | "plan" | "subagents" | "inspector" | "diagnostics";

interface SidebarRightProps {
  embedded?: boolean;
  initialTab?: StackTab | "details" | "context";
}

interface DoctorPayload {
  backend?: Record<string, unknown>;
  llm?: Record<string, unknown>;
  mcp?: unknown[];
  workspace?: Record<string, unknown>;
  git?: Record<string, unknown>;
  preview?: Record<string, unknown>;
  terminal?: Record<string, unknown>;
  error?: string;
}

const normalizeInitialTab = (tab: SidebarRightProps["initialTab"]): StackTab => {
  if (tab === "details" || tab === "context") return "inspector";
  return tab ?? "preview";
};

const LazyTerminalPanel = lazy(() =>
  import("../panels/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

const preloadTerminal = () => {
  void import("@xterm/xterm");
  void import("@xterm/addon-fit");
  void import("../panels/TerminalPanel");
};

export const SidebarRight = ({ embedded = false, initialTab = "preview" }: SidebarRightProps) => {
  const messages = useAppStore((s) => s.messages);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const rightStackTabLocked = useAppStore((s) => s.rightStackTabLocked);
  const rightSidebarWidth = useAppStore((s) => s.rightSidebarWidth);
  const setRightSidebarWidth = useAppStore((s) => s.setRightSidebarWidth);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [tabMenuOpen, setTabMenuOpen] = useState(false);
  const activeTab = embedded ? localTab : rightStackTab;
  const setActiveTab = embedded ? setLocalTab : setRightStackTab;
  const plan = useAppStore((s) => s.plan);
  const todos = useAppStore((s) => s.todos);
  const subagents = useAppStore((s) => s.subagents);
  const permissionMode = useAppStore((s) => s.permissionMode);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const previewArtifact = useAppStore((s) => s.previewArtifact);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const runningTasks = todos.filter((t) => t.status === "in_progress").length;

  useEffect(() => { preloadTerminal(); }, []);
  const runningProgress = useAppStore((s) => {
    const key = s.conversationId || "__active__";
    return s.agentProgress.filter((entry) =>
      entry.status === "running" && (entry.conversationId === key || entry.conversationId === "__active__")
    ).length;
  });

  useEffect(() => {
    if (embedded) return;
    if (rightStackTabLocked) return;
    if (livePreviewUrl || previewArtifact) setRightStackTab("preview", { automatic: true });
  }, [embedded, livePreviewUrl, previewArtifact?.artifactId, rightStackTabLocked, setRightStackTab]);

  useEffect(() => {
    if (embedded) return;
    if (rightStackTabLocked) return;
    if (plan && plan.status !== "completed" && plan.status !== "cancelled") setRightStackTab("plan", { automatic: true });
  }, [embedded, plan?.planId, plan?.status, rightStackTabLocked, setRightStackTab]);

  useEffect(() => {
    if (embedded) return;
    if (rightStackTabLocked) return;
    if (runningTasks + runningProgress > 0) setRightStackTab("tasks", { automatic: true });
  }, [embedded, rightStackTabLocked, runningProgress, runningTasks, setRightStackTab]);

  const lastToolCalls = useMemo(() => messages.flatMap((m) => getToolCallsFromMessage(m)).slice(-5), [messages]);
  const runningSubagents = subagents.filter((subagent) => subagent.status === "running").length;
  const runningTerminals = terminalSessions.filter((t) => t.status !== "exited").length;
  const mcpErrors = mcpServers.filter((s) => s.status === "error").length;

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    { id: "preview", label: "Preview", badge: livePreviewUrl || previewArtifact ? "on" : undefined, icon: <MonitorPlay size={15} /> },
    ...(isDesktop() ? [{ id: "browser" as const, label: "Browser", icon: <Globe size={15} /> }] : []),
    { id: "terminal", label: "Terminal", badge: runningTerminals ? String(runningTerminals) : undefined, icon: <TerminalSquare size={15} /> },
    { id: "tasks", label: "Activity", badge: runningTasks || runningProgress ? String(runningTasks + runningProgress) : undefined, icon: <CheckCircle size={15} /> },
    { id: "inspector", label: "Context", icon: <Search size={15} /> },
    { id: "plan", label: "Plan", badge: permissionMode === "plan" ? "mode" : plan?.status === "executing" ? "run" : undefined, icon: <Activity size={15} /> },
    { id: "subagents", label: "Agents", badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Users size={15} /> },
    { id: "diagnostics", label: "Health", badge: mcpErrors ? String(mcpErrors) : undefined, icon: <Wrench size={15} /> },
  ];
  const activeItem = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const primaryTabs = tabs.filter((tab) => ["preview", "terminal", "tasks", "inspector"].includes(tab.id));
  const overflowTabs = tabs.filter((tab) => !primaryTabs.some((primary) => primary.id === tab.id));
  const activateTab = (tab: StackTab) => {
    setActiveTab(tab);
    setTabMenuOpen(false);
  };
  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (embedded) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = rightSidebarWidth;
    const onMove = (moveEvent: PointerEvent) => {
      setRightSidebarWidth(startWidth + startX - moveEvent.clientX);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <aside
      style={{
        position: "relative",
        width: embedded ? "100%" : rightSidebarWidth,
        minWidth: embedded ? 0 : 320,
        maxWidth: embedded ? "none" : 1040,
        flex: embedded ? "1 1 auto" : `0 0 ${rightSidebarWidth}px`,
        background: "var(--surface-base)",
        borderLeft: embedded ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: embedded ? 0 : 0,
      }}
    >
      {!embedded && (
        <div
          role="separator"
          aria-orientation="vertical"
          title="Resize side panel"
          onPointerDown={startResize}
          style={resizeHandleStyle}
        />
      )}
      <div style={panelTopBarStyle}>
        <div style={activePanelTitleStyle}>
          <span style={{ display: "inline-flex", color: "var(--text-muted)" }}>{activeItem.icon}</span>
          <span>{activeItem.label}</span>
          {activeItem.badge && <span style={badgeStyle}>{activeItem.badge}</span>}
        </div>
        <div style={panelIconGroupStyle}>
        {primaryTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => activateTab(t.id)}
            title={t.label}
            aria-label={`Open ${t.label}`}
            style={{
              ...panelIconButtonStyle,
              background: activeTab === t.id ? "var(--surface-page)" : "transparent",
              color: activeTab === t.id ? "var(--text-primary)" : "var(--text-muted)",
              borderColor: activeTab === t.id ? "var(--border-subtle)" : "transparent",
            }}
          >
            {t.icon}
            {t.badge && <span style={badgeStyle}>{t.badge}</span>}
          </button>
        ))}
        <div style={{ position: "relative" }}>
          <button
            type="button"
            onClick={() => setTabMenuOpen((open) => !open)}
            title="More panels"
            aria-label="More panels"
            style={{
              ...panelIconButtonStyle,
              background: tabMenuOpen ? "var(--surface-page)" : "transparent",
              color: tabMenuOpen ? "var(--text-primary)" : "var(--text-muted)",
              borderColor: tabMenuOpen ? "var(--border-subtle)" : "transparent",
            }}
          >
            <MoreHorizontal size={15} />
          </button>
          {tabMenuOpen && (
            <div style={panelMenuStyle}>
              {overflowTabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => activateTab(tab.id)}
                  style={{
                    ...panelMenuItemStyle,
                    background: activeTab === tab.id ? "var(--surface-soft)" : "transparent",
                  }}
                >
                  <span style={{ color: "var(--text-muted)", display: "inline-flex" }}>{tab.icon}</span>
                  <span style={{ flex: 1 }}>{tab.label}</span>
                  {tab.badge && <span style={badgeStyle}>{tab.badge}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
        {!embedded && (
          <button
            type="button"
            onClick={toggleRightPanel}
            title="Close panel (Ctrl+Shift+B)"
            aria-label="Close right panel"
            style={{
              ...panelIconButtonStyle,
              background: "transparent",
              color: "var(--text-muted)",
              borderColor: "transparent",
            }}
          >
            <PanelRightClose size={15} />
          </button>
        )}
        </div>
      </div>
      <div style={sidebarContentStyle}>
        {activeTab === "preview" && <PreviewPanel />}
        {activeTab === "browser" && <BrowserPanel />}
        {activeTab === "terminal" && (
          <ChunkErrorBoundary>
            <Suspense fallback={<PanelSkeleton kind="terminal" />}>
              <LazyTerminalPanel />
            </Suspense>
          </ChunkErrorBoundary>
        )}
        {activeTab === "tasks" && <ScrollablePanel><TasksTab /></ScrollablePanel>}
        {activeTab === "plan" && <ScrollablePanel><PlanTab /></ScrollablePanel>}
        {activeTab === "subagents" && <ScrollablePanel><SubagentsTab /></ScrollablePanel>}
        {activeTab === "inspector" && <ScrollablePanel><InspectorTab toolCalls={lastToolCalls} /></ScrollablePanel>}
        {activeTab === "diagnostics" && <ScrollablePanel><DiagnosticsTab /></ScrollablePanel>}
      </div>
    </aside>
  );
};

const PlanTab = () => {
  const plan = useAppStore((s) => s.plan);
  const permissionMode = useAppStore((s) => s.permissionMode);
  if (!plan) {
    return (
      <div style={{ display: "grid", gap: 10 }}>
        <PanelHeader title="Plan" meta={permissionMode === "plan" ? "read-only" : undefined} />
        <AgentProgressTrace mode="compact" />
        <EmptyLine>No proposed plan in this session.</EmptyLine>
      </div>
    );
  }
  const doneCount = plan.steps.filter((s) => s.status === "done").length;

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <PanelHeader title="Plan" meta={`${doneCount}/${plan.steps.length} ${plan.status}`} />
      <AgentProgressTrace mode="compact" />
      <div style={{ display: "grid", gap: 2 }}>
        {plan.steps.map((s, i) => (
          <div key={s.id} style={rowCardStyle(i === plan.currentStep && s.status === "running")}>
            <StatusMark status={s.status} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: s.status === "done" ? "var(--text-muted)" : "var(--text-primary)", textDecoration: s.status === "done" ? "line-through" : "none", fontSize: "var(--text-xs)", lineHeight: 1.45, fontWeight: i === plan.currentStep ? 600 : 400 }}>
                {s.title}
              </div>
              {s.detail && <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: 2 }}>{s.detail}</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const TasksTab = () => {
  const todos = useAppStore((s) => s.todos);
  const hasProgress = useAppStore((s) => {
    const key = s.conversationId || "__active__";
    return s.agentProgress.some((entry) => entry.conversationId === key || entry.conversationId === "__active__");
  });
  if (todos.length === 0) {
    return (
      <div style={{ display: "grid", gap: 10 }}>
        <AgentProgressTrace />
        {!hasProgress && <EmptyLine>No active tasks.</EmptyLine>}
      </div>
    );
  }
  const completed = todos.filter((t) => t.status === "completed").length;
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <PanelHeader title="Tasks" meta={`${completed}/${todos.length}`} />
      <AgentProgressTrace />
      <div style={{ display: "grid", gap: 4 }}>
        {todos.map((t) => (
          <div key={t.id} style={rowCardStyle(t.status === "in_progress")}>
            <StatusMark status={t.status} />
            <span style={{ color: t.status === "completed" ? "var(--text-muted)" : "var(--text-primary)", textDecoration: t.status === "completed" ? "line-through" : "none", fontSize: "var(--text-xs)", lineHeight: 1.45 }}>
              {t.status === "in_progress" && t.activeForm ? t.activeForm : t.content}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

const SubagentsTab = () => {
  const subagents = useAppStore((s) => s.subagents);
  if (subagents.length === 0) return <EmptyLine>No delegated subagents are running.</EmptyLine>;
  const running = subagents.filter((subagent) => subagent.status === "running").length;
  return (
    <div>
      <PanelHeader title="Subagents" meta={running ? `${running}/${subagents.length} running` : `${subagents.length} recent`} />
      <div style={{ display: "grid", gap: 4 }}>
        {subagents.map((subagent) => (
          <div key={subagent.id} style={rowCardStyle(subagent.status === "running")}>
            <StatusMark status={subagent.status} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text-primary)", fontSize: "var(--text-xs)", lineHeight: 1.45 }}>
                {subagent.role}
              </div>
              <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
                {subagent.id}
              </div>
              {subagent.summary && (
                <div style={{ color: "var(--text-secondary)", fontSize: "var(--text-xs)", marginTop: 4 }}>
                  {subagent.summary}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const InspectorTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => (
  <div style={{ display: "grid", gap: 12 }}>
    <ContextTab />
    <DetailsTab toolCalls={toolCalls} />
  </div>
);

const ContextTab = () => {
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const contextUsage = useAppStore((s) => s.contextUsage);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const [gitStatus, setGitStatus] = useState<WorkspaceGitStatusResponse | null>(null);
  const [gitLoading, setGitLoading] = useState(false);
  const conversation = conversations.find((item) => item.id === conversationId);
  const workspacePath = conversation?.worktreePath || conversation?.workspaceRoot || workingDirectory || workspaceGit?.currentPath || "";
  const branch = conversation?.gitBranch || workspaceGit?.branch || "No branch";
  const displayWorkspace = workspaceDisplayName(workspacePath, "Current workspace");
  const displayBranch = branchDisplayName(branch) || "No branch";
  const contextPercent = contextUsage && contextUsage.limit > 0 ? Math.round((contextUsage.used / contextUsage.limit) * 100) : null;
  const changedCount = gitStatus ? gitStatus.modified.length + gitStatus.staged.length + gitStatus.untracked.length : null;

  const refreshGitStatus = () => {
    setGitLoading(true);
    fetchWorkspaceGitStatus().then((result) => setGitStatus(result)).finally(() => setGitLoading(false));
  };

  useEffect(() => {
    refreshGitStatus();
  }, [workspacePath]);

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <SectionLabel label="Session" />
      <InfoCard>
        <InfoRow label="Conversation" value={conversation?.title || conversationId || "No active conversation"} />
        <InfoRow label="Isolation" value={conversation?.gitIsolated || workspaceGit?.isWorktree ? "Protected workspace" : "Shared workspace"} tone={conversation?.gitIsolated || workspaceGit?.isWorktree ? "accent" : "muted"} />
        <InfoRow label="Branch" value={displayBranch} mono />
      </InfoCard>
      <SectionLabel label="Workspace" />
      <InfoCard>
        <InfoRow label="Path" value={displayWorkspace} mono />
        <InfoRow label="Changes" value={gitLoading ? "Checking..." : changedCount == null ? "Unknown" : changedCount === 0 ? "Clean" : `${changedCount} changed`} tone={changedCount && changedCount > 0 ? "warning" : "muted"} />
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          <SmallButton icon={<FolderOpen size={12} />} label="Reveal" disabled={!workspacePath || !isDesktop()} onClick={() => void revealPath(workspacePath)} />
          <SmallButton icon={<Copy size={12} />} label="Copy" disabled={!workspacePath} onClick={() => void navigator.clipboard?.writeText(workspacePath)} />
          <SmallButton icon={<GitBranch size={12} />} label="Refresh" onClick={refreshGitStatus} />
        </div>
      </InfoCard>
      <SectionLabel label="Runtime" />
      <InfoCard>
        <InfoRow label="Context" value={contextPercent == null ? "Not reported" : `${contextPercent}% (${contextUsage?.used}/${contextUsage?.limit})`} tone={contextPercent != null && contextPercent >= 85 ? "warning" : "muted"} />
        <InfoRow
          label="Compact"
          value={contextUsage?.compactedAt ? `${new Date(contextUsage.compactedAt).toLocaleTimeString()}: ${contextUsage.compactSummary || "Done"}` : "Not compacted"}
          tone={contextUsage?.compactedAt ? "accent" : "muted"}
        />
        <InfoRow label="Terminals" value={String(terminalSessions.length)} />
        <InfoRow label="Editor" value={activeEditorPath || "No active file"} mono />
        <SmallButton icon={<TerminalSquare size={12} />} label="Open Terminal" onClick={() => setRightStackTab("terminal")} />
      </InfoCard>
    </div>
  );
};

const DetailsTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => {
  const inspectorEntries = useAppStore((s) => s.inspectorEntries);
  const inspectorFocus = useAppStore((s) => s.inspectorFocus);
  const focusedEntry = inspectorFocus ? inspectorEntries.find((e) => e.targetId === inspectorFocus.id) : null;
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <SectionLabel label="Inspector" />
      {focusedEntry && (
        <pre style={jsonBlockStyle}>
          {JSON.stringify(focusedEntry.payload, null, 2)}
        </pre>
      )}
      {inspectorEntries.length > 0 && inspectorEntries.slice(-8).reverse().map((entry, i) => (
        <button key={`${entry.targetId}-${i}`} onClick={() => useAppStore.getState().setInspectorFocus({ kind: entry.targetKind, id: entry.targetId })} style={eventButtonStyle(entry.targetId === inspectorFocus?.id)}>
          <span style={{ color: "var(--text-muted)" }}>{entry.targetKind}</span>
          <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{entry.targetId.slice(0, 12)}</span>
        </button>
      ))}
      {toolCalls.length > 0 && (
        <>
          <SectionLabel label="Recent Tools" />
          {toolCalls.map((tc) => (
            <div key={tc.id} style={toolCallRowStyle}>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{tc.name}</span>
              <span style={{ marginLeft: 8, color: statusColor(tc.status) }}>{tc.status}</span>
            </div>
          ))}
        </>
      )}
      {inspectorEntries.length === 0 && toolCalls.length === 0 && <EmptyLine>No recent activity to inspect.</EmptyLine>}
    </div>
  );
};

const DiagnosticsTab = () => {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const local = useLocalDiagnostics();

  const refresh = () => {
    setLoading(true);
    fetch(`${apiBase()}/api/doctor`, { cache: "no-store", headers: authHeaders() })
      .then((res) => res.ok ? res.json() : Promise.reject(new Error(res.statusText)))
      .then((payload) => setDoctor(payload as DoctorPayload))
      .catch((error) => setDoctor({ error: String(error) }))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div style={{ display: "grid", gap: 10 }}>
      <PanelHeader title="Diagnostics" meta={loading ? "checking" : "ready"} action={<SmallButton icon={<RefreshCw size={12} />} label="Refresh" onClick={refresh} />} />
      {doctor?.error && <div style={errorStyle}>{doctor.error}</div>}
      <InfoCard>
        <InfoRow label="Backend" value={String(doctor?.backend?.status ?? "unknown")} tone={doctor?.backend?.status === "ok" ? "accent" : "warning"} />
        <InfoRow label="Sessions" value={String(doctor?.backend?.active_sessions ?? local.activeSessions)} />
        <InfoRow label="Provider" value={String(doctor?.llm?.provider ?? "unknown")} />
        <InfoRow label="Model" value={String(doctor?.llm?.active_model ?? doctor?.llm?.current_model ?? local.model)} mono />
      </InfoCard>
      <InfoCard>
        <InfoRow label="Workspace" value={workspaceDisplayName(String((doctor?.workspace?.root ?? local.workspace) || ""), "Current workspace")} mono />
        <InfoRow label="Branch" value={branchDisplayName(String(doctor?.git?.branch ?? local.branch ?? "")) || "--"} />
        <InfoRow label="Preview Pane" value={String(doctor?.preview?.url ?? local.preview ?? "--")} mono />
        <InfoRow label="Terminal" value={`${local.terminals} sessions`} />
      </InfoCard>
      <InfoCard>
        <InfoRow label="MCP" value={`${Array.isArray(doctor?.mcp) ? doctor?.mcp.length : local.mcpServers} servers`} tone={local.mcpErrors ? "warning" : "muted"} />
        <InfoRow label="MCP errors" value={String(local.mcpErrors)} tone={local.mcpErrors ? "warning" : "muted"} />
        <InfoRow label="Runtime" value={isDesktop() ? "Electron desktop" : "Web fallback"} />
      </InfoCard>
    </div>
  );
};

const useLocalDiagnostics = () => {
  const conversations = useAppStore((s) => s.conversations);
  const currentModel = useAppStore((s) => s.currentModel);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  return useMemo(() => ({
    activeSessions: conversations.length,
    model: currentModel || "No model",
    workspace: workingDirectory,
    branch: workspaceGit?.branch,
    preview: livePreviewUrl,
    terminals: terminalSessions.length,
    mcpServers: mcpServers.length,
    mcpErrors: mcpServers.filter((s) => s.status === "error").length,
  }), [conversations.length, currentModel, workingDirectory, workspaceGit?.branch, livePreviewUrl, terminalSessions.length, mcpServers]);
};

function statusColor(status: string): string {
  if (status === "running" || status === "in_progress") return "var(--state-info)";
  if (status === "done" || status === "success" || status === "completed") return "var(--state-success)";
  if (status === "error" || status === "failed") return "var(--state-danger)";
  if (status === "blocked") return "var(--state-warning)";
  return "var(--text-muted)";
}

const StatusMark = ({ status }: { status: string }) => {
  const size = 14;
  const color = statusColor(status);
  if (status === "completed" || status === "done") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" fill={color} opacity={0.15} />
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
        <path d="M5 8.2 7 10.2 11 6" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      </svg>
    );
  }
  if (status === "running" || status === "in_progress") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" strokeDasharray="11 33" strokeLinecap="round">
          <animateTransform attributeName="transform" type="rotate" from="0 8 8" to="360 8 8" dur="0.8s" repeatCount="indefinite" />
        </circle>
        <circle cx="8" cy="8" r="3" fill={color} opacity={0.5} />
      </svg>
    );
  }
  if (status === "failed" || status === "error" || status === "blocked") {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
        <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
        <path d="M6 6l4 4M10 6l-4 4" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
      <circle cx="8" cy="8" r="7" stroke={color} strokeWidth="1.5" fill="none" />
    </svg>
  );
};

const ScrollablePanel = ({ children }: { children: React.ReactNode }) => (
  <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px 14px", background: "var(--surface-base)" }}>
    {children}
  </div>
);

const EmptyLine = ({ children }: { children: React.ReactNode }) => (
  <span style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", lineHeight: 1.5 }}>{children}</span>
);

const PanelHeader = ({ title, meta, action }: { title: string; meta?: string; action?: React.ReactNode }) => (
  <div style={panelHeaderStyle}>
    <span style={{ fontWeight: 700, color: "var(--text-primary)", flex: 1 }}>{title}</span>
    {meta && <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{meta}</span>}
    {action}
  </div>
);

const SectionLabel = ({ label }: { label: string }) => (
  <div style={sectionLabelStyle}>
    {label}
  </div>
);

const InfoCard = ({ children }: { children: React.ReactNode }) => (
  <div style={infoCardStyle}>
    {children}
  </div>
);

const InfoRow = ({ label, value, mono, tone = "default" }: { label: string; value: string; mono?: boolean; tone?: "default" | "muted" | "accent" | "warning" }) => {
  const color = tone === "accent" ? "var(--accent-primary)" : tone === "warning" ? "var(--state-warning)" : tone === "muted" ? "var(--text-muted)" : "var(--text-secondary)";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "88px minmax(0, 1fr)", gap: 8, fontSize: "var(--text-xs)" }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span title={value} style={{ color, fontFamily: mono ? "var(--font-mono)" : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</span>
    </div>
  );
};

const SmallButton = ({ icon, label, onClick, disabled }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) => (
  <button type="button" disabled={disabled} onClick={onClick} style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 7px", background: "var(--surface-page)", color: disabled ? "var(--text-muted)" : "var(--text-secondary)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", cursor: disabled ? "not-allowed" : "pointer", fontSize: "var(--text-xs)", whiteSpace: "nowrap" }}>
    {icon}
    {label}
  </button>
);

const panelTopBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 10px",
  minHeight: 42,
  background: "var(--surface-sidebar)",
  borderBottom: "1px solid var(--border-subtle)",
};

const resizeHandleStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  bottom: 0,
  left: -3,
  width: 7,
  cursor: "col-resize",
  zIndex: 30,
  background: "transparent",
};

const activePanelTitleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  color: "var(--text-primary)",
  fontSize: "14px",
  fontWeight: 650,
};

const panelIconGroupStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
};

const panelIconButtonStyle: React.CSSProperties = {
  width: 32,
  height: 32,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  padding: 0,
  position: "relative",
};

const panelMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: "calc(100% + 6px)",
  right: 0,
  zIndex: 20,
  width: 180,
  padding: 5,
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-strong, var(--shadow-md))",
};

const panelMenuItemStyle: React.CSSProperties = {
  width: "100%",
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 30,
  padding: "0 8px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: "transparent",
  color: "var(--text-primary)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  textAlign: "left",
};

const badgeStyle: React.CSSProperties = {
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  color: "var(--accent-primary)",
  minWidth: 16,
  height: 16,
  padding: "0 4px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: 999,
  background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)",
};

const rowCardStyle = (active: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "flex-start",
  gap: 8,
  padding: "8px 0",
  borderRadius: 0,
  borderTop: "1px solid var(--border-subtle)",
  background: active ? "color-mix(in oklch, var(--state-info) 6%, transparent)" : "transparent",
  boxShadow: active ? "inset 2px 0 0 var(--state-info)" : "none",
});

const eventButtonStyle = (active: boolean): React.CSSProperties => ({
  display: "flex",
  gap: 8,
  padding: "6px 8px",
  background: active ? "var(--surface-soft)" : "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  fontSize: "var(--text-xs)",
  cursor: "pointer",
  textAlign: "left",
});

const toolCallRowStyle: React.CSSProperties = {
  padding: "6px 8px",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  fontSize: "var(--text-xs)",
};

const jsonBlockStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  color: "var(--text-secondary)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  margin: 0,
  padding: 8,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
};

const sidebarContentStyle: React.CSSProperties = {
  flex: 1,
  overflow: "hidden",
  fontSize: "var(--text-sm)",
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-base)",
};

const panelHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  marginBottom: 8,
  minHeight: 28,
  paddingBottom: 8,
  borderBottom: "1px solid var(--border-subtle)",
};

const sectionLabelStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
  textTransform: "uppercase",
  fontWeight: 700,
  paddingTop: 2,
  letterSpacing: 0,
};

const infoCardStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  padding: 14,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
};

const errorStyle: React.CSSProperties = {
  color: "var(--state-danger)",
  background: "var(--state-danger-soft)",
  border: "1px solid var(--state-danger)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: 8,
  fontSize: "var(--text-xs)",
};
