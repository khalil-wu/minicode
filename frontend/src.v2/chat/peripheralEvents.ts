import { useAppStore } from "../stores";
import type {
  ConnectorsMarketplaceListEvent,
  EnvListEvent,
  FileChangedEvent,
  GitPrStatusEvent,
  SchedulerListEvent,
  ServerEvent,
} from "../protocol/events";
import type { McpServerStatus } from "../stores/types";
import { pushToast } from "../overlays/ToastContainer";

export const handlePeripheralEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "terminal.output":
      // TerminalPanel subscribes to raw websocket events and owns terminal output.
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
      if (e.session_id) {
        s.upsertTerminalSession({
          id: e.session_id,
          pid: e.pid,
          shell: e.shell ?? "",
          cwd: e.cwd ?? "",
          status: "running",
          createdAt: Date.now(),
        });
        s.setActiveTerminalSession(e.session_id);
      }
      return true;
    }
    case "terminal.killed": {
      if (e.session_id) s.removeTerminalSession(e.session_id);
      return true;
    }
    case "terminal.exit": {
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
        }[];
      };
      if (ev.sessions) {
        s.setTerminalSessions(ev.sessions
          .filter((session) => typeof session.session_id === "string" && session.session_id.length > 0)
          .map((session) => ({
            id: session.session_id!,
            pid: session.pid,
            shell: session.shell ?? "",
            cwd: session.cwd ?? "",
            status: session.is_alive === false ? "exited" : "running",
            createdAt: session.started_at ? session.started_at * 1000 : undefined,
          })));
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
        output?: string;
        output_chars?: number;
        total_output_chars?: number;
        truncated?: boolean;
        error?: string;
      };
      const id = ev.session_id ?? "";
      if (!id) return true;
      s.upsertTerminalSnapshot({
        id,
        pid: ev.pid,
        shell: ev.shell ?? "",
        cwd: ev.cwd ?? "",
        status: ev.is_alive === false ? "exited" : "running",
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
      return true;
    }
    case "scheduler.list": {
      const ev = e as SchedulerListEvent;
      if (ev.tasks) s.setScheduledTasks(ev.tasks);
      return true;
    }
    case "connectors.marketplace.list": {
      const ev = e as ConnectorsMarketplaceListEvent;
      if (ev.connectors) s.setMarketplaceConnectors(ev.connectors);
      return true;
    }
    case "background.completed": {
      const ev = e as unknown as {
        command_id?: string;
        command?: string;
        exit_code?: number;
        duration?: number;
      };
      const cmd = ev.command ?? "command";
      const shortCmd = cmd.length > 40 ? `${cmd.slice(0, 37)}...` : cmd;
      const conversationId = String((e as unknown as { conversation_id?: string }).conversation_id || s.conversationId || "");
      pushToast(
        `${shortCmd} finished (exit ${ev.exit_code ?? 0})`,
        ev.exit_code === 0 ? "success" : "error",
      );
      s.addBackgroundTask({
        id: ev.command_id ?? `bg-${Date.now().toString(36)}`,
        command: cmd,
        status: ev.exit_code === 0 ? "completed" : "failed",
        exitCode: ev.exit_code,
        duration: ev.duration,
        timestamp: Date.now(),
      });
      const replayed = Boolean((e as unknown as { __replayed?: boolean }).__replayed);
      if (!replayed && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: ev.exit_code === 0 ? "Background command completed" : "Background command failed",
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
