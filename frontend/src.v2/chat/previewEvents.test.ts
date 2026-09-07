/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../protocol/events";
import { useAppStore } from "../stores";
import { selectPreviewForConversation } from "../lib/preview-projection";
import { buildActivitySidebarState } from "../shell/activitySidebarState";
import { handlePreviewEvent } from "./previewEvents";
import { handleCommandResultEvent } from "./commandResultEvents";
import { __resetOpenWebInBrowserForTests, subscribeBrowserOpenRequests } from "./openWebInBrowser";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

beforeEach(__resetOpenWebInBrowserForTests);
afterEach(__resetOpenWebInBrowserForTests);

describe("live preview browser routing", () => {
  const url = "http://localhost:4173";
  const launch = {
    id: "web", name: "web", command: "npm run dev", cwd: "C:/active",
    port: 4173, url, pid: 1001, status: "starting",
  };
  const send = (event: Record<string, unknown>, conversationId = "conv-active") => handlePreviewEvent({
    conversation_id: conversationId, workspace_root: "C:/active", ...event,
  } as ServerEvent);
  const browserRequests = vi.fn();

  beforeEach(() => {
    browserRequests.mockClear();
    subscribeBrowserOpenRequests(browserRequests);
    useAppStore.setState({
      conversationId: "conv-active", workingDirectory: "C:/active",
      conversations: [], sideChats: {}, conversationMessages: {}, conversationWorkbenchStates: {},
      previewServers: [], previewLaunchConfigs: [], previewLaunchProcesses: [], previewArtifact: null,
      previewVerification: null, livePreviewUrl: null, previewOwnerConversationId: null,
      rightStackTab: "tasks", rightStackTabLocked: false, rightPanelOpen: false,
    });
  });

  it.each([url, ""])("waits for readiness before routing a launch with URL '%s'", (startingUrl) => {
    send({ type: "preview.launch.started", ...launch, url: startingUrl });

    expect(useAppStore.getState().previewLaunchProcesses).toEqual([
      expect.objectContaining({ id: launch.id, status: "starting", url: startingUrl }),
    ]);
    expect(browserRequests).not.toHaveBeenCalled();
    expect(useAppStore.getState().rightStackTab).toBe("tasks");
    expect(useAppStore.getState().rightPanelOpen).toBe(false);

    send({ type: "preview.server.ready", id: launch.id, port: launch.port, url });

    expect(browserRequests).toHaveBeenCalledExactlyOnceWith({
      id: 1, url: `${url}/`, conversationId: "conv-active",
    });
    expect(useAppStore.getState().previewLaunchProcesses[0]).toMatchObject({ status: "ready", url });
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
  });

  it.each([
    { type: "preview.launch.started", ...launch, status: "ready" },
    { type: "preview.launch.started", ...launch, status: "running" },
    { type: "preview.server.ready", id: launch.id, port: launch.port, url },
    { type: "preview.navigated", url },
  ])("restores replayed $type state without opening the browser", (event) => {
    send({ ...event, replayed: true });

    expect(useAppStore.getState().livePreviewUrl).toBe(url);
    expect(browserRequests).not.toHaveBeenCalled();
    expect(useAppStore.getState().rightStackTab).toBe("tasks");
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
  });

  it("caches background readiness without borrowing the active browser owner", () => {
    useAppStore.setState({ conversationMessages: { "conv-background": [] } });
    send({ type: "preview.launch.started", ...launch }, "conv-background");
    send({ type: "preview.server.ready", id: launch.id, port: launch.port, url }, "conv-background");

    expect(selectPreviewForConversation(useAppStore.getState(), "conv-background")).toMatchObject({
      livePreviewUrl: url, previewLaunchProcesses: [expect.objectContaining({ status: "ready" })],
    });
    expect(useAppStore.getState().livePreviewUrl).toBeNull();
    expect(useAppStore.getState().previewOwnerConversationId).toBeNull();
    expect(browserRequests).not.toHaveBeenCalled();
    expect(useAppStore.getState().rightStackTab).toBe("tasks");
  });

  it("reuses the existing browser request deduplication for an already-ready launch", () => {
    send({ type: "preview.launch.started", ...launch, status: "ready" });
    send({ type: "preview.server.ready", id: launch.id, port: launch.port, url });

    expect(browserRequests).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().rightStackTab).toBe("browser");
  });

  it("preserves the file preview and only routes explicit opens for the active owner", () => {
    const artifact = { name: "notes.md", content: "# Notes", mediaType: "text/markdown" };
    useAppStore.getState().setPreviewArtifact(artifact);

    useAppStore.getState().openLivePreview("localhost:5173", "conv-background");
    expect(browserRequests).not.toHaveBeenCalled();
    expect(selectPreviewForConversation(useAppStore.getState(), "conv-background").livePreviewUrl)
      .toBe("http://localhost:5173");

    useAppStore.getState().openLivePreview("localhost:4173", "conv-active");
    expect(browserRequests).toHaveBeenCalledExactlyOnceWith({
      id: 1, url: `${url}/`, conversationId: "conv-active",
    });
    expect(useAppStore.getState().previewArtifact).toEqual(artifact);
    expect(selectPreviewForConversation(useAppStore.getState(), "conv-active").previewArtifact).toEqual(artifact);
  });
});

