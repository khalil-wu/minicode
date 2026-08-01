import { useAppStore } from "../stores";
import type {
  ConnectorsMarketplaceListEvent,
  EnvListEvent,
  FileChangedEvent,
  GitPrStatusEvent,
  SchedulerListEvent,
  ServerEvent,
} from "../protocol/events";
import type { McpServerStatus, TerminalSessionInfo } from "../stores/types";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";

const terminalEventConversationId = (event: ServerEvent): string => {
  const value = (event as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof value === "string" ? value.trim() : "";
};

const terminalEventTargetsActiveConversation = (event: ServerEvent): boolean => {
  const owner = terminalEventConversationId(event);
  return Boolean(owner && owner === (useAppStore.getState().conversationId || ""));
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
      const ev = e as unknown as {
        project?: {
          root_path?: string;
          rootPath?: string;
          path?: string;
          name?: string;
        };
      };
      const rootPath = ev.project?.root_path ?? ev.project?.rootPath ?? ev.project?.path;
      if (rootPath) {
        s.setWorkingDirectory(rootPath);
        s.bumpFileTreeVersion();
        s.requestGitChanges();
        sendClientCommand({ type: "git.pr_status" });
        sendClientCommand({ type: "scheduler.list" });
        useAppStore.setState((state) => ({
          conversations: state.conversations.map((conversation) =>
            conversation.id === state.conversationId
              ? { ...conversation, workspaceRoot: rootPath, worktreePath: "" }
              : conversation,
          ),
        }));
        pushToast(`Workspace opened: ${ev.project?.name ?? rootPath}`, "success", 3000);
      }
      return true;
    }
    case "file.changed": {
      const ev = e as FileChangedEvent & { timestamp?: number };
      if (ev.path) {
        s.addFileChange({ path: ev.path, event: ev.event ?? "change", timestamp: ev.timestamp ?? Date.now() });
        s.requestGitChanges();
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
          sendClientCommand({ type: "terminal.snapshot.request", session_id: terminal.id });
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
          transport?: string;
          error?: string;
          phase?: string;
          recoverable?: boolean;
          requires_user_action?: boolean;
          setup_hint?: string;
          docs_url?: string;
        }[];
      };
      if (ev.servers) {
        const prev = useAppStore.getState().mcpServers;
        s.setMcpServers(ev.servers.map((srv) => ({
          name: srv.name,
          status: srv.status as "connected" | "disconnected" | "error" | "reconnecting",
          tools: srv.tools_count ?? srv.tools,
          transport: srv.transport as "stdio" | "http" | "sse" | "streamable-http" | undefined,
          lastError: srv.error || undefined,
          phase: srv.phase as McpServerStatus["phase"],
          recoverable: srv.recoverable,
          requiresUserAction: srv.requires_user_action,
          setupHint: srv.setup_hint,
          docsUrl: srv.docs_url,
        })));
        for (const srv of ev.servers) {
          const was = prev.find((p) => p.name === srv.name);
          if (was?.status !== "error" && srv.status === "error") {
            pushToast(`MCP: ${srv.name} error`, "error");
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
      if (ev.tasks) s.setScheduledTasks(ev.tasks);
      if (ev.runs) s.setScheduledTaskRuns(ev.runs);
      return true;
    }
    case "connectors.marketplace.list": {
      const ev = e as ConnectorsMarketplaceListEvent;
      if (ev.connectors) s.setMarketplaceConnectors(ev.connectors);
      return true;
    }
    case "background.started": {
      const ev = e as unknown as {
        command_id?: string;
        command?: string;
        description?: string;
        cwd?: string;
        started_at?: number;
        conversation_id?: string;
      };
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
    case "background.completed": {
      const ev = e as unknown as {
        command_id?: string;
        command?: string;
        exit_code?: number;
        duration?: number;
        status?: string;
        started_at?: number;
        completed_at?: number;
        conversation_id?: string;
      };
      const conversationId = String(ev.conversation_id || "").trim();
      if (!conversationId) return true;
      const cmd = ev.command ?? "command";
      const shortCmd = cmd.length > 40 ? `${cmd.slice(0, 37)}...` : cmd;
      const status = ev.status === "cancelled"
        ? "cancelled"
        : ev.status === "failed" || (ev.exit_code != null && ev.exit_code !== 0)
          ? "failed"
          : "completed";
      pushToast(
        status === "cancelled" ? `${shortCmd} 已取消` : `${shortCmd} finished (exit ${ev.exit_code ?? 0})`,
        status === "completed" ? "success" : status === "cancelled" ? "info" : "error",
      );
      s.addBackgroundTask({
        id: ev.command_id ?? `bg-${Date.now().toString(36)}`,
        command: cmd,
        status,
        exitCode: ev.exit_code,
        duration: ev.duration,
        timestamp: ev.started_at ? ev.started_at * 1000 : Date.now(),
        completedAt: ev.completed_at ? ev.completed_at * 1000 : Date.now(),
        conversationId,
      });
      const replayed = Boolean((e as unknown as { __replayed?: boolean }).__replayed);
      if (!replayed && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: status === "completed"
            ? "Background command completed"
            : status === "cancelled"
              ? "Background command cancelled"
              : "Background command failed",
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
