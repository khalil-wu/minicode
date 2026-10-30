import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { createServer, type Server } from "node:http";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Socket } from "node:net";

type JsonRecord = Record<string, unknown>;
type ExternalPeer = { env: Record<string, string>; fingerprint: string };
type WireRecord = { connection: number; direction: "client" | "server"; atMs: number; message: JsonRecord };

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const desktopEntry = path.join(repoRoot, "desktop");
const desktopMain = path.join(desktopEntry, "main.js");
const desktopRequire = createRequire(path.join(desktopEntry, "package.json"));
const electronExecutable = desktopRequire("electron") as string;
const { externalPeerEnvironment } = desktopRequire("./test-peer-env.js") as {
  externalPeerEnvironment: () => Promise<ExternalPeer>;
};
const frontendPort = Number(process.env.MINICODE_E2E_PORT ?? "43173");
const desktopViewport = { width: 1800, height: 920 };
const ownerId = "conv-electron";
const runningResult = "Desktop restart verification completed.";
const lazyResult = "Recovered result body from lazy status lookup.";
const recoveredResult = "Persisted result restored from the durable child journal.";

const processBlock = (id: string, content: string, timestamp: number) => ({
  type: "process", id, item_kind: "process_text", content, source: "model_preamble",
  status: "completed", visibility: "timeline", timestamp,
});
const finalBlock = (id: string, content: string) => ({
  type: "text", item_id: id, content, source: "model_final", status: "completed", is_streaming: false,
});
const completedChildMessages = (id: string, process: string, result: string, timestamp: number) => [{
  id, turn_id: id, role: "assistant", content: result, timestamp, is_streaming: false,
  completed_at: timestamp + 5, terminal_status: "completed",
  blocks: [processBlock(`${id}-process`, process, timestamp), finalBlock(`${id}-answer`, result)],
}];

class TestWebSocket {
  private buffer = Buffer.alloc(0);

  constructor(
    private readonly socket: Socket,
    private readonly onMessage: (message: JsonRecord) => void,
    private readonly onSend: (message: JsonRecord) => void,
  ) {
    socket.on("data", (chunk) => this.consume(chunk));
  }

  send(message: JsonRecord) {
    this.onSend(message);
    const payload = Buffer.from(JSON.stringify(message));
    const length = payload.length;
    const header = length < 126
      ? Buffer.from([0x81, length])
      : length < 65_536
        ? Buffer.from([0x81, 126, length >> 8, length & 0xff])
        : (() => {
            const frame = Buffer.alloc(10);
            frame[0] = 0x81;
            frame[1] = 127;
            frame.writeBigUInt64BE(BigInt(length), 2);
            return frame;
          })();
    this.socket.write(Buffer.concat([header, payload]));
  }

  private consume(chunk: Buffer) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 2) {
      const opcode = this.buffer[0]! & 0x0f;
      const second = this.buffer[1]!;
      const masked = Boolean(second & 0x80);
      let payloadLength = second & 0x7f;
      let offset = 2;
      if (payloadLength === 126) {
        if (this.buffer.length < 4) return;
        payloadLength = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (payloadLength === 127) {
        if (this.buffer.length < 10) return;
        payloadLength = Number(this.buffer.readBigUInt64BE(2));
        offset = 10;
      }
      const maskLength = masked ? 4 : 0;
      if (this.buffer.length < offset + maskLength + payloadLength) return;
      const mask = masked ? this.buffer.subarray(offset, offset + 4) : null;
      offset += maskLength;
      const payload = Buffer.from(this.buffer.subarray(offset, offset + payloadLength));
      this.buffer = this.buffer.subarray(offset + payloadLength);
      if (mask) {
        for (let index = 0; index < payload.length; index += 1) payload[index] ^= mask[index % 4]!;
      }
      if (opcode === 0x08) {
        this.socket.end(Buffer.from([0x88, 0]));
        return;
      }
      expect(opcode, "fixture transport receives complete JSON text frames").toBe(0x01);
      this.onMessage(JSON.parse(payload.toString("utf8")) as JsonRecord);
    }
  }
}

