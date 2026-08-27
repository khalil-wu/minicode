import { useAppStore } from "../stores";
import type {
  BackgroundCompletedEvent,
  BackgroundStalledEvent,
  BackgroundStartedEvent,
  EnvListEvent,
  FileChangedEvent,
  GitPrStatusEvent,
  SchedulerListEvent,
  ServerEvent,
  WorkspaceImportedEvent,
} from "../protocol/events";
import type { McpServerStatus, TerminalSessionInfo } from "../stores/types";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { addInspectorPayload } from "./inspectorEntries";

const terminalEventConversationId = (event: ServerEvent): string => {
  const value = (event as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof value === "string" ? value.trim() : "";
};

const terminalEventTargetsActiveConversation = (event: ServerEvent): boolean => {
  const owner = terminalEventConversationId(event);
  return Boolean(owner && owner === (useAppStore.getState().conversationId || ""));
};

const isReplayedEvent = (event: ServerEvent): boolean =>
  (event as ServerEvent & { __replayed?: boolean }).__replayed === true;

const eventTimestampMs = (event: ServerEvent): number => {
  const parsed = Date.parse(String(event.timestamp || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const shortBackgroundCommand = (command: string): string =>
  command.length > 40 ? `${command.slice(0, 37)}...` : command;

const eventTargetsActiveWorkspace = (event: ServerEvent): boolean => {
  const state = useAppStore.getState();
  const owner = terminalEventConversationId(event);
  const workspace = normalizeWorkspaceRoot(
    (event as unknown as { workspace_root?: unknown }).workspace_root,
  );
  return Boolean(
    owner
    && owner === String(state.conversationId || "").trim()
    && workspace
    && workspace === normalizeWorkspaceRoot(state.workingDirectory),
  );
};

// A single agent turn can emit dozens of file.changed events. Each one used to
// fire a git working-tree + staged diff pair immediately, and those blocking
// git subprocesses stalled the backend loop that was also delivering stream
// tokens. Collapse bursts into one trailing refresh.
const GIT_CHANGES_REFRESH_DEBOUNCE_MS = 400;
let gitChangesRefreshTimer: ReturnType<typeof setTimeout> | null = null;
const scheduleGitChangesRefresh = () => {
  if (gitChangesRefreshTimer !== null) clearTimeout(gitChangesRefreshTimer);
  gitChangesRefreshTimer = setTimeout(() => {
    gitChangesRefreshTimer = null;
    useAppStore.getState().requestGitChanges();
  }, GIT_CHANGES_REFRESH_DEBOUNCE_MS);
};

export const handlePeripheralEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "terminal.output":
      // TerminalPanel subscribes to raw websocket events and owns terminal output.
      return true;
    case "terminal.resized":
      // Resize is an acknowledgement; xterm already owns the requested size.
      return true;
    case "workspace.imported": {
      const ev = e as WorkspaceImportedEvent;
      const owner = ev.conversation_id.trim();
      const rootPath = ev.workspace_root.trim();
      useAppStore.setState((state) => ({
        conversations: state.conversations.map((conversation) =>
          conversation.id === owner
            ? { ...conversation, workspaceRoot: rootPath, worktreePath: "" }
            : conversation,
        ),
      }));
      if (owner !== String(useAppStore.getState().conversationId || "").trim()) {
        return true;
      }

      s.setWorkingDirectory(rootPath);
      s.bumpFileTreeVersion();
      addInspectorPayload("workspace", `workspace:${owner}:${normalizeWorkspaceRoot(rootPath)}`, {
        event: ev.type,
        conversation_id: owner,
        workspace_root: rootPath,
        request_id: ev.request_id,
        project_name: ev.project.name,
        project_type: ev.project.project_type,
        description: ev.project.description,
        file_count: ev.file_count,
        total_size: ev.project.total_size,
        index_truncated: ev.project.index_truncated,
        summary: ev.summary,
        replayed: isReplayedEvent(e),
      });

      if (!isReplayedEvent(e)) {
        s.requestGitChanges();
        sendClientCommand({ type: "git.pr_status", conversation_id: owner, workspace_root: rootPath });
        sendClientCommand({ type: "scheduler.list", conversation_id: owner, workspace_root: rootPath });
        const projectLabel = [
          ev.project.name || rootPath,
          ev.project.project_type,
          `${ev.file_count} files`,
          ev.project.index_truncated ? "index truncated" : "",
        ].filter(Boolean).join(" · ");
        pushToast(`Opened workspace: ${projectLabel}`, "success", 3500);
      }
      return true;
    }
    case "file.changed": {
      const ev = e as FileChangedEvent;
      if (!eventTargetsActiveWorkspace(e)) return true;
      // Replay restores the durable event log for the transcript. It is not a
      // new filesystem mutation and must not mark an already-open preview or
      // editor buffer as externally changed.
      if (isReplayedEvent(e)) return true;
      if (ev.path) {
        const parsedTimestamp = typeof ev.timestamp === "string" ? Date.parse(ev.timestamp) : Number.NaN;
        s.addFileChange({
          path: ev.path,
          event: ev.event ?? "change",
          timestamp: Number.isFinite(parsedTimestamp) ? parsedTimestamp : Date.now(),
        });
        scheduleGitChangesRefresh();
      }
      return true;
    }
    case "terminal.created": {
      if (e.session_id && terminalEventTargetsActiveConversation(e)) {
        s.upsertTerminalSession({
          id: e.session_id,
          conversationId: terminalEventConversationId(e),
          pid: e.pid,
          shell: e.shell ?? "",
          cwd: e.cwd ?? "",
          status: "running",
          createdAt: Date.now(),
          terminalMode: e.terminal_mode === "pty" ? "pty" : "pipe",
        });
        s.setActiveTerminalSession(e.session_id);
      }
      return true;
    }
    case "terminal.killed": {
      if (e.session_id && terminalEventTargetsActiveConversation(e)) s.removeTerminalSession(e.session_id);
      return true;
    }
    case "terminal.exit": {
      if (!terminalEventTargetsActiveConversation(e)) return true;
      const current = useAppStore.getState().terminalSessions.find((session) => session.id === e.session_id);
      if (e.session_id && current) {
        s.upsertTerminalSession({ ...current, status: "exited" });
      }
      return true;
    }
    case "terminal.list": {
      const ev = e as unknown as {
        sessions?: {
          session_id?: string;
          pid?: number;
          shell?: string;
          cwd?: string;
          is_alive?: boolean;
          started_at?: number;
          terminal_mode?: string;
          conversation_id?: string;
        }[];
        conversation_id?: string;
      };
      if (ev.sessions) {
        const listOwner = String(ev.conversation_id || "").trim();
        if (!listOwner || listOwner !== (s.conversationId || "")) return true;
        const sessions: TerminalSessionInfo[] = ev.sessions
          .filter((session) => (
            typeof session.session_id === "string"
            && session.session_id.length > 0
            && session.conversation_id === listOwner
          ))
          .map((session) => ({
            id: session.session_id!,
            conversationId: session.conversation_id || listOwner,
            pid: session.pid,
            shell: session.shell ?? "",
            cwd: session.cwd ?? "",
            status: session.is_alive === false ? "exited" : "running",
            createdAt: session.started_at ? session.started_at * 1000 : undefined,
            terminalMode: session.terminal_mode === "pty" ? "pty" : "pipe",
          }));
        s.setTerminalSessions(sessions);
        // The list is metadata only.  Rehydrate every owned terminal after a
        // reconnect so output continuity does not depend on the active tab.
        for (const terminal of sessions) {
          sendClientCommand({
            type: "terminal.snapshot.request",
            session_id: terminal.id,
            conversation_id: listOwner,
          });
        }
      }
      return true;
    }
    case "terminal.snapshot": {
      const ev = e as unknown as {
        session_id?: string;
        pid?: number | null;
        shell?: string;
        cwd?: string;
        is_alive?: boolean;
        terminal_mode?: string;
        output?: string;
        output_chars?: number;
        total_output_chars?: number;
        truncated?: boolean;
        error?: string;
        conversation_id?: string;
      };
      const id = ev.session_id ?? "";
      if (!id) return true;
      const snapshotOwner = String(ev.conversation_id || "").trim();
      if (!snapshotOwner || snapshotOwner !== (s.conversationId || "")) return true;
      s.upsertTerminalSnapshot({
        id,
        conversationId: snapshotOwner,
        pid: ev.pid,
        shell: ev.shell ?? "",
        cwd: ev.cwd ?? "",
        status: ev.is_alive === false ? "exited" : "running",
        terminalMode: ev.terminal_mode === "pty" ? "pty" : "pipe",
        output: ev.output ?? "",
        outputChars: ev.output_chars,
        totalOutputChars: ev.total_output_chars,
        truncated: ev.truncated,
        capturedAt: Date.now(),
        error: ev.error,
      });
      return true;
    }
    case "mcp_status": {
      const ev = e as unknown as {
        servers?: {
          name: string;
          status: string;
          tools?: number;
          tools_count?: number;
          capabilities?: McpServerStatus["capabilities"];
          transport?: string;
          command?: string;
          args?: string[];
          env?: Record<string, string>;
          headers?: Record<string, string>;
          headers_helper?: string;
          oauth?: { client_id?: string; callback_port?: number };
          env_vars?: Array<string | { name?: string; source?: string }>;
          cwd?: string;
          url?: string;
          auto_start?: boolean;
          editable?: boolean;
          enabled?: boolean;
          disabled_reason?: string;
          source?: string;
          approval_status?: "approved" | "rejected" | "pending" | "not_applicable";
          config_path?: string;
          project_workspace?: string;
          error?: string;
          auth_status?: "unsupported" | "not_logged_in" | "oauth";
          phase?: string;
          recoverable?: boolean;
          requires_user_action?: boolean;
          setup_hint?: string;
          docs_url?: string;
          cleanup?: {
            pending: boolean;
            reason: string;
            requested_at?: number | null;
            completed_at?: number | null;
          };
          operation_failures?: Array<{
            operation: string;
            failure_kind: string;
            message: string;
            retryable: boolean;
          }>;
        }[];
      };
      if (ev.servers) {
        const prev = useAppStore.getState().mcpServers;
        s.setMcpServers(ev.servers.map((srv) => ({
          name: srv.name,
          status: srv.status as "connected" | "disconnected" | "error" | "reconnecting",
          tools: srv.tools_count ?? srv.tools,
          capabilities: srv.capabilities,
          transport: srv.transport as McpServerStatus["transport"],
          command: srv.command,
          args: srv.args,
          env: srv.env,
          headers: srv.headers,
          headersHelper: srv.headers_helper,
          oauth: srv.oauth ? {
            clientId: srv.oauth.client_id,
            callbackPort: srv.oauth.callback_port,
          } : undefined,
          envVars: srv.env_vars,
          cwd: srv.cwd,
          url: srv.url,
          autoStart: srv.auto_start,
          editable: srv.editable,
          enabled: srv.enabled,
          disabledReason: srv.disabled_reason,
          source: srv.source,
          approvalStatus: srv.approval_status,
          configPath: srv.config_path,
          projectWorkspace: srv.project_workspace,
          lastError: srv.error || undefined,
          authStatus: srv.auth_status,
          phase: srv.phase as McpServerStatus["phase"],
          recoverable: srv.recoverable,
          requiresUserAction: srv.requires_user_action,
          setupHint: srv.setup_hint,
          docsUrl: srv.docs_url,
          cleanup: srv.cleanup ? {
            pending: srv.cleanup.pending,
            reason: srv.cleanup.reason,
            requestedAt: srv.cleanup.requested_at,
            completedAt: srv.cleanup.completed_at,
          } : undefined,
          operationFailures: srv.operation_failures?.map((failure) => ({
            operation: failure.operation,
            failureKind: failure.failure_kind,
            message: failure.message,
            retryable: failure.retryable,
          })),
        })));
        for (const srv of ev.servers) {
          const was = prev.find((p) => p.name === srv.name);
          if (was?.status !== "error" && srv.status === "error") {
        pushToast(`MCP：${srv.name} 出错`, "error");
          }
        }
      }
      return true;
    }
    case "env.list": {
      const ev = e as EnvListEvent;
      if (ev.entries) s.setEnvVars(ev.entries);
      return true;
    }
    case "git.pr_status": {
      const ev = e as GitPrStatusEvent;
      if (!eventTargetsActiveWorkspace(e)) return true;
      s.setPrStatus(ev.pr ?? null, ev.checks ?? []);
      if (ev.pr) {
        const checks = ev.checks ?? [];
        const failedChecks = checks.filter((check) => /fail|error|cancel/i.test(check.status));
        const ciStatus = failedChecks.length > 0
          ? "failed"
          : checks.some((check) => /pending|queued|in_progress|running/i.test(check.status))
            ? "running"
            : "passed";
        s.setPRMonitor({
          prNumber: ev.pr.number,
          prUrl: ev.pr.url,
          ciStatus,
          failedChecks: failedChecks.map((check) => check.name),
          autoFix: Boolean(ev.automation?.auto_fix),
          autoMerge: Boolean(ev.automation?.auto_merge),
          lastCheckedAt: Date.now(),
          checksCount: checks.length,
        });
      } else {
        s.setPRMonitor(null);
      }
      return true;
    }
    case "scheduler.list": {
      const ev = e as SchedulerListEvent;
      if (!eventTargetsActiveWorkspace(e)) return true;
      if (ev.tasks) s.setScheduledTasks(ev.tasks);
      if (ev.runs) s.setScheduledTaskRuns(ev.runs);
      return true;
    }
    case "background.started": {
      const ev = e as BackgroundStartedEvent;
      const conversationId = String(ev.conversation_id || "").trim();
      if (!ev.command_id || !conversationId) return true;
      s.addBackgroundTask({
        id: ev.command_id,
        command: ev.command ?? ev.description ?? "后台命令",
        status: "running",
        timestamp: ev.started_at ? ev.started_at * 1000 : Date.now(),
        conversationId,
        cwd: ev.cwd,
      });
      return true;
    }
    case "background.stalled": {
      const ev = e as BackgroundStalledEvent;
      const conversationId = String(ev.conversation_id || "").trim();
      if (!ev.command_id || !conversationId) return true;
      const existing = s.backgroundTasks.find((task) => (
        task.id === ev.command_id && task.conversationId === conversationId
      ));
      const command = ev.command ?? ev.description ?? existing?.command ?? "后台命令";
      s.addBackgroundTask({
        ...existing,
        id: ev.command_id,
        command,
        status: "stalled",
        timestamp: existing?.timestamp ?? eventTimestampMs(e),
        conversationId,
        stalledTail: ev.tail,
        stalledAdvice: ev.advice,
        stalledAt: eventTimestampMs(e),
      });
      if (!isReplayedEvent(e) && terminalEventTargetsActiveConversation(e)) {
        const promptTail = ev.tail.trim().replace(/\s+/g, " ");
        pushToast(
          `后台命令等待输入：${shortBackgroundCommand(command)}${promptTail ? ` · ${promptTail.slice(0, 120)}` : ""}`,
          "warning",
          7000,
        );
      }
      if (!isReplayedEvent(e) && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: "后台命令等待输入",
          body: shortBackgroundCommand(command),
          target: { kind: "conversation" as const, conversationId },
        }));
      }
      return true;
    }
    case "background.completed": {
      const ev = e as BackgroundCompletedEvent;
      const conversationId = String(ev.conversation_id || "").trim();
      if (!ev.command_id || !conversationId) return true;
      const existing = s.backgroundTasks.find((task) => (
        task.id === ev.command_id && task.conversationId === conversationId
      ));
      const cmd = ev.command ?? ev.description ?? existing?.command ?? "后台命令";
      const shortCmd = shortBackgroundCommand(cmd);
      // An unproven exit is not a clean termination: the backend kept the PID
      // for a later reaper, so reporting "已取消" would tell the user the
      // process is gone when it may still be writing to the workspace.
      const cleanupPending = ev.cleanup_pending === true;
      const status = cleanupPending
        ? "failed"
        : ev.status === "cancelled"
          ? "cancelled"
          : ev.status === "failed" || ev.status === "interrupted" || (ev.exit_code != null && ev.exit_code !== 0)
            ? "failed"
            : "completed";
      const replayed = isReplayedEvent(e);
      if (!replayed && terminalEventTargetsActiveConversation(e)) {
        pushToast(
          cleanupPending
            ? `${shortCmd} 已请求停止，但未能确认进程退出`
            : status === "cancelled"
              ? `${shortCmd} 已取消`
              : ev.status === "interrupted"
                ? `${shortCmd} 因上次 MiniCode 进程退出而中断`
                : `${shortCmd} 已结束（exit ${ev.exit_code ?? 0}）`,
          status === "completed" ? "success" : status === "cancelled" ? "info" : "error",
        );
      }
      s.addBackgroundTask({
        ...existing,
        id: ev.command_id,
        command: cmd,
        status,
        exitCode: ev.exit_code,
        duration: ev.duration,
        timestamp: existing?.timestamp ?? (ev.started_at ? ev.started_at * 1000 : eventTimestampMs(e)),
        completedAt: ev.completed_at ? ev.completed_at * 1000 : eventTimestampMs(e),
        conversationId,
        outputPreview: ev.output,
      });
      if (!replayed && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: cleanupPending
            ? "后台命令未能确认退出"
            : status === "completed"
              ? "后台命令已完成"
              : status === "cancelled"
                ? "后台命令已取消"
                : "后台命令失败",
          body: shortCmd,
          ...(conversationId ? { target: { kind: "conversation" as const, conversationId } } : {}),
        }));
      }
      return true;
    }
    default:
      return false;
  }
};
