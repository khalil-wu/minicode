/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";
import { registerWebSocketSender, resetPendingCommandResultsForTests, resolveClientCommandResult } from "../protocol/ws-outbox";
import { compareWriteWorkspaceFile, readWorkspaceFile } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { openPreviewLaunchConfiguration, openPreviewServiceManager, restartPreviewService } from "./preview-server-actions";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/api", async (load) => ({ ...await load<typeof import("../protocol/api")>(), apiBase: () => "http://test.local", authHeaders: () => ({}), fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const scope = { conversation_id: "A", workspace_root: "C:/project" };
const commands: ClientCommand[] = [];
const answer = (command: ClientCommand, level = "success", message = "") => resolveClientCommandResult({ type: "command.result", command: command.type, level, message, data: { client_command_id: command.client_command_id, ...scope } } as CommandResultEvent);
beforeEach(() => {
  commands.length = 0;
  registerWebSocketSender((command) => { commands.push(command); return true; });
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/project", editorTabs: [], activeTabPath: null, editorOpenRequests: [], previewServiceManagerRequest: null, rightStackTab: "tasks" });
});
afterEach(() => { resetPendingCommandResultsForTests(); registerWebSocketSender(null); vi.unstubAllGlobals(); });

describe("preview service actions", () => {
  it("does not start a replacement until a semantic stop result confirms completion", async () => {
    const operation = restartPreviewService(scope, "web");
    expect(commands.map((command) => command.type)).toEqual(["preview.launch.stop"]);
    const ack = { type: "client.command.ack", client_command_id: commands[0].client_command_id, command_type: "preview.launch.stop" };
    expect(resolveClientCommandResult(ack as unknown as CommandResultEvent & { client_command_id?: string })).toBe(false);
    await Promise.resolve();
    expect(commands).toHaveLength(1);
    answer(commands[0]);
    await vi.waitFor(() => expect(commands).toHaveLength(2));
    expect(commands[1]).toMatchObject({ type: "preview.launch.start", ...scope, name: "web" });
    answer(commands[1]);
    await operation;
  });
  it("retains the stop error and refuses a replacement when cleanup is unfinished", async () => {
    const operation = restartPreviewService(scope, "web");
    answer(commands[0], "error", "Old process is still alive");
    await expect(operation).rejects.toThrow("Old process is still alive");
    expect(commands).toHaveLength(1);
  });
  it("does not continue a restart into a different conversation after its original stop finishes", async () => {
    const operation = restartPreviewService(scope, "web");
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/other" });
    answer(commands[0]);
    expect(await operation).toBeNull();
    expect(commands).toHaveLength(1);
  });
  it("stores the owning open request before BrowserPanel is mounted", () => {
    openPreviewServiceManager();
    expect(useAppStore.getState().previewServiceManagerRequest).toEqual({ conversationId: "A", workspaceRoot: "C:/project" });
    expect(useAppStore.getState().rightStackTab).toBe("browser");
  });
  it("opens a valid unsaved launch draft and writes only after the user explicitly saves it", async () => {
    let diskContent: string | null = null;
    const writes: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      expect(url.searchParams.get("workspace_root")).toBe("C:/project");
      if (url.pathname.endsWith("/file/compare-write")) {
        const body = JSON.parse(String(init?.body));
        writes.push(body); diskContent = body.content;
        return new Response(JSON.stringify({ path: ".minicode/launch.json", content: diskContent, content_hash: "saved" }));
      }
      return diskContent === null ? new Response(JSON.stringify({ detail: "Not found" }), { status: 404 })
        : new Response(JSON.stringify({ path: ".minicode/launch.json", content: diskContent, content_hash: "saved" }));
    }));
    expect(await openPreviewLaunchConfiguration(scope, [])).toBe(true);
    const initial = useAppStore.getState().editorTabs[0];
    expect(initial.path).toBe(".minicode/launch.json");
    expect(initial.original).toBe("");
    expect(initial.contentHash).toBe("");
    expect(JSON.parse(initial.content)).toEqual({ configurations: [] });
    expect(initial.content).not.toBe(initial.original);
    expect(writes).toHaveLength(0);
    const configured = JSON.stringify({ configurations: [{ name: "web", command: "node server.js", cwd: ".", port: 4173 }] }, null, 2);
    useAppStore.getState().updateTabContent(initial.path, configured);
    const draft = useAppStore.getState().editorTabs[0];
    const saved = await compareWriteWorkspaceFile(draft.path, draft.contentHash || "", draft.content, scope.workspace_root);
    expect(saved.ok).toBe(true);
    expect(writes[0]).toMatchObject({ expected_hash: "", content: configured });
    const file = await readWorkspaceFile(".minicode/launch.json", scope.workspace_root);
    expect(JSON.parse(file.content).configurations[0]).toMatchObject({ name: "web", command: "node server.js" });
  });
});