class MultiAgentBackend {
  readonly commands: JsonRecord[] = [];
  readonly wire: WireRecord[] = [];
  readonly transportErrors: JsonRecord[] = [];
  readonly closingConnections = new Set<number>();
  readonly server: Server;
  readonly sockets = new Set<Socket>();
  readonly connections: TestWebSocket[] = [];
  port = 0;
  private runningSeq = 2;
  private runningProgressAdded = false;
  private runningFinal = false;
  private runningCompleted = false;
  private lazyResultLoaded = false;

  constructor(readonly peer: ExternalPeer) {
    this.server = createServer((req, res) => {
      res.setHeader("access-control-allow-origin", "*");
      res.setHeader("access-control-allow-headers", "X-MiniCode-Token, Content-Type");
      res.setHeader("access-control-allow-methods", "GET, POST, PUT, DELETE, OPTIONS");
      res.setHeader("content-type", "application/json");
      if (req.method === "OPTIONS") {
        res.writeHead(204);
        res.end();
        return;
      }
      if (req.url === "/readyz") {
        res.end(JSON.stringify({ status: "ok", ready: true, desktop_bridge_fingerprint: peer.fingerprint }));
        return;
      }
      if (req.url === "/health") {
        res.end(JSON.stringify({ status: "ok", ready: true }));
        return;
      }
      const url = new URL(req.url!, "http://fixture");
      if (url.pathname === "/api/thread-metadata/sections") {
        res.end(JSON.stringify({ sections: [] }));
        return;
      }
      if (url.pathname === "/api/llm/draft-selection") {
        res.end(JSON.stringify({ provider: "custom", model: "gpt-5", current_model: "gpt-5", available_models: ["gpt-5"],
          models_source: "configured", config_version: "fixture", config_cwd: url.searchParams.get("config_cwd") ?? "",
          effective_reasoning_effort: "medium", available_permission_modes: ["confirm", "auto", "bypass"],
          feature_flags: {}, composer_commands: [], default_model_selection: { provider: "custom", model: "gpt-5",
            reasoning_effort: "medium", conversation_id: "", working_directory: url.searchParams.get("workspace_root") ?? "" } }));
        return;
      }
      res.end(JSON.stringify({ ok: true, providers: [], models: [] }));
    });
    this.server.on("upgrade", (request, socket) => {
      this.sockets.add(socket);
      socket.once("close", () => this.sockets.delete(socket));
      const accept = createHash("sha1")
        .update(`${request.headers["sec-websocket-key"]}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest("base64");
      socket.write([
        "HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
        `Sec-WebSocket-Accept: ${accept}`, "Sec-WebSocket-Protocol: minicode", "", "",
      ].join("\r\n"));

      const connection = this.connections.length + 1;
      socket.on("error", (error: NodeJS.ErrnoException) => {
        this.transportErrors.push({ connection, code: error.code, message: error.message,
          duringNativeClose: this.closingConnections.has(connection) });
      });
      const ws = new TestWebSocket(socket, (command) => {
        this.commands.push(command);
        this.wire.push({ connection, direction: "client", atMs: Date.now(), message: command });
        if (typeof command.client_command_id === "string") {
          ws.send({ type: "client.command.ack", client_command_id: command.client_command_id, command_type: command.type });
        }
        if (command.type === "session.restore" || command.type === "session.sync") {
          this.sendSessionSnapshot(ws, connection > 1, command);
        } else if (command.type === "conversation.switch") {
          expect(command.conversation_id).toBe(ownerId);
          this.sendSessionSnapshot(ws, connection > 1, command);
        } else if (command.type === "conversation.list") {
          this.sendConversationList(ws);
        } else if (command.type === "subagent.transcript") {
          const data = this.childTranscript(String(command.subagent_id));
          ws.send({ type: "command.result", command: "subagent.transcript", level: "success", message: "",
            data: { client_command_id: command.client_command_id, subagent_id: command.subagent_id, ...data } });
        } else if (command.type === "subagent.status" && command.subagent_id === "subagent-complete") {
          this.lazyResultLoaded = true;
          const snapshot = { status: "completed", result: { status: "completed", content: lazyResult,
            duration_ms: 2400, tool_call_count: 4 } };
          ws.send({ type: "subagent.done", conversation_id: ownerId, subagent_id: "subagent-complete",
            summary: "Review completed", snapshot, transcript_snapshot: this.childTranscript("subagent-complete") });
          ws.send({ type: "command.result", command: "subagent.status", level: "success", message: "",
            data: { client_command_id: command.client_command_id, subagent_id: command.subagent_id, snapshot } });
        } else if (command.type === "ping") {
          ws.send({ type: "pong" });
        }
      }, (message) => this.wire.push({ connection, direction: "server", atMs: Date.now(), message }));
      this.connections.push(ws);
    });
  }

  async listen() {
    await new Promise<void>((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", () => {
        this.server.off("error", reject);
        this.port = (this.server.address() as { port: number }).port;
        resolve();
      });
    });
  }

  async close() {
    for (const socket of this.sockets) socket.destroy();
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }

  publishLiveChildren() {
    const ws = this.connections[0]!;
    ws.send({ type: "subagent.event", conversation_id: ownerId, subagent_id: "workflow-electron", event: {
      type: "workflow_started", workflow_id: "workflow-electron", name: "Desktop recovery", mode: "pipeline",
      steps: [
        { step_id: "review", node_id: "review", task_id: "subagent-complete", title: "Review persisted state",
          role: "reviewer", objective: "Review persisted state", ready: true },
        { step_id: "verify", node_id: "verify", task_id: "subagent-running", title: "Verify restart recovery",
          role: "verification", objective: "Verify restart recovery", depends_on_nodes: ["review"], blocked_by: [], ready: true },
      ],
    } });
    ws.send({ type: "subagent.start", conversation_id: ownerId, subagent_id: "subagent-running", parent_id: "root",
      role: "verification", prompt: "Verify restart recovery", objective: "Verify restart recovery",
      workflow_id: "workflow-electron", workflow_name: "Desktop recovery", workflow_mode: "pipeline",
      node_id: "verify", task_id: "subagent-running", agent_path: "/root/verify" });
    ws.send({ type: "subagent.progress", conversation_id: ownerId, subagent_id: "subagent-running",
      iteration: 2, max_iterations: 5, source_event_type: "agent.progress", detail: "Checking the Electron restart path",
      transcript_snapshot: this.childTranscript("subagent-running") });
    ws.send({ type: "subagent.start", conversation_id: ownerId, subagent_id: "subagent-complete", parent_id: "root",
      role: "reviewer", prompt: "Review persisted state", objective: "Review persisted state",
      workflow_id: "workflow-electron", workflow_name: "Desktop recovery", workflow_mode: "pipeline",
      node_id: "review", task_id: "subagent-complete" });
    ws.send({ type: "subagent.done", conversation_id: ownerId, subagent_id: "subagent-complete", summary: "Review completed" });
  }

  publishRunningProgress() {
    this.runningProgressAdded = true;
    this.runningSeq = 3;
    this.connections[0]!.send({ type: "subagent.progress", conversation_id: ownerId, subagent_id: "subagent-running",
      transcript_delta: { kind: "events", base_seq: 2, seq: 3, events: [{
        event_id: "native-child-reading", event_type: "system", seq: 3, ts_ms: 12, payload: {
          transcript_only: true, kind: "process_text", item_id: "child-running-added", content: "Verified the restored child journal",
          source: "model_preamble", status: "completed", visibility: "timeline",
        },
      }] } });
  }

  publishRunningFinal() {
    this.runningFinal = true;
    this.runningSeq = 4;
    this.connections[0]!.send({ type: "subagent.progress", conversation_id: ownerId, subagent_id: "subagent-running",
      transcript_delta: { kind: "events", base_seq: 3, seq: 4, events: [{
        event_id: "native-child-final", event_type: "progress", seq: 4, ts_ms: 13, payload: {
          kind: "assistant_message", item_id: "running-answer", content: runningResult, source: "model_final", status: "completed",
        },
      }] } });
  }

  publishRunningDone() {
    this.runningCompleted = true;
    this.runningSeq = 5;
    this.connections[0]!.send({ type: "subagent.done", conversation_id: ownerId, subagent_id: "subagent-running",
      result: { status: "completed", content: runningResult }, transcript_snapshot: this.childTranscript("subagent-running") });
  }

  private childTranscript(id: string): JsonRecord {
    if (id === "subagent-running") {
      return { seq: this.runningSeq, agent_path: "/root/verify", messages: [
        { id: "child-running-user", role: "user", content: "Verify restart recovery", timestamp: 10 },
        { id: "subagent-turn-first", turn_id: "first", role: "assistant", content: this.runningFinal ? runningResult : "",
          timestamp: 11, is_streaming: !this.runningCompleted,
          ...(this.runningCompleted ? { terminal_status: "completed", completed_at: 15 } : {}),
          blocks: [processBlock("child-running-process", "Checking the Electron restart path", 11),
            ...(this.runningProgressAdded ? [processBlock("child-running-added", "Verified the restored child journal", 12)] : []),
            ...(this.runningFinal ? [finalBlock("running-answer", runningResult)] : [])] },
      ] };
    }
    if (id === "subagent-complete") return { seq: this.lazyResultLoaded ? 1 : 0,
      messages: this.lazyResultLoaded
        ? completedChildMessages("child-complete-result", "Reviewed the durable desktop state", lazyResult, 20) : [] };
    if (id === "subagent-recovered") return { seq: 1,
      messages: completedChildMessages("child-recovered-result", "Loaded the persisted child journal", recoveredResult, 30) };
    expect(id).toBe("subagent-historical");
    return { seq: 1, model: "gpt-6.1-sol", provider: "custom", reasoning_effort: "high", agent_path: "/root/historical_review",
      messages: completedChildMessages("child-historical-result", "Read the historical child journal",
        "Historical child journal loaded without an active worker.", 25) };
  }

  private activeConversation(recovered = false) {
    return { id: ownerId, title: "Electron agents", updated_at: "2026-10-15T00:00:00.000Z",
      ...(recovered ? { context_snapshot: { ui_agent_state: { plan: null, todos: [], agentProgress: [], subagents: [{
        id: "subagent-recovered", role: "verification", status: "done", objective: "Recover completed desktop verification",
        summary: "Recovered after desktop restart", result_content: recoveredResult, result_available: true, completed_at: 35,
      }] } } } : {}),
      messages: [
        { id: "message-electron", role: "user", content: "Verify the desktop multi-agent recovery path.", artifacts: [], timestamp: 1 },
        { id: "assistant-current", role: "assistant", artifacts: [], timestamp: 2, is_streaming: !recovered,
          content: recovered ? "The historical desktop review is complete." : "Checking the current desktop state...",
          ...(recovered ? { completed_at: 4, terminal_status: "completed", blocks: [
            { type: "progress", id: "historical-completion", stage: "status", phase: "subagent", status: "completed",
              message: "Historical desktop review已完成", subagent_id: "subagent-historical", subagent_name: "Historical desktop review",
              subagent_identity: "/root/historical_review", subagent_status: "done", timestamp: 3, visibility: "timeline" },
            finalBlock("historical-parent-final", "The historical desktop review is complete."),
          ] } : {}) },
      ],
    };
  }

  private sendConversationList(ws: TestWebSocket) {
    ws.send({ type: "conversation.list", conversations: [{ id: ownerId, title: "Electron agents", updated_at: "2026-10-15T00:00:00.000Z" }],
      active_conversation_id: ownerId });
  }

  private sendSessionSnapshot(ws: TestWebSocket, recovered: boolean, command: JsonRecord) {
    const restore = command.type === "session.restore";
    const conversation = this.activeConversation(recovered);
    const session = { session_id: "session-electron", active_conversation_id: ownerId, active_conversation: conversation,
      active_stream_conversation_ids: recovered ? [] : [ownerId], active_subagents: [],
      pending_turn_inputs: recovered ? [] : [{ conversation_id: ownerId, mode: "steer", message_id: "assistant-steer",
        user_message_id: "user-steer", target_message_id: "assistant-current",
        content: "Continue with the recovered verification path.", queued_at_ms: 3 }],
    };
    ws.send({ type: "llm.model.updated", model: "gpt-5", current_model: "gpt-5", available_models: ["gpt-5"] });
    if (command.type !== "conversation.switch") ws.send({ type: restore ? "session.restored" : "session.synced",
      ...(restore ? { restored: true } : { synced: true }), protocol_version: "1.0.0",
      client_command_id: command.client_command_id, active_conversation_id: ownerId,
      active_conversation: conversation, conversation, conversation_switched_follows: true, session,
      last_seq: Number(command.last_seq ?? 0), current_seq: recovered ? 2 : 0, replayed_events: 0 });
    ws.send({ type: "conversation.switched", conversation_id: ownerId, conversation, session,
      is_hydrating: false, context_pending: false,
      ...(command.type === "conversation.switch" ? { client_command_id: command.client_command_id,
        client_command_type: "conversation.switch" } : {}) });
    this.sendConversationList(ws);
  }
}

async function writeHiddenDesktopEntry(userDataDir: string) {
  const entry = path.join(userDataDir, "hidden-desktop-entry.cjs");
  await writeFile(entry, `
const Module = require('node:module');
const desktopRequire = Module.createRequire(${JSON.stringify(path.join(desktopEntry, "package.json"))});
const electron = desktopRequire('electron');
const originalLoad = Module._load;
const visibilityCalls = [];
const desktopViewport = ${JSON.stringify(desktopViewport)};
const HiddenBrowserWindow = new Proxy(electron.BrowserWindow, {
  construct(Target, [options]) {
    // Exercise the wide summary route independently of the CI display bounds.
    const window = new Target({ ...options, ...desktopViewport, show: false,
      webPreferences: { ...options.webPreferences, backgroundThrottling: false } });
    for (const method of ['show', 'showInactive', 'focus', 'restore']) window[method] = () => visibilityCalls.push(method);
    window.flashFrame = () => {};
    return window;
  }
});
const testElectron = { ...electron, BrowserWindow: HiddenBrowserWindow };
Module._load = function(request, ...args) {
  return request === 'electron' ? testElectron : originalLoad.call(this, request, ...args);
};
globalThis.__offscreenQa = { visibilityCalls };
require(${JSON.stringify(desktopMain)});
`);
  return entry;
}

async function launchDesktop(backend: MultiAgentBackend, userDataDir: string, entry: string) {
  const { ELECTRON_RUN_AS_NODE: _electronRunAsNode, ...cleanEnv } = process.env;
  return electron.launch({ executablePath: electronExecutable,
    args: [...(process.platform === "win32" ? [] : ["--disable-gpu", "--no-sandbox"]), "--lang=zh-CN", entry], cwd: desktopEntry,
    env: { ...cleanEnv, ...backend.peer.env, MINICODE_SKIP_BACKEND: "1",
      MINICODE_FRONTEND_URL: `http://127.0.0.1:${frontendPort}/`, MINICODE_API_BASE_URL: `http://127.0.0.1:${backend.port}`,
      MINICODE_WS_BASE_URL: `ws://127.0.0.1:${backend.port}`, MINICODE_RUNTIME_TOKEN: `e2e-${randomUUID()}`,
      MINICODE_USER_DATA_DIR: userDataDir },
  });
}

async function nativeWindowState(app: ElectronApplication) {
  const native = await app.evaluate(({ BrowserWindow }) => ({
    windows: BrowserWindow.getAllWindows().map((window) => ({
      id: window.id, visible: window.isVisible(), focused: window.isFocused(),
      bounds: window.getBounds(), contentBounds: window.getContentBounds(),
    })),
    visibilityCalls: (globalThis as typeof globalThis & { __offscreenQa: { visibilityCalls: string[] } }).__offscreenQa.visibilityCalls,
  }));
  const renderers = await Promise.all(app.windows().map(async (window) => ({
    viewportSize: window.viewportSize(),
    ...await window.evaluate(() => ({
      innerWidth, innerHeight, devicePixelRatio,
      chatFrameWidths: Array.from(document.querySelectorAll<HTMLElement>('.mc-main-slot-frame[data-panel-slot-kind="chat"]'))
        .map((frame) => frame.clientWidth),
    })),
  })));
  return { ...native, renderers };
}

async function captureDesktop(app: ElectronApplication, filePath: string) {
  await app.windows()[0]!.locator(".subagents-detail").evaluate(async (detail) => {
    const transitions = detail.getAnimations({ subtree: true })
      .filter((animation) => animation.effect!.getTiming().iterations !== Infinity);
    await Promise.all(transitions.map((animation) => animation.finished));
  });
  const png = await app.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0]!.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
  await writeFile(filePath, Buffer.from(png, "base64"));
}