describe("handlePreviewEvent owner projection", () => {
  beforeEach(() => {
    useAppStore.setState({
      conversationId: "conv-active",
      workingDirectory: "C:/active",
      previewServers: [],
      previewLaunchConfigs: [],
      previewLaunchProcesses: [],
      previewVerification: null,
      livePreviewUrl: null,
      previewOwnerConversationId: null,
      conversationWorkbenchStates: {},
      conversationMessages: {},
      conversations: [],
      sideChats: {},
    });
  });

  it("ignores preview events owned by another conversation", () => {
    expect(handlePreviewEvent({
      type: "preview.launch.config",
      conversation_id: "conv-other",
      workspace_root: "C:/active",
      configs: [{
        name: "other",
        command: "npm run dev",
        cwd: "C:/other",
        port: 5173,
        url: "http://localhost:5173",
      }],
      running: [],
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().previewLaunchConfigs).toEqual([]);
  });

  it("fails closed when a preview event has no owner", () => {
    expect(handlePreviewEvent({
      type: "preview.server.ready",
      workspace_root: "C:/active",
      id: "server-ownerless",
      port: 5173,
      url: "http://localhost:5173",
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().previewServers).toEqual([]);
    expect(useAppStore.getState().livePreviewUrl).toBeNull();
  });

  it("applies preview snapshots only for the active conversation", () => {
    expect(handlePreviewEvent({
      type: "preview.launch.config",
      conversation_id: "conv-active",
      workspace_root: "C:/active",
      configs: [{
        name: "active",
        command: "npm run dev",
        cwd: "C:/active",
        port: 4173,
        url: "http://localhost:4173",
      }],
      running: [],
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().previewLaunchConfigs).toEqual([
      expect.objectContaining({ name: "active", port: 4173 }),
    ]);
  });

  it("removes a naturally exited preview and its port without removing another server", () => {
    for (const port of [4173, 5173]) {
      handlePreviewEvent({
        type: "preview.launch.started",
        conversation_id: "conv-active",
        workspace_root: "C:/active",
        id: `web-${port}`,
        name: `web-${port}`,
        command: "npm run dev",
        cwd: "C:/active",
        port,
        url: `http://localhost:${port}`,
        status: "starting",
      } as ServerEvent);
      handlePreviewEvent({
        type: "preview.server.ready",
        conversation_id: "conv-active",
        workspace_root: "C:/active",
        id: `web-${port}`,
        port,
        url: `http://localhost:${port}`,
      } as ServerEvent);
    }

    handlePreviewEvent({
      type: "preview.launch.stopped",
      conversation_id: "conv-active",
      workspace_root: "C:/active",
      id: "web-4173",
      port: 4173,
      status: "exited",
    } as ServerEvent);

    expect(useAppStore.getState().previewLaunchProcesses.map((process) => process.id)).toEqual(["web-5173"]);
    expect(useAppStore.getState().previewServers.map((server) => server.port)).toEqual([5173]);
  });

  it("ignores a same-conversation preview event from a differently-cased POSIX root", () => {
    useAppStore.setState({
      workingDirectory: "/tmp/Project",
      previewServers: [],
    });

    handlePreviewEvent({
      type: "preview.servers.updated",
      conversation_id: "conv-active",
      workspace_root: "/tmp/project",
      servers: [{ port: 4173, url: "http://localhost:4173" }],
    } as ServerEvent);

    expect(useAppStore.getState().previewServers).toEqual([]);
  });

  it("projects exact live refresh evidence once and suppresses replay side effects", () => {
    const listener = vi.fn();
    window.addEventListener("preview:auto-refresh", listener);
    try {
      expect(handlePreviewEvent({
        type: "preview.refreshed",
        conversation_id: "conv-active",
        workspace_root: "C:/active",
        request_id: "refresh-1",
        path: "src/app.ts",
        url: "http://localhost:5173/app",
      } as ServerEvent)).toBe(true);
      expect(handlePreviewEvent({
        type: "preview.refreshed",
        conversation_id: "conv-active",
        workspace_root: "C:/active",
        request_id: "refresh-replayed",
        path: "src/old.ts",
        replayed: true,
      } as unknown as ServerEvent)).toBe(true);

      expect(listener).toHaveBeenCalledTimes(1);
      expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
        conversation_id: "conv-active",
        workspace_root: "C:/active",
        request_id: "refresh-1",
        path: "src/app.ts",
        url: "http://localhost:5173/app",
      });
    } finally {
      window.removeEventListener("preview:auto-refresh", listener);
    }
  });
});

describe("preview verification lifecycle", () => {
  const launch = {
    id: "web", name: "web", command: "npm run dev", cwd: "C:/active",
    port: 4173, url: "http://localhost:4173", pid: 1001, status: "starting",
  };
  const send = (event: Record<string, unknown>, conversationId = "conv-active") => handlePreviewEvent({
    conversation_id: conversationId, workspace_root: "C:/active", ...event,
  } as ServerEvent);
  const verify = (url = launch.url, conversationId = "conv-active") => {
    send({ type: "preview.navigated", url }, conversationId);
    send({ type: "preview.verified", url, ok: true, status_code: 200, elapsed_ms: 1 }, conversationId);
  };
  const browserFor = (conversationId = "conv-active") => buildActivitySidebarState({
    ...selectPreviewForConversation(useAppStore.getState(), conversationId),
    conversationId, messages: [], todos: [], plan: null, agentProgress: [],
  }).browser[0];

  beforeEach(() => {
    useAppStore.setState({
      conversationId: "conv-active", workingDirectory: "C:/active",
      conversations: [], sideChats: {}, conversationMessages: {}, conversationWorkbenchStates: {},
      previewServers: [], previewLaunchConfigs: [], previewLaunchProcesses: [],
      previewVerification: null, livePreviewUrl: null, previewOwnerConversationId: null,
    });
    send({ type: "preview.launch.started", ...launch });
    send({ type: "preview.server.ready", id: launch.id, port: launch.port, url: launch.url });
    verify();
  });

  it.each([
    ["launch stop", { type: "preview.launch.stopped", id: launch.id, port: launch.port }, "idle"],
    ["crash", { type: "preview.server.crashed", id: launch.id, exit_code: 1 }, "failed"],
    ["unhealthy", { type: "preview.server.unhealthy", id: launch.id }, "failed"],
    ["restart", { type: "preview.launch.started", ...launch, pid: 1002 }, "running"],
    ["detected stop", { type: "preview.server.stopped", port: launch.port }, "running"],
    ["launch snapshot", { type: "preview.launch.config", configs: [], running: [] }, "idle"],
    ["server snapshot", { type: "preview.servers.updated", servers: [] }, "running"],
  ])("invalidates the old success on %s before sidebar projection", (_label, event, status) => {
    expect(browserFor().status).toBe("verified");
    send(event as Record<string, unknown>);

    const state = useAppStore.getState();
    expect(state.previewVerification).toBeNull();
    expect(selectPreviewForConversation(state, "conv-active").previewVerification).toBeNull();
    expect(state.livePreviewUrl).toBe(launch.url);
    expect(browserFor().status).toBe(status);
    expect(browserFor().detail).not.toBe("200 in 1ms");
  });

  it("removes the crashed launch port but retains its failure evidence", () => {
    send({ type: "preview.server.crashed", id: launch.id, exit_code: 1, stderr_tail: ["fixture error"] });

    const state = useAppStore.getState();
    expect(state.previewServers).toEqual([]);
    expect(state.previewLaunchProcesses[0]).toMatchObject({ status: "crashed", stderr_tail: ["fixture error"] });
    expect(browserFor()).toMatchObject({ status: "failed", detail: "crashed" });
  });

  it("invalidates a verified subpath of the stopped server", () => {
    verify(`${launch.url}/app?tab=second`);
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port });

    expect(useAppStore.getState().previewVerification).toBeNull();
    expect(browserFor().status).toBe("idle");
  });

  it.each([
    "http://localhost:5173", "http://example.test:4173", "https://localhost:4173",
    "http://www.localhost:4173",
  ])("preserves verification belonging to another origin: %s", (url) => {
    verify(url);
    send({ type: "preview.server.crashed", id: launch.id, exit_code: 1 });

    expect(useAppStore.getState().previewVerification?.url).toBe(url);
    expect(browserFor().status).toBe("verified");
    useAppStore.getState().setPreviewVerification(null, "conv-active");
    expect(browserFor().status).toBe("idle");
  });

  it("invalidates only the background conversation at the same URL", () => {
    useAppStore.setState({ conversationMessages: { "conv-background": [] } });
    send({ type: "preview.launch.started", ...launch }, "conv-background");
    verify(launch.url, "conv-background");
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port }, "conv-background");

    const state = useAppStore.getState();
    expect(state.previewVerification?.ok).toBe(true);
    expect(state.previewLaunchProcesses[0].id).toBe(launch.id);
    expect(state.previewOwnerConversationId).toBe("conv-active");
    expect(selectPreviewForConversation(state, "conv-background").previewVerification).toBeNull();
    expect(browserFor("conv-background").status).toBe("idle");
    expect(browserFor().status).toBe("verified");
  });

  it.each([
    { conversation_id: "conv-unknown" },
    { workspace_root: "C:/other" },
  ])("ignores lifecycle events outside the accepted scope: %j", (scope) => {
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port, ...scope });

    expect(useAppStore.getState().previewVerification?.ok).toBe(true);
    expect(browserFor().status).toBe("verified");
  });

  it.each([
    { type: "preview.server.output", id: launch.id, stream: "stdout", line: "still running" },
    { type: "preview.server.ready", id: launch.id, port: launch.port, url: launch.url },
    { type: "preview.launch.config", configs: [], running: [{ ...launch, status: "ready" }] },
    { type: "preview.servers.updated", servers: [{ port: launch.port, url: launch.url }] },
  ])("preserves current verification on an unchanged lifecycle: $type", (event) => {
    send(event);

    expect(useAppStore.getState().previewVerification?.ok).toBe(true);
    expect(browserFor().status).toBe("verified");
  });

  it.each([
    { ...launch, pid: 1002, status: "ready" },
    { ...launch, status: "crashed" },
  ])("invalidates a changed process in a reconnect snapshot: %j", (process) => {
    send({ type: "preview.launch.config", configs: [], running: [process] });

    expect(useAppStore.getState().previewVerification).toBeNull();
    expect(browserFor().status).toBe(process.status === "crashed" ? "failed" : "running");
  });

  it("uses the known server when the stopped process was not in the local snapshot", () => {
    useAppStore.getState().setPreviewLaunchProcesses([], "conv-active");
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port });

    expect(useAppStore.getState().previewVerification).toBeNull();
    expect(browserFor().status).toBe("idle");
  });

  it("accepts a fresh verification after restarting", () => {
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port });
    send({ type: "preview.launch.started", ...launch, pid: 1002 });
    verify();

    expect(browserFor().status).toBe("verified");
  });

  it.each(["preview.navigate", "preview.verify"])("keeps the replacement intact when an old %s result expires", (command) => {
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port });
    send({ type: "preview.launch.started", ...launch, pid: 1002 });
    send({ type: "preview.server.ready", id: launch.id, port: launch.port, url: launch.url });
    const expired = {
      type: "command.result", command, level: "error",
      message: "Preview process stopped or restarted before verification completed.",
      conversation_id: "conv-active", workspace_root: "C:/active", request_id: "old-verification",
    } as ServerEvent;

    expect(handleCommandResultEvent(expired)).toBe(true);
    expect(useAppStore.getState().previewVerification).toBeNull();
    expect(useAppStore.getState().previewLaunchProcesses[0]).toMatchObject({ pid: 1002, status: "ready" });
    expect(browserFor().status).toBe("running");

    verify();
    handleCommandResultEvent(expired);
    expect(browserFor().status).toBe("verified");
  });

  it("retains unfinished cleanup evidence and explains it in the sidebar", () => {
    send({
      type: "preview.server.unhealthy", id: launch.id,
      cleanup_pending: true, cleanup_reason: "preview_cleanup_unproven",
      last_error: "Preview resources could not be confirmed stopped; retry stop",
    });

    const preview = selectPreviewForConversation(useAppStore.getState(), "conv-active");
    expect(preview.previewLaunchProcesses[0]).toMatchObject({
      status: "unhealthy", cleanup_pending: true, cleanup_reason: "preview_cleanup_unproven",
    });
    expect(preview.previewVerification).toBeNull();
    expect(browserFor()).toMatchObject({ status: "failed", detail: "清理未完成，请重试停止" });
    const activity = buildActivitySidebarState({
      ...preview, conversationId: "conv-active", messages: [], todos: [], plan: null, agentProgress: [],
    });
    expect(activity.runs[0]).toMatchObject({
      previewId: launch.id, status: "failed", detail: "清理未完成，请重试停止", attention: true,
    });
    send({ type: "preview.launch.stopped", id: launch.id, port: launch.port, cleanup_pending: false });
    expect(useAppStore.getState().previewLaunchProcesses).toEqual([]);
    expect(browserFor().status).toBe("idle");
  });

  it("projects a reconnect snapshot that is still stopping without showing the old success", () => {
    send({
      type: "preview.launch.config", configs: [],
      running: [{ ...launch, status: "stopping", cleanup_pending: true, cleanup_reason: "preview_cleanup_pending" }],
    });

    expect(browserFor()).toMatchObject({ status: "running", detail: "正在停止" });
    expect(useAppStore.getState().previewVerification).toBeNull();
    expect(selectPreviewForConversation(useAppStore.getState(), "conv-active").previewLaunchProcesses[0])
      .toMatchObject({ status: "stopping", cleanup_pending: true });
  });
});
