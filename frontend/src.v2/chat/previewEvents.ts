import { useAppStore } from "../stores";
import type { PreviewRefreshedEvent, ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";

const isReplayed = (event: ServerEvent): boolean =>
  (event as ServerEvent & { __replayed?: boolean }).__replayed === true;

export const handlePreviewEvent = (e: ServerEvent): boolean => {
  if (!e.type.startsWith("preview.")) return false;
  const s = useAppStore.getState();
  const eventConversationId = String(
    (e as unknown as { conversation_id?: unknown }).conversation_id ?? "",
  ).trim();
  const activeConversationId = String(s.conversationId ?? "").trim();
  const eventWorkspaceRoot = normalizeWorkspaceRoot(
    (e as unknown as { workspace_root?: unknown }).workspace_root,
  );
  const activeWorkspaceRoot = normalizeWorkspaceRoot(s.workingDirectory);
  if (
    !eventConversationId
    || !activeConversationId
    || eventConversationId !== activeConversationId
    || !eventWorkspaceRoot
    || eventWorkspaceRoot !== activeWorkspaceRoot
  ) {
    return true;
  }
  switch (e.type) {
    case "preview.servers.updated": {
      const ev = e as unknown as {
        servers?: { port?: number; url?: string; name?: string; framework?: string }[];
      };
      s.setPreviewServers((ev.servers ?? [])
        .filter((server): server is { port: number; url: string; name?: string; framework?: string } =>
          typeof server.port === "number" && typeof server.url === "string",
        )
        .map((server) => ({
          port: server.port,
          url: server.url,
          name: server.name ?? `:${server.port}`,
          framework: server.framework,
        })));
      return true;
    }
    case "preview.server.detected": {
      const ev = e as unknown as { port?: number; url?: string; name?: string; framework?: string };
      if (ev.port && ev.url) {
        s.addPreviewServer({
          port: ev.port,
          url: ev.url,
          name: ev.name ?? `:${ev.port}`,
          framework: ev.framework,
        });
      }
      return true;
    }
    case "preview.server.stopped": {
      const ev = e as unknown as { port?: number };
      if (ev.port) s.removePreviewServer(ev.port);
      return true;
    }
    case "preview.navigated": {
      const ev = e as unknown as { url?: string };
      if (ev.url) s.openLivePreview(ev.url);
      return true;
    }
    case "preview.refreshed": {
      const ev = e as PreviewRefreshedEvent;
      // A replayed file-watcher notification is historical evidence, not a
      // request to reload the live iframe and issue a fresh verification call.
      if (isReplayed(e)) return true;
      window.dispatchEvent(new CustomEvent("preview:auto-refresh", {
        detail: {
          conversation_id: ev.conversation_id,
          workspace_root: ev.workspace_root,
          request_id: ev.request_id,
          path: ev.path,
          url: ev.url,
        },
      }));
      return true;
    }
    case "preview.launch.config": {
      const ev = e as unknown as {
        configs?: {
          name: string;
          command: string;
          cwd: string;
          port: number;
          url: string;
          auto_port?: boolean;
          source?: string;
        }[];
        running?: {
          id: string;
          name: string;
          command: string;
          cwd: string;
          port: number;
          url: string;
          pid?: number;
          status: "starting" | "running" | "ready" | "exited" | "crashed";
          auto_port?: boolean;
          source?: string;
          stderr_tail?: string[];
          output_tail?: { stream: "stdout" | "stderr"; line: string; timestamp?: number }[];
        }[];
      };
      s.setPreviewLaunchConfigs(ev.configs ?? []);
      s.setPreviewLaunchProcesses(ev.running ?? []);
      return true;
    }
    case "preview.launch.started": {
      const ev = e as unknown as {
        id?: string;
        name?: string;
        command?: string;
        cwd?: string;
        port?: number;
        url?: string;
        pid?: number;
        status?: "starting" | "running" | "ready" | "exited" | "crashed";
        stderr_tail?: string[];
        output_tail?: { stream: "stdout" | "stderr"; line: string; timestamp?: number }[];
      };
      if (ev.id && ev.name && ev.command && ev.cwd && typeof ev.port === "number" && ev.url) {
        s.upsertPreviewLaunchProcess({
          id: ev.id,
          name: ev.name,
          command: ev.command,
          cwd: ev.cwd,
          port: ev.port,
          url: ev.url,
          pid: ev.pid,
          status: ev.status ?? "running",
          stderr_tail: ev.stderr_tail,
          output_tail: ev.output_tail,
        });
        s.openLivePreview(ev.url);
      }
      return true;
    }
    case "preview.server.ready": {
      const ev = e as unknown as { id?: string; url?: string; port?: number };
      if (!ev.id || !ev.url || typeof ev.port !== "number") return true;
      const current = useAppStore.getState().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        s.upsertPreviewLaunchProcess({
          ...current,
          url: ev.url,
          port: ev.port,
          status: "ready",
        });
      }
      s.addPreviewServer({
        port: ev.port,
        url: ev.url,
        name: ev.id,
        framework: "launch",
      });
      s.openLivePreview(ev.url);
      return true;
    }
    case "preview.server.output": {
      const ev = e as unknown as {
        id?: string;
        stream?: "stdout" | "stderr";
        line?: string;
      };
      if (!ev.id || !ev.stream || typeof ev.line !== "string") return true;
      const current = useAppStore.getState().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (!current) return true;
      s.upsertPreviewLaunchProcess({
        ...current,
        output_tail: [
          ...(current.output_tail ?? []),
          { stream: ev.stream, line: ev.line, timestamp: Date.now() },
        ].slice(-80),
        stderr_tail: ev.stream === "stderr"
          ? [...(current.stderr_tail ?? []), ev.line].slice(-20)
          : current.stderr_tail,
      });
      return true;
    }
    case "preview.server.crashed": {
      const ev = e as unknown as { id?: string; exit_code?: number | null; stderr_tail?: string[] };
      if (!ev.id) return true;
      const current = useAppStore.getState().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        s.upsertPreviewLaunchProcess({
          ...current,
          status: "crashed",
          stderr_tail: ev.stderr_tail,
        });
      }
      pushToast(`预览服务 ${ev.id} 已退出（${ev.exit_code ?? "未知"}）`, "error");
      return true;
    }
    case "preview.server.unhealthy": {
      const ev = e as unknown as { id?: string; last_error?: string };
      if (!ev.id) return true;
      const current = useAppStore.getState().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        s.upsertPreviewLaunchProcess({
          ...current,
          status: "unhealthy",
        });
      }
      pushToast(`预览服务 ${ev.id} 状态异常：${ev.last_error ?? "无响应"}`, "warning");
      return true;
    }
    case "preview.launch.stopped": {
      const ev = e as unknown as { id?: string; port?: number };
      if (ev.id) s.removePreviewLaunchProcess(ev.id);
      if (typeof ev.port === "number") s.removePreviewServer(ev.port);
      return true;
    }
    case "preview.verified": {
      const ev = e as unknown as {
        url?: string;
        ok?: boolean;
        status_code?: number | null;
        elapsed_ms?: number;
        error?: string;
      };
      if (ev.url) {
        s.setPreviewVerification({
          url: ev.url,
          ok: Boolean(ev.ok),
          status_code: ev.status_code,
          elapsed_ms: ev.elapsed_ms ?? 0,
          error: ev.error,
          checkedAt: Date.now(),
        });
      }
      return true;
    }
    default:
      return false;
  }
};