async function openAgentsPanel(window: Page) {
  const agentsTab = window.getByRole("tab", { name: "打开子智能体", exact: true });
  if (await agentsTab.isVisible()) {
    await agentsTab.click();
    return;
  }
  const closeRightPanel = window.getByRole("button", { name: "关闭右侧栏", exact: true });
  if (await closeRightPanel.isVisible()) await closeRightPanel.click();
  const chatFrame = window.locator('.mc-main-slot-frame[data-panel-slot-kind="chat"]');
  await expect.poll(() => chatFrame.evaluate((frame) => frame.clientWidth)).toBeGreaterThanOrEqual(900);
  const summaryToggle = window.getByRole("button", { name: "切换摘要", exact: true });
  if (await summaryToggle.getAttribute("aria-pressed") !== "true") await summaryToggle.click();
  const summary = window.getByRole("complementary", { name: "工作区上下文摘要", exact: true });
  await expect(summary).toBeVisible();
  await summary.getByRole("button", { name: "子智能体", exact: true }).click();
  await expect(agentsTab).toBeVisible();
}

async function expectReadonlyAgentDetail(window: Page, title: string) {
  const detail = window.getByRole("region", { name: `子智能体任务详情：${title}` });
  await expect(detail).toBeVisible();
  await expect(detail.getByRole("textbox")).toHaveCount(0);
  await expect(detail.getByRole("button", { name: /补充指令|刷新子智能体工作详情|停止子智能体|发送给子智能体/ })).toHaveCount(0);
  await expect(detail.locator(".subagents-detail-header-actions")).toHaveCount(0);
  return detail;
}

