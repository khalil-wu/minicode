/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../protocol/events";
import { useAppStore } from "../stores";
import { handlePreviewEvent } from "./previewEvents";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

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
