import { useAppStore } from "../stores";
import type {
  PreviewLaunchConfigEvent,
  PreviewLaunchStartedEvent,
  PreviewRefreshedEvent,
  PreviewServerUnhealthyEvent,
  ServerEvent,
} from "../protocol/events";
import { isReplayedEvent as isReplayed } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { previewUrlsShareOrigin, selectPreviewForConversation } from "../lib/preview-projection";
import { refreshWebInBrowser } from "./openWebInBrowser";

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
  const isActiveEvent = Boolean(
    eventConversationId
    && activeConversationId
    && eventConversationId === activeConversationId,
  );
  const owner = s.conversations.find((conversation) => conversation.id === eventConversationId);
  const ownerWorkspaceRoot = isActiveEvent
    ? activeWorkspaceRoot
    : normalizeWorkspaceRoot(owner?.worktreePath || owner?.workspaceRoot) || activeWorkspaceRoot;
  const isKnownConversation = Boolean(
    eventConversationId
    && (
      isActiveEvent
      || s.conversations.some((conversation) => conversation.id === eventConversationId)
      || Object.prototype.hasOwnProperty.call(s.sideChats, eventConversationId)
      || Object.prototype.hasOwnProperty.call(s.conversationMessages, eventConversationId)
      || Object.prototype.hasOwnProperty.call(s.conversationWorkbenchStates, eventConversationId)
    ),
  );
  if (
    !eventConversationId
    || !isKnownConversation
    || !eventWorkspaceRoot
    || !ownerWorkspaceRoot
    || eventWorkspaceRoot !== ownerWorkspaceRoot
  ) {
    return true;
  }
  const updateLivePreview = (url: string): void => {
    if (isActiveEvent && !isReplayed(e)) {
      s.openLivePreview(url, eventConversationId);
      return;
    }
    // A background conversation may report a new preview URL during an active
    // main-chat turn. Preserve its state, but do not steal focus or reload the
    // current iframe.
    s.setLivePreviewUrl(url, eventConversationId);
  };
  const previewForEvent = () => selectPreviewForConversation(useAppStore.getState(), eventConversationId);
  const invalidateVerification = (url: string): void => {
    const verification = previewForEvent().previewVerification;
    if (verification && previewUrlsShareOrigin(verification.url, url)) {
      s.setPreviewVerification(null, eventConversationId);
    }
  };
  switch (e.type) {
    case "preview.servers.updated": {
      const ev = e as unknown as {
        servers?: { port?: number; url?: string; name?: string; framework?: string }[];
      };
      for (const current of previewForEvent().previewServers) {
        if (!ev.servers?.some((server) => server.port === current.port && server.url === current.url)) {
          invalidateVerification(current.url);
        }
      }
      s.setPreviewServers((ev.servers ?? [])
        .filter((server): server is { port: number; url: string; name?: string; framework?: string } =>
          typeof server.port === "number" && typeof server.url === "string",
        )
        .map((server) => ({
          port: server.port,
          url: server.url,
          name: server.name ?? `:${server.port}`,
          framework: server.framework,
        })), eventConversationId);
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
        }, eventConversationId);
      }
      return true;
    }
    case "preview.server.stopped": {
      const ev = e as unknown as { port?: number };
      if (ev.port) {
        const current = previewForEvent().previewServers.find((server) => server.port === ev.port);
        if (current) invalidateVerification(current.url);
        s.removePreviewServer(ev.port, eventConversationId);
      }
      return true;
    }
    case "preview.navigated": {
      const ev = e as unknown as { url?: string };
      if (ev.url) updateLivePreview(ev.url);
      return true;
    }
    case "preview.refreshed": {
      const ev = e as PreviewRefreshedEvent;
      // A replayed file-watcher notification is historical evidence, not a
      // request to reload the live iframe and issue a fresh verification call.
      if (isReplayed(e) || !isActiveEvent) return true;
      const url = ev.url || previewForEvent().livePreviewUrl;
      if (url) refreshWebInBrowser(url, eventConversationId, eventWorkspaceRoot);
      return true;
    }
    case "preview.launch.config": {
      const ev = e as PreviewLaunchConfigEvent;
      for (const current of previewForEvent().previewLaunchProcesses) {
        const next = ev.running?.find((process) => process.id === current.id);
        if (!next || next.pid !== current.pid || next.url !== current.url || !["running", "ready"].includes(next.status)) {
          invalidateVerification(current.url);
        }
      }
      s.setPreviewLaunchConfigs(ev.configs ?? [], eventConversationId);
      s.setPreviewLaunchProcesses(ev.running ?? [], eventConversationId);
      return true;
    }
    case "preview.launch.started": {
      const ev = e as PreviewLaunchStartedEvent;
      if (ev.id && ev.name && ev.command && ev.cwd && typeof ev.port === "number" && typeof ev.url === "string") {
        invalidateVerification(ev.url);
        s.upsertPreviewLaunchProcess({
          id: ev.id,
          name: ev.name,
          command: ev.command,
          cwd: ev.cwd,
          port: ev.port,
          url: ev.url,
          pid: ev.pid,
          status: ev.status ?? "running",
          cleanup_pending: ev.cleanup_pending,
          cleanup_reason: ev.cleanup_reason,
          stderr_tail: ev.stderr_tail,
          output_tail: ev.output_tail,
        }, eventConversationId);
        if (ev.url) {
          if (ev.status === "ready" || ev.status === "running") updateLivePreview(ev.url);
          else s.setLivePreviewUrl(ev.url, eventConversationId);
        }
      }
      return true;
    }
    case "preview.server.ready": {
      const ev = e as unknown as { id?: string; url?: string; port?: number };
      if (!ev.id || !ev.url || typeof ev.port !== "number") return true;
      const current = previewForEvent().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        s.upsertPreviewLaunchProcess({
          ...current,
          url: ev.url,
          port: ev.port,
          status: "ready",
        }, eventConversationId);
      }
      s.addPreviewServer({
        port: ev.port,
        url: ev.url,
        name: ev.id,
        framework: "launch",
      }, eventConversationId);
      updateLivePreview(ev.url);
      return true;
    }
    case "preview.server.output": {
      const ev = e as unknown as {
        id?: string;
        stream?: "stdout" | "stderr";
        line?: string;
      };
      if (!ev.id || !ev.stream || typeof ev.line !== "string") return true;
      const current = previewForEvent().previewLaunchProcesses.find((process) => process.id === ev.id);
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
      }, eventConversationId);
      return true;
    }
    case "preview.server.crashed": {
      const ev = e as unknown as { id?: string; exit_code?: number | null; stderr_tail?: string[] };
      if (!ev.id) return true;
      const current = previewForEvent().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        invalidateVerification(current.url);
        s.upsertPreviewLaunchProcess({
          ...current,
          status: "crashed",
          stderr_tail: ev.stderr_tail,
        }, eventConversationId);
        s.removePreviewServer(current.port, eventConversationId);
      }
      if (isActiveEvent) {
        pushToast(`预览服务 ${ev.id} 已退出（${ev.exit_code ?? "未知"}）`, "error");
      }
      return true;
    }
    case "preview.server.unhealthy": {
      const ev = e as PreviewServerUnhealthyEvent;
      if (!ev.id) return true;
      const current = previewForEvent().previewLaunchProcesses.find((process) => process.id === ev.id);
      if (current) {
        invalidateVerification(current.url);
        s.upsertPreviewLaunchProcess({
          ...current,
          status: "unhealthy",
          cleanup_pending: ev.cleanup_pending ?? current.cleanup_pending,
          cleanup_reason: ev.cleanup_reason ?? current.cleanup_reason,
        }, eventConversationId);
      }
      if (isActiveEvent) {
        pushToast(`预览服务 ${ev.id} 状态异常：${ev.last_error ?? "无响应"}`, "warning");
      }
      return true;
    }
    case "preview.launch.stopped": {
      const ev = e as unknown as { id?: string; port?: number };
      const preview = previewForEvent();
      const current = preview.previewLaunchProcesses.find((process) => process.id === ev.id)
        ?? preview.previewServers.find((server) => server.port === ev.port);
      if (current) invalidateVerification(current.url);
      if (ev.id) s.removePreviewLaunchProcess(ev.id, eventConversationId);
      if (typeof ev.port === "number") s.removePreviewServer(ev.port, eventConversationId);
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
        }, eventConversationId);
      }
      return true;
    }
    default:
      return false;
  }
};