test("real Electron workbench supports read-only child transcripts, lazy results, and persisted history recovery", async ({}, testInfo) => {
  test.setTimeout(180_000);
  const backend = new MultiAgentBackend(await externalPeerEnvironment());
  const userDataDir = await mkdtemp(path.join(tmpdir(), "minicode-agent-e2e-"));
  const ownedApps = new Set<ElectronApplication>();
  const nativeStates: JsonRecord[] = [];
  let completed = false;
  const entry = await writeHiddenDesktopEntry(userDataDir);
  await backend.listen();

  try {
    const app = await launchDesktop(backend, userDataDir, entry);
    ownedApps.add(app);
    const window = await app.firstWindow();
    await window.setViewportSize(desktopViewport);
    await expect.poll(() => backend.commands.some((command) => command.type === "session.sync"), { timeout: 30_000 }).toBe(true);
    // Startup may already have restored this reader before the row is clicked.
    // Open a real local draft so selecting the fixture thread exercises a switch.
    await window.getByRole("button", { name: "新聊天", exact: true }).click();
    await expect(window.getByRole("heading", { name: "我们要构建什么？", exact: true })).toBeVisible();
    await window.getByRole("list", { name: "最近聊天", exact: true })
      .getByRole("button", { name: "Electron agents", exact: true }).click();
    await expect.poll(() => backend.commands.some((command) => command.type === "conversation.switch"
      && command.conversation_id === ownerId)).toBe(true);
    await expect(window.getByText("Continue with the recovered verification path.", { exact: true })).toBeVisible();
    const initialNative = await nativeWindowState(app);
    nativeStates.push({ phase: "initial", ...initialNative });
    expect(initialNative.windows).toHaveLength(1);
    expect(initialNative.windows.every((state) => !state.visible && !state.focused)).toBe(true);
    backend.publishLiveChildren();
    await openAgentsPanel(window);

    await expect(window.getByText("Desktop recovery", { exact: true })).toHaveCount(0);
    const runningTask = window.getByRole("button", { name: "打开子智能体任务：Verify restart recovery", exact: true });
    await expect(runningTask).toBeVisible();
    await expect(runningTask).toHaveAttribute("data-status", "running");
    await expect(runningTask.locator(".subagents-row-status")).toHaveCount(0);
    await expect(window.getByRole("progressbar", { name: "Agent 执行进度" })).toHaveCount(0);
    await expect(window.locator("body")).not.toContainText(/workflow-electron|subagent-running|node_id|task_id|iteration|tool_call/i);

    await runningTask.click();
    const runningDetail = await expectReadonlyAgentDetail(window, "Verify restart recovery");
    const transcript = runningDetail.locator('[aria-label="子智能体工作记录"]');
    await expect(transcript.getByText("Checking the Electron restart path", { exact: true })).toBeVisible();
    await expect(transcript.locator('[data-message-id="child-running-user"]')).toHaveCount(0);
    await expect.poll(() => backend.commands.some((command) => command.type === "subagent.transcript"
      && command.subagent_id === "subagent-running" && command.conversation_id === ownerId)).toBe(true);
    await runningDetail.getByRole("button", { name: "收起处理步骤", exact: true }).click();
    backend.publishRunningProgress();
    await expect(transcript.getByText("Checking the Electron restart path", { exact: true })).toHaveCount(0);
    await expect(transcript.getByText("Verified the restored child journal", { exact: true })).toHaveCount(0);
    await runningDetail.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expect(transcript.getByText("Checking the Electron restart path", { exact: true })).toBeVisible();
    await expect(transcript.getByText("Verified the restored child journal", { exact: true })).toBeVisible();
    await runningDetail.getByRole("button", { name: "收起处理步骤", exact: true }).click();
    backend.publishRunningFinal();
    await expect(transcript.getByText(runningResult, { exact: true })).toBeVisible();
    await expect(transcript.getByText("Checking the Electron restart path", { exact: true })).toHaveCount(0);
    backend.publishRunningDone();
    await expect(transcript.getByText(runningResult, { exact: true })).toBeVisible();
    await expect(runningDetail.getByRole("button", { name: "展开处理步骤", exact: true })).toBeVisible();
    await window.getByRole("button", { name: "返回子智能体列表" }).click();
    await window.getByRole("button", { name: "打开子智能体任务：Verify restart recovery", exact: true }).click();
    await expectReadonlyAgentDetail(window, "Verify restart recovery");
    await expect(transcript.getByText("Checking the Electron restart path", { exact: true })).toHaveCount(0);
    await expect(transcript.getByText(runningResult, { exact: true })).toBeVisible();
    await captureDesktop(app, testInfo.outputPath("child-manual-fold.png"));

    await window.getByRole("button", { name: "返回子智能体列表" }).click();
    await window.getByRole("button", { name: "打开子智能体任务：Review persisted state", exact: true }).click();
    const lazyDetail = await expectReadonlyAgentDetail(window, "Review persisted state");
    await expect.poll(() => backend.commands.some((command) => command.type === "subagent.status"
      && command.subagent_id === "subagent-complete" && command.include_result === true && command.conversation_id === ownerId)).toBe(true);
    await expect(lazyDetail.getByText(lazyResult, { exact: true })).toBeVisible();
    await expect(lazyDetail.getByText("Reviewed the durable desktop state", { exact: true })).toHaveCount(0);
    await lazyDetail.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expect(lazyDetail.getByText("Reviewed the durable desktop state", { exact: true })).toBeVisible();
    await captureDesktop(app, testInfo.outputPath("lazy-result-expanded.png"));
    backend.closingConnections.add(1);
    await app.close();
    ownedApps.delete(app);

    const restarted = await launchDesktop(backend, userDataDir, entry);
    ownedApps.add(restarted);
    const restartedWindow = await restarted.firstWindow();
    await restartedWindow.setViewportSize(desktopViewport);
    await expect.poll(() => backend.wire.some((record) => record.connection === 2 && record.direction === "client"
      && record.message.type === "session.restore" && record.message.last_conversation_id === ownerId
      && Number(record.message.last_seq ?? 0) === 0), { timeout: 30_000 }).toBe(true);
    await expect(restartedWindow.getByText("The historical desktop review is complete.", { exact: true })).toBeVisible();
    await openAgentsPanel(restartedWindow);
    const recoveredTask = restartedWindow.getByRole("button", { name: "打开子智能体任务：Recover completed desktop verification", exact: true });
    await expect(recoveredTask).toBeVisible();
    await recoveredTask.click();
    const recoveredDetail = await expectReadonlyAgentDetail(restartedWindow, "Recover completed desktop verification");
    await expect(recoveredDetail.getByText(recoveredResult, { exact: true })).toBeVisible();
    await expect.poll(() => backend.commands.some((command) => command.type === "subagent.transcript"
      && command.subagent_id === "subagent-recovered" && command.conversation_id === ownerId)).toBe(true);
    await expect(recoveredDetail.getByText("Loaded the persisted child journal", { exact: true })).toHaveCount(0);
    await recoveredDetail.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expect(recoveredDetail.getByText("Loaded the persisted child journal", { exact: true })).toBeVisible();

    await restartedWindow.getByRole("button", { name: "返回子智能体列表" }).click();
    await expect(restartedWindow.getByRole("button", { name: "打开子智能体任务：Historical desktop review", exact: true })).toHaveCount(0);
    await restartedWindow.locator('.chat-turn[data-message-id="assistant-current"]')
      .getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await restartedWindow.getByRole("button", { name: "打开子智能体：Historical desktop review", exact: true }).click();
    const historicalDetail = await expectReadonlyAgentDetail(restartedWindow, "Historical desktop review");
    await expect.poll(() => backend.commands.some((command) => command.type === "subagent.transcript"
      && command.subagent_id === "subagent-historical" && command.conversation_id === ownerId)).toBe(true);
    await expect(historicalDetail.locator('[aria-label="子智能体工作记录"]'))
      .toContainText("Historical child journal loaded without an active worker.");
    const historicalModel = historicalDetail.getByText("6.1 Sol · 高", { exact: true });
    await expect(historicalModel).toBeVisible();
    await expect(historicalModel).toHaveAttribute("title", "custom · gpt-6.1-sol · high");
    await expect(historicalDetail.getByText("Read the historical child journal", { exact: true })).toHaveCount(0);
    await historicalDetail.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expect(historicalDetail.getByText("Read the historical child journal", { exact: true })).toBeVisible();
    await captureDesktop(restarted, testInfo.outputPath("cold-history-recovered.png"));
    const recoveredNative = await nativeWindowState(restarted);
    nativeStates.push({ phase: "cold-restart", ...recoveredNative });
    expect(recoveredNative.windows).toHaveLength(1);
    expect(recoveredNative.windows.every((state) => !state.visible && !state.focused)).toBe(true);
    expect(backend.connections).toHaveLength(2);
    expect(backend.wire.some((record) => record.message.type === "session.replay")).toBe(false);
    expect(backend.commands.some((command) => ["send_message", "user_message", "subagent.cancel"].includes(String(command.type)))).toBe(false);
    backend.closingConnections.add(2);
    await restarted.close();
    ownedApps.delete(restarted);
    // Closing an owned Electron process resets its TCP socket on Windows.
    // Keep that evidence and reject every reset/error while a native session is active.
    expect(backend.transportErrors.filter((error) => error.code !== "ECONNRESET" || !error.duringNativeClose)).toEqual([]);
    completed = true;
  } finally {
    const remainingPages = await Promise.allSettled(Array.from(ownedApps).flatMap((app) => app.windows().map(async (window) => ({
      url: window.url(), body: await window.locator("body").innerText({ timeout: 1_000 }),
    }))));
    for (const app of ownedApps) await app.close();
    await backend.close();
    const evidencePath = testInfo.outputPath("protocol-evidence.json");
    await writeFile(evidencePath, JSON.stringify({ scope: "Real Electron renderer/native shell; deterministic loopback WebSocket fixture, not live agent harness execution",
      completed, nativeStates, remainingPages: remainingPages.map((result) => result.status === "fulfilled" ? result : { status: result.status, reason: String(result.reason) }),
      transportErrors: backend.transportErrors, wire: backend.wire }, null, 2));
    await testInfo.attach("protocol-evidence", { path: evidencePath, contentType: "application/json" });
    const desktopLogPath = testInfo.outputPath("desktop.log");
    await writeFile(desktopLogPath, await readFile(path.join(userDataDir, "desktop.log"), "utf8"));
    await testInfo.attach("desktop-log", { path: desktopLogPath, contentType: "text/plain" });
    expect(path.dirname(userDataDir)).toBe(path.resolve(tmpdir()));
    expect(path.basename(userDataDir)).toMatch(/^minicode-agent-e2e-/);
    await rm(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
});
