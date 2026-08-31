import { _electron as electron, expect, test } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Socket } from "node:net";

type JsonRecord = Record<string, unknown>;

class TestWebSocket {
  private buffer = Buffer.alloc(0);

  constructor(
    private readonly socket: Socket,
    private readonly onMessage: (message: JsonRecord) => void,
  ) {
    socket.on("data", (chunk) => this.consume(chunk));
  }

  send(message: JsonRecord) {
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
        for (let index = 0; index < payload.length; index += 1) {
          payload[index] ^= mask[index % 4]!;
        }
      }
      try {
        this.onMessage(JSON.parse(payload.toString("utf8")) as JsonRecord);
      } catch {
        // Ignore non-JSON control frames in the test transport.
      }
    }
  }
}

class MultiAgentBackend {
  readonly commands: JsonRecord[] = [];
  readonly server: Server;
  readonly sockets = new Set<Socket>();
  port = 0;
  connectionCount = 0;

  constructor() {
    this.server = createServer((req, res) => {
      res.setHeader("access-control-allow-origin", "*");
      res.setHeader("content-type", "application/json");
      if (req.method === "OPTIONS") {
        res.writeHead(204);
        res.end();
        return;
      }
      if (req.url === "/health") {
        res.end(JSON.stringify({ status: "ok", ready: true }));
        return;
      }
      res.end(JSON.stringify({ ok: true, providers: [], models: [] }));
    });
    this.server.on("upgrade", (request, socket) => {
      this.sockets.add(socket);
      socket.once("close", () => this.sockets.delete(socket));
      const key = String(request.headers["sec-websocket-key"] || "");
      const accept = createHash("sha1")
        .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest("base64");
      socket.write([
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        `Sec-WebSocket-Accept: ${accept}`,
        "Sec-WebSocket-Protocol: minicode",
        "",
        "",
      ].join("\r\n"));

      this.connectionCount += 1;
      const connectionNumber = this.connectionCount;
      let bootstrapped = false;
      const ws = new TestWebSocket(socket, (command) => {
        this.commands.push(command);
        if (typeof command.client_command_id === "string") {
          ws.send({
            type: "client.command.ack",
            client_command_id: command.client_command_id,
            command_type: String(command.type || ""),
          });
        }
        if (command.type === "subagent.transcript") {
          const isRunning = command.subagent_id === "subagent-running";
          const isRecovered = command.subagent_id === "subagent-recovered";
          ws.send({
            type: "command.result",
            command: "subagent.transcript",
            level: "success",
            message: "",
            data: {
              client_command_id: command.client_command_id,
              subagent_id: command.subagent_id,
              seq: isRunning ? 2 : isRecovered ? 1 : 0,
              messages: isRecovered ? [{
                id: "child-recovered-result",
                role: "assistant",
                content: "Persisted result restored through session replay.",
                timestamp: 30,
                terminal_status: "completed",
              }] : isRunning ? [
                {
                  id: "child-running-user",
                  role: "user",
                  content: "Verify restart recovery",
                  timestamp: 10,
                },
                {
                  id: "child-running-process",
                  role: "assistant",
                  content: "",
                  timestamp: 11,
                  blocks: [{
                    type: "process",
                    id: "child-running-process",
                    item_kind: "process_text",
                    content: "Checking the Electron restart path",
                    source: "model_preamble",
                    status: "completed",
                    visibility: "timeline",
                    timestamp: 11,
                  }],
                },
              ] : [],
            },
          });
        }
        if (command.type === "subagent.status" && command.subagent_id === "subagent-complete") {
          ws.send({
            type: "subagent.done",
            conversation_id: "conv-electron",
            subagent_id: "subagent-complete",
            summary: "Review completed",
            snapshot: {
              result: {
                content: "Recovered result body from lazy status lookup.",
                duration_ms: 2400,
                tool_call_count: 4,
              },
            },
            transcript_snapshot: {
              seq: 1,
              messages: [{
                id: "child-complete-result",
                role: "assistant",
                content: "Recovered result body from lazy status lookup.",
                timestamp: 20,
                blocks: [{
                  type: "text",
                  item_id: "child-complete-result",
                  content: "Recovered result body from lazy status lookup.",
                  source: "model_final",
                  status: "completed",
                }],
              }],
            },
          });
          ws.send({
            type: "command.result",
            command: "subagent.status",
            level: "success",
            message: "",
            data: { client_command_id: command.client_command_id },
          });
        }
        if (command.type === "subagent.cancel" && command.subagent_id === "subagent-running") {
          ws.send({
            type: "subagent.done",
            conversation_id: "conv-electron",
            subagent_id: "subagent-running",
            summary: "Agent cancelled by user.",
            result: {
              status: "cancelled",
              content: "Partial verification retained after cancellation.",
              duration_ms: 3100,
              tool_call_count: 3,
            },
            transcript_snapshot: {
              seq: 3,
              messages: [
                {
                  id: "child-running-user",
                  role: "user",
                  content: "Verify restart recovery",
                  timestamp: 10,
                },
                {
                  id: "child-running-process",
                  role: "assistant",
                  content: "",
                  timestamp: 11,
                  blocks: [{
                    type: "process",
                    id: "child-running-process",
                    item_kind: "process_text",
                    content: "Checking the Electron restart path",
                    source: "model_preamble",
                    status: "completed",
                    visibility: "timeline",
                    timestamp: 11,
                  }],
                },
                {
                  id: "child-running-result",
                  role: "assistant",
                  content: "Partial verification retained after cancellation.",
                  timestamp: 12,
                  terminal_status: "cancelled",
                  blocks: [{
                    type: "text",
                    item_id: "child-running-result",
                    content: "Partial verification retained after cancellation.",
                    source: "model_final",
                    status: "completed",
                  }],
                },
              ],
            },
          });
          ws.send({
            type: "command.result",
            command: "subagent.cancel",
            level: "success",
            message: "",
            data: { client_command_id: command.client_command_id },
          });
        }
        if (command.type === "send_message" && command.recipient === "subagent-running") {
          ws.send({
            type: "subagent.event",
            conversation_id: "conv-electron",
            subagent_id: "subagent-running",
            event: {
              type: "message",
              message: {
                message_id: command.message_id,
                sender_id: "user",
                recipient_id: "subagent-running",
                content: command.message,
                sender_mailbox_epoch: 0,
                recipient_mailbox_epoch: 1,
                created_at: Date.now(),
                seq: 12,
              },
            },
          });
        }
        if (command.type === "conversation.list" && !bootstrapped) {
          bootstrapped = true;
          if (connectionNumber === 1) {
            this.sendBootstrap(ws, connectionNumber);
          } else {
            this.sendConversationList(ws);
          }
        }
        if (command.type === "session.restore") {
          this.sendRecoveryRestore(ws);
        }
      });
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

  private sendBootstrap(ws: TestWebSocket, connectionNumber: number) {
    this.sendConversationList(ws, true);
    ws.send({
      type: "llm.model.updated",
      model: "gpt-5",
      current_model: "gpt-5",
      available_models: ["gpt-5"],
    });
    ws.send({
      type: "conversation.switched",
      conversation_id: "conv-electron",
      conversation: this.activeConversation(),
    });
    ws.send({
      type: "session.synced",
      active_conversation_id: "conv-electron",
      session: {
        session_id: "session-electron",
        active_conversation_id: "conv-electron",
        active_stream_conversation_ids: ["conv-electron"],
        pending_turn_inputs: [{
          conversation_id: "conv-electron",
          mode: "steer",
          message_id: "assistant-steer",
          user_message_id: "user-steer",
          target_message_id: "assistant-current",
          content: "Continue with the recovered verification path.",
          queued_at_ms: 3,
        }],
      },
    });

    setTimeout(() => {
      if (connectionNumber > 1) {
        return;
      }

      ws.send({
        type: "subagent.event",
        conversation_id: "conv-electron",
        subagent_id: "workflow-electron",
        event: {
          type: "workflow_started",
          workflow_id: "workflow-electron",
          name: "Desktop recovery",
          mode: "pipeline",
          steps: [
            {
              step_id: "review",
              node_id: "review",
              task_id: "subagent-complete",
              title: "Review persisted state",
              role: "reviewer",
              objective: "Review persisted state",
              ready: true,
            },
            {
              step_id: "verify",
              node_id: "verify",
              task_id: "subagent-running",
              title: "Verify restart recovery",
              role: "verification",
              objective: "Verify restart recovery",
              depends_on_nodes: ["review"],
              blocked_by: [],
              ready: true,
            },
          ],
        },
      });
      ws.send({
        type: "subagent.start",
        conversation_id: "conv-electron",
        subagent_id: "subagent-running",
        parent_id: "root",
        role: "verification",
        prompt: "Verify restart recovery",
        objective: "Verify restart recovery",
        workflow_id: "workflow-electron",
        workflow_name: "Desktop recovery",
        workflow_mode: "pipeline",
        node_id: "verify",
        task_id: "subagent-running",
      });
      ws.send({
        type: "subagent.progress",
        conversation_id: "conv-electron",
        subagent_id: "subagent-running",
        iteration: 2,
        max_iterations: 5,
        source_event_type: "agent.progress",
        detail: "Checking the Electron restart path",
        transcript_snapshot: {
          seq: 2,
          messages: [
            {
              id: "child-running-user",
              role: "user",
              content: "Verify restart recovery",
              timestamp: 10,
            },
            {
              id: "child-running-process",
              role: "assistant",
              content: "",
              timestamp: 11,
              blocks: [{
                type: "process",
                id: "child-running-process",
                item_kind: "process_text",
                content: "Checking the Electron restart path",
                source: "model_preamble",
                status: "completed",
                visibility: "timeline",
                timestamp: 11,
              }],
            },
          ],
        },
      });
      ws.send({
        type: "subagent.start",
        conversation_id: "conv-electron",
        subagent_id: "subagent-complete",
        parent_id: "root",
        role: "reviewer",
        prompt: "Review persisted state",
        objective: "Review persisted state",
        workflow_id: "workflow-electron",
        workflow_name: "Desktop recovery",
        workflow_mode: "pipeline",
        node_id: "review",
        task_id: "subagent-complete",
      });
      ws.send({
        type: "subagent.done",
        conversation_id: "conv-electron",
        subagent_id: "subagent-complete",
        summary: "Review completed",
      });
    }, 500);
  }

  private activeConversation() {
    return {
      id: "conv-electron",
      title: "Electron agents",
      updated_at: "2026-07-12T00:00:00.000Z",
      messages: [
        {
          id: "message-electron",
          role: "user",
          content: "Verify the desktop multi-agent recovery path.",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "assistant-current",
          role: "assistant",
          content: "Checking the current desktop state...",
          artifacts: [],
          timestamp: 2,
        },
      ],
    };
  }

  private sendConversationList(ws: TestWebSocket, includeActiveConversation = false) {
    ws.send({
      type: "conversation.list",
      conversations: [{
        id: "conv-electron",
        title: "Electron agents",
        updated_at: "2026-07-12T00:00:00.000Z",
      }],
      active_conversation_id: "conv-electron",
      ...(includeActiveConversation ? { active_conversation: this.activeConversation() } : {}),
    });
  }

  private sendRecoveryRestore(ws: TestWebSocket) {
    ws.send({
      type: "session.restored",
      restored: true,
      active_conversation_id: "conv-electron",
      active_conversation: this.activeConversation(),
      conversation: this.activeConversation(),
      conversation_switched_follows: true,
      last_seq: 0,
      current_seq: 2,
      replayed_events: 2,
      session: {
        session_id: "session-electron",
        active_conversation_id: "conv-electron",
        active_conversation: this.activeConversation(),
        active_stream_conversation_ids: [],
        pending_turn_inputs: [],
      },
    });
    ws.send({
      type: "conversation.switched",
      conversation_id: "conv-electron",
      conversation: this.activeConversation(),
    });
    this.sendRecoveryReplay(ws);
  }

  private sendRecoveryReplay(ws: TestWebSocket) {
    ws.send({
      type: "session.replay",
      last_seq: 0,
      current_seq: 2,
      replayed_events: 2,
      events: [
        {
          type: "subagent.start",
          event_id: "replay-subagent-start",
          seq: 1,
          previous_replay_seq: 0,
          conversation_id: "conv-electron",
          subagent_id: "subagent-recovered",
          parent_id: "root",
          role: "verification",
          prompt: "Recover completed desktop verification",
          objective: "Recover completed desktop verification",
        },
        {
          type: "subagent.done",
          event_id: "replay-subagent-done",
          seq: 2,
          previous_replay_seq: 1,
          conversation_id: "conv-electron",
          subagent_id: "subagent-recovered",
          summary: "Recovered after desktop restart",
          snapshot: {
            result: {
              content: "Persisted result restored through session replay.",
              duration_ms: 1800,
              tool_call_count: 2,
            },
          },
          transcript_snapshot: {
            seq: 1,
            messages: [{
              id: "child-recovered-result",
              role: "assistant",
              content: "Persisted result restored through session replay.",
              timestamp: 30,
              terminal_status: "completed",
            }],
          },
        },
      ],
    });
  }
}

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const electronExecutable = path.join(repoRoot, "desktop", "node_modules", "electron", "dist", "electron.exe");
const desktopEntry = path.join(repoRoot, "desktop");
const desktopMain = path.join(desktopEntry, "main.js");
const frontendPort = Number(process.env.MINICODE_E2E_PORT ?? "43173");
const userDataPrefix = "minicode-agent-e2e-";
const isTransientWindowsLock = (error: unknown) =>
  process.platform === "win32" && (error as NodeJS.ErrnoException).code === "EBUSY";

async function cleanupStaleUserDataDirs() {
  const entries = await readdir(tmpdir(), { withFileTypes: true });
  await Promise.all(entries
    .filter((entry) => entry.isDirectory() && entry.name.startsWith(userDataPrefix))
    .map(async (entry) => {
      try {
        await rm(path.join(tmpdir(), entry.name), { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
      } catch (error) {
        if (!isTransientWindowsLock(error)) throw error;
      }
    }));
}

async function launchDesktop(backend: MultiAgentBackend, userDataDir: string) {
  const { ELECTRON_RUN_AS_NODE: _electronRunAsNode, ...cleanEnv } = process.env;
  return electron.launch({
    executablePath: electronExecutable,
    args: [desktopMain],
    cwd: desktopEntry,
    env: {
      ...cleanEnv,
      MINICODE_SKIP_BACKEND: "1",
      MINICODE_FRONTEND_URL: `http://127.0.0.1:${frontendPort}/`,
      MINICODE_API_BASE_URL: `http://127.0.0.1:${backend.port}`,
      MINICODE_WS_BASE_URL: `ws://127.0.0.1:${backend.port}`,
      MINICODE_RUNTIME_TOKEN: `e2e-${randomUUID()}`,
      MINICODE_USER_DATA_DIR: userDataDir,
    },
  });
}

async function openAgentsPanel(window: Awaited<ReturnType<Awaited<ReturnType<typeof launchDesktop>>["firstWindow"]>>) {
  const agentsTab = window.getByRole("tab", { name: /^(?:Open Agents|打开子智能体)$/ });
  if (await agentsTab.isVisible().catch(() => false)) {
    await agentsTab.click();
    return;
  }

  const collaborationButton = window.getByRole("button", { name: /打开子智能体面板/ });
  // Recovery events are intentionally streamed after the session baseline.
  // Wait for the in-chat entry before falling back to the panel launcher.
  const collaborationReady = await expect(collaborationButton)
    .toBeVisible({ timeout: 6_000 })
    .then(() => true)
    .catch(() => false);
  if (collaborationReady) {
    await collaborationButton.click();
    await expect(agentsTab).toBeVisible();
    return;
  }

  const addPanelButton = window.getByRole("button", { name: /^(?:Add panel|添加面板)$/ });
  const openRightPanelButton = window.getByRole("button", { name: /^(?:Open right panel|打开右侧栏)$/ });
  await expect(addPanelButton.or(openRightPanelButton)).toBeVisible();
  if (await openRightPanelButton.isVisible()) {
    await openRightPanelButton.click();
  }
  await expect(window.getByRole("tablist", { name: "右侧栏面板" })).toBeVisible();
  await addPanelButton.click();
  await window.getByRole("button", { name: /^(?:Show Agents|子智能体)$/ }).click();
  await expect(agentsTab).toBeVisible();
}

test("real Electron workbench supports read-only child transcripts, cancellation, lazy result, and restart recovery", async () => {
  test.setTimeout(90_000);
  const backend = new MultiAgentBackend();
  await cleanupStaleUserDataDirs();
  const userDataDir = await mkdtemp(path.join(tmpdir(), userDataPrefix));
  await backend.listen();

  try {
    const app = await launchDesktop(backend, userDataDir);
    const window = await app.firstWindow();
    await expect.poll(() => backend.commands.some((command) => command.type === "conversation.list"), {
      timeout: 15_000,
    }).toBe(true);
    await expect(window.getByText("Continue with the recovered verification path.", { exact: true })).toBeVisible();
    await expect(window.getByText("已引导当前任务", { exact: true })).toBeVisible();
    await openAgentsPanel(window);

    await expect(window.getByText("Desktop recovery", { exact: true })).toHaveCount(0);
    const runningTask = window.getByRole("button", { name: "打开子智能体任务：Verify restart recovery", exact: true });
    await expect(runningTask).toBeVisible();
    await expect(runningTask).toContainText("运行中");
    await expect(window.getByRole("progressbar", { name: "Agent 执行进度" })).toHaveCount(0);
    await expect(window.locator("body")).not.toContainText(/workflow-electron|subagent-running|node_id|task_id|iteration|tool_call/i);

    await runningTask.click();
    await expect(window.getByRole("region", { name: "子智能体任务详情：Verify restart recovery" })).toBeVisible();
    await expect(window.locator('[aria-label="子智能体工作记录"]')).toContainText("Checking the Electron restart path");
    await expect(window.getByRole("textbox", { name: "给这个子智能体发送消息" })).toHaveCount(0);
    await expect(window.getByRole("button", { name: "发送给子智能体" })).toHaveCount(0);
    expect(backend.commands.some((command) => command.type === "send_message")).toBe(false);
    await window.getByRole("button", { name: "停止子智能体" }).click();
    await expect.poll(() => backend.commands.some((command) =>
      command.type === "subagent.cancel" && command.subagent_id === "subagent-running",
    )).toBe(true);
    await expect(window.getByText("Partial verification retained after cancellation.")).toBeVisible();

    await window.getByRole("button", { name: "返回子智能体列表" }).click();
    await window.getByRole("button", { name: "打开子智能体任务：Review persisted state", exact: true }).click();
    await expect(window.getByRole("region", { name: "子智能体任务详情：Review persisted state" })).toBeVisible();
    await expect.poll(() => backend.commands.some((command) =>
      command.type === "subagent.status" && command.subagent_id === "subagent-complete",
    )).toBe(true);
    await expect(window.getByText("Recovered result body from lazy status lookup.")).toBeVisible();

    await app.close();

    const restarted = await launchDesktop(backend, userDataDir);
    const restartedWindow = await restarted.firstWindow();
    await openAgentsPanel(restartedWindow);
    await expect(restartedWindow.getByRole("button", {
      name: "打开子智能体任务：Recover completed desktop verification",
      exact: true,
    })).toBeVisible();
    await restartedWindow.getByRole("button", {
      name: "打开子智能体任务：Recover completed desktop verification",
      exact: true,
    }).click();
    await expect(restartedWindow.getByRole("region", {
      name: "子智能体任务详情：Recover completed desktop verification",
    })).toBeVisible();
    await expect(restartedWindow.getByText("Persisted result restored through session replay.")).toBeVisible();
    await restarted.close();
  } finally {
    await backend.close();
    try {
      await rm(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    } catch (error) {
      if (!isTransientWindowsLock(error)) throw error;
    }
  }
});
