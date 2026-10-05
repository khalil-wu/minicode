/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ClientCommand, CommandResultEvent, PreviewLaunchProcessInfo, ServerEvent } from "../protocol/events";
import { handlePreviewEvent } from "../chat/previewEvents";
import { registerWebSocketSender, resetPendingCommandResultsForTests, resolveClientCommandResult } from "../protocol/ws-outbox";
import { openPreviewServiceManager } from "../lib/preview-server-actions";
import { PreviewServerManager } from "./PreviewServerManager";
import { BrowserPanel } from "./BrowserPanel";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../desktop/runtime", async (load) => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => false }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const scope = { conversation_id: "A", workspace_root: "C:/project" };
const config = { name: "web", command: "node server.js", cwd: "C:/project", port: 4173, url: "http://127.0.0.1:4173", source: ".minicode/launch.json" };
let running: PreviewLaunchProcessInfo[];
const commands: ClientCommand[] = [];
beforeEach(() => {
  commands.length = 0;
  running = [];
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/project", isConnected: true, conversations: [], sideChats: {}, conversationWorkbenchStates: {},
    previewLaunchConfigs: [], previewLaunchProcesses: [], previewServers: [], previewVerification: null, previewServiceManagerRequest: null, editorTabs: [], messages: [], panelSlots: [{ id: "chat", kind: "chat", label: "Chat" }] });
  registerWebSocketSender((command) => {
    commands.push(command);
    queueMicrotask(() => {
      if (command.type === "preview.launch.config") handlePreviewEvent({ type: "preview.launch.config", ...scope, configs: [config], running } as ServerEvent);
      if (command.type === "preview.verify") handlePreviewEvent({ type: "preview.verified", ...scope, url: command.url, ok: false, status_code: 503, error: "HTTP 503", elapsed_ms: 5 } as ServerEvent);
      resolveClientCommandResult({ type: "command.result", command: command.type, level: "success", message: "", data: { client_command_id: command.client_command_id } } as CommandResultEvent);
    });
    return true;
  });
});
afterEach(() => { cleanup(); resetPendingCommandResultsForTests(); registerWebSocketSender(null); vi.restoreAllMocks(); });

describe("preview services menu", () => {
  it("consumes a first-open request after the browser module mounts in Web mode", async () => {
    act(() => openPreviewServiceManager());
    render(<BrowserPanel />);
    expect(screen.getByText("内置浏览器仅在桌面版可用")).toBeTruthy();
    expect(await screen.findByRole("dialog", { name: "预览服务管理" })).toBeTruthy();
    await waitFor(() => expect(commands[0]).toMatchObject({ type: "preview.launch.config", ...scope }));
    await waitFor(() => expect(screen.getByRole("button", { name: "启动", exact: true })).toHaveProperty("disabled", false));
    expect(useAppStore.getState().previewServiceManagerRequest).toBeNull();
  });
  it("shows actual stdout/stderr while keeping process startup separate from HTTP readiness", async () => {
    running = [{ ...config, id: "owned", pid: 123, status: "starting", output_tail: [{ stream: "stdout", line: "Listening soon" }, { stream: "stderr", line: "Waiting for database" }] }];
    render(<PreviewServerManager />);
    fireEvent.click(screen.getByRole("button", { name: "管理预览服务" }));
    expect(await screen.findByText("进程已启动，等待服务")).toBeTruthy();
    expect(screen.getByText("HTTP 尚未检测")).toBeTruthy();
    expect(screen.getByText(/\[stdout\] Listening soon/)).toBeTruthy();
    expect(screen.getByText(/\[stderr\] Waiting for database/)).toBeTruthy();
    expect(screen.getByRole("button", { name: config.url })).toHaveProperty("disabled", true);
    const verify = screen.getByRole("button", { name: "检测 HTTP" });
    await waitFor(() => expect(verify).toHaveProperty("disabled", false));
    fireEvent.click(verify);
    expect(await screen.findByText(/HTTP 尚未就绪 · 503/)).toBeTruthy();
    expect(commands.find((command) => command.type === "preview.verify")).toMatchObject({ ...scope, url: config.url });
  });
  it("opens the service's returned ready URL rather than its configured port guess", async () => {
    const actualUrl = "http://127.0.0.1:4174/app";
    running = [{ ...config, id: "owned", pid: 123, status: "ready", port: 4174, url: actualUrl }];
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<PreviewServerManager />);
    fireEvent.click(screen.getByRole("button", { name: "管理预览服务" }));
    const target = await screen.findByRole("button", { name: actualUrl });
    fireEvent.click(target);
    expect(open).toHaveBeenCalledExactlyOnceWith(actualUrl, "_blank", "noopener,noreferrer");
  });
});
