/** Only the model endpoint is controlled. All messages, acknowledgements,
 * compaction, attachments and UI state come through the production desktop. */
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { createRequire } from "node:module";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
type WireMessage = { role: string; content: string | Array<{ text?: string }> };
type ProviderRequest = { model: string; stream?: boolean; messages: WireMessage[]; tools?: Array<{ function: { name: string } }> };
const repoRoot = path.resolve(import.meta.dirname, "../../..");
const desktopRoot = path.join(repoRoot, "desktop");
const executablePath = createRequire(path.join(desktopRoot, "package.json"))("electron") as string;
const evidenceRoot = path.resolve(process.env.MINICODE_COMPACT_E2E_EVIDENCE_ROOT || path.join(repoRoot, "output/playwright/compact-composer-e2e"));
const fixtureModel = "minicode-compact-ui-loopback";
const messageText = (message: WireMessage) => typeof message.content === "string" ? message.content : message.content.map(part => part.text || "").join("\n");

class CompactProvider {
  readonly requests: Fact[] = [];
  readonly failures: Fact[] = [];
  readonly gates = new Map<number, (fail: boolean) => void>();
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ object: "list", data: [{ id: fixtureModel, object: "model", owned_by: "controlled-test" }] }));
      return;
    }
    const buffers: Buffer[] = [];
    for await (const chunk of request) buffers.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(buffers).toString("utf8")) as ProviderRequest;
    const texts = body.messages.map(messageText);
    const memory = texts.some(text => text.includes("## Memory Writing Agent: Phase 2 (Consolidation)"));
    const compact = !memory && body.messages.some(message => message.role === "system"
      && messageText(message).includes("You are a context summarization assistant."));
    const turn = body.messages.filter(message => message.role === "user").map(messageText)
      .findLast(text => /COMPACT_UI_TURN_\d/.test(text))?.match(/COMPACT_UI_TURN_(\d)/)?.[1];
    const route = memory ? "memory-consolidation" : compact ? "compact" : body.tools?.length && turn !== undefined ? "foreground" : "auxiliary";
    const index = this.requests.filter(item => item.route === route).length;
    this.requests.push({ at: Date.now(), route, index, turn, model: body.model, stream: body.stream,
      tools: body.tools?.map(tool => tool.function.name) || [] });
    let content = route === "foreground"
      ? `COMPACT_UI_REPLY_${turn}\n已记录第 ${turn} 轮用户要求，继续保留原始会话内容、引用和附件。`
      : route === "memory-consolidation" ? "Controlled maintenance: no file changes needed." : "上下文压缩测试";
    if (compact) {
      const fail = await new Promise<boolean>(resolve => this.gates.set(index, resolve));
      if (fail) {
        response.writeHead(400, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: { message: "COMPACT_UI_EXPECTED_FAILURE", type: "controlled_compaction_rejection" } }));
        return;
      }
      content = "## Goal\nContinue the user's existing compact UI task.\n\n## Constraints\nPreserve original history, quoted reply, attachment, and the new unsent draft.\n\n## Progress\nEight real user turns were completed.\n\n## Next Steps\nContinue from the existing task.";
    }
    if (!body.stream) {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ id: `chatcmpl-${route}-${index}`, object: "chat.completion", model: fixtureModel,
        choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }],
        usage: { prompt_tokens: 500, completion_tokens: 100, total_tokens: 600 } }));
      return;
    }
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    for (const [delta, finish_reason] of [[{ role: "assistant" }, null], [{ content }, null], [{}, "stop"]] as const) {
      response.write(`data: ${JSON.stringify({ id: `chatcmpl-${route}-${index}`, object: "chat.completion.chunk", model: fixtureModel,
        choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
    }
    response.end("data: [DONE]\n\n");
  });
  async start() {
    await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve));
    return `http://127.0.0.1:${(this.server.address() as { port: number }).port}/v1`;
  }
  release(index: number, fail = false) { this.gates.get(index)!(fail); this.gates.delete(index); }
  async close() {
    for (const gate of this.gates.values()) gate(false);
    this.gates.clear();
    await new Promise<void>((resolve, reject) => this.server.close(error => error ? reject(error) : resolve()));
  }
}

async function availablePort() {
  const server = createPortServer();
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  await new Promise<void>(resolve => server.close(() => resolve()));
  return port;
}

async function launch(profile: string, providerUrl: string) {
  const { ELECTRON_RUN_AS_NODE: _nodeMode, ...environment } = process.env;
  return electron.launch({ executablePath, args: [path.join(desktopRoot, "main.js")], cwd: desktopRoot,
    env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await availablePort()),
      MINICODE_FRONTEND_URL: "", MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
      MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"),
      MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key",
      CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: fixtureModel, CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
      OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
}

async function hide(app: ElectronApplication) {
  await app.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows()[0];
    window.webContents.setBackgroundThrottling(false);
    window.setIgnoreMouseEvents(true);
    window.hide();
  });
}

async function capture(app: ElectronApplication, page: Page, name: string) {
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].showInactive());
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
  await writeFile(path.join(evidenceRoot, `${name}.png`), Buffer.from(png, "base64"));
  await hide(app);
}

async function composerContext(page: Page) {
  return page.evaluate(() => {
    const state = (window as any).__zustandStore.getState();
    return { quote: state.quotedMessage, attachments: state.attachments.map((attachment: any) => ({
      id: attachment.id, name: attachment.name, status: attachment.status,
      artifactId: attachment.artifactId || attachment.attachment?.artifact_id || "",
    })) };
  });
}

async function durableContext(data: string, conversationId: string) {
  // Read the same materialized durable record as the backend, including the
  // manifest's committed projection log above the immutable base generation.
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "from backend.conversations.repository import ConversationRepository",
    "record = ConversationRepository(Path(sys.argv[1]) / 'conversations').get_conversation(sys.argv[2])",
    "print(json.dumps({'transcript': ''.join(json.dumps(message, ensure_ascii=False, sort_keys=True) + '\\n' for message in record.transcript), 'snapshot': record.context_snapshot}, ensure_ascii=False))",
  ].join("\n");
  const result = await promisify(execFile)(process.env.MINICODE_PYTHON || "python", ["-c", script, data, conversationId], {
    cwd: repoRoot, windowsHide: true, encoding: "utf8", maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  return JSON.parse(result.stdout) as { transcript: string; snapshot: Fact };
}

test("COMPACT: production composer acknowledges promptly and preserves draft, quote, attachment and durable history", async () => {
  test.setTimeout(240_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-compact-ui-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const workspace = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-compact-ui-workspace-"));
  await mkdir(data, { recursive: true });
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN" }));
  const attachment = path.join(workspace, "compact-note.txt");
  await writeFile(attachment, "COMPACT_UI_ATTACHMENT_BYTES\nKeep this attachment for the next actual user prompt.\n");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: {
    code_mode_only: true, max_iterations: 8, compaction_keep_recent_tokens: 64,
  } }));
  const provider = new CompactProvider(), incoming: Fact[] = [], outgoing: Fact[] = [], verdicts: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  let testFailure: string | undefined;
  try {
    app = await launch(profile, providerUrl);
    const page = await app.firstWindow();
    await hide(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(page.url()).toContain("/dist/index.html");
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameReceived", ({ response }) => {
      if (response.opcode === 1) incoming.push(JSON.parse(response.payloadData));
    });
    cdp.on("Network.webSocketFrameSent", ({ response }) => {
      if (response.opcode === 1) outgoing.push(JSON.parse(response.payloadData));
    });
    // These are the same production actions as the workspace/new-chat UI.
    // No messages, backend events or conversation transcript are seeded.
    await page.evaluate(root => {
      const state = (window as any).__zustandStore.getState();
      state.setWorkingDirectory(root);
      state.createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
    }, workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBeTruthy();
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"))).toBe(true);
    const composer = page.locator(".composer-input-region textarea").first();
    for (let index = 0; index < 8; index++) {
      await composer.fill(`COMPACT_UI_TURN_${index}: Keep the actual completed turn ${index} and continue the same task.`);
      await composer.press("Enter");
      await expect(page.locator(".assistant-cell-wrap").filter({ hasText: `COMPACT_UI_REPLY_${index}` })).toBeVisible({ timeout: 30_000 });
      await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 30_000 }).toBe(false);
    }
    const conversationId = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId) as string;
    // The rendered final answer can precede its final durable write. Capture
    // the preservation baseline only after all eight actual turns are saved.
    await expect.poll(async () => (await durableContext(data, conversationId)).transcript.trim().split("\n")
      .map(line => JSON.parse(line) as Fact)
      .filter(message => message.role === "assistant" && message.terminal_status === "completed"
        && message.content.includes("COMPACT_UI_REPLY_")).length, { timeout: 30_000 }).toBe(8);
    const historyBefore = await durableContext(data, conversationId);
    const lastReply = page.locator(".assistant-cell-wrap").filter({ hasText: "COMPACT_UI_REPLY_7" });
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].showInactive());
    await lastReply.hover();
    await lastReply.getByRole("button", { name: "引用回复", exact: true }).click();
    await hide(app);
    await page.locator('input[type="file"]').first().setInputFiles(attachment);
    await expect.poll(async () => (await composerContext(page)).attachments[0]?.status).toBe("ready");
    const originalComposer = await composerContext(page);
    expect(originalComposer.quote.content).toContain("COMPACT_UI_REPLY_7");
    expect(originalComposer.attachments[0].artifactId).toBeTruthy();

    // The first command goes through the actual slash menu's Enter handler.
    await composer.fill("/compact");
    await expect(page.getByRole("option").filter({ hasText: "/compact" }).first()).toBeVisible();
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has(0), { timeout: 30_000 }).toBe(true);
    const compactCommands = () => outgoing.filter(event => event.type === "user_message" && event.content === "/compact");
    expect(compactCommands()).toHaveLength(1);
    const firstId = compactCommands()[0].client_command_id;
    expect(incoming.some(event => event.type === "client.command.ack" && event.client_command_id === firstId && event.accepted !== false)).toBe(true);
    expect(incoming.some(event => event.type === "command.result" && event.client_command_id === firstId && event.data?.native_completion === "completed")).toBe(false);
    await expect(composer).toHaveValue("");
    await expect(composer).toBeEditable();
    await expect(page.locator(".status-notice-title").filter({ hasText: /^正在压缩上下文$/ })).toBeVisible();
    await expect(page.locator(".status-notice-cell[data-processing='true']")).toHaveCount(1);
    expect(await composerContext(page)).toEqual(originalComposer);
    await composer.fill("FIRST_COMPACT_NEW_UNSENT_DRAFT");
    await capture(app, page, "01-real-compacting-receipt-clears-input-preserves-context");
    provider.release(0);
    await expect(page.locator(".status-notice-title").filter({ hasText: /^上下文已压缩$/ })).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => incoming.some(event => event.type === "command.result" && event.client_command_id === firstId && event.data?.native_completion === "completed"), { timeout: 30_000 }).toBe(true);
    await expect(page.locator(".status-notice-cell[data-processing='true']")).toHaveCount(0);
    await expect(composer).toHaveValue("FIRST_COMPACT_NEW_UNSENT_DRAFT");
    expect(await composerContext(page)).toEqual(originalComposer);
    const historyAfterSuccess = await durableContext(data, conversationId);
    expect(historyAfterSuccess.transcript).toBe(historyBefore.transcript);
    expect(historyAfterSuccess.snapshot.compaction_count).toBeGreaterThan(historyBefore.snapshot.compaction_count);
    await capture(app, page, "02-real-compacted-new-draft-quote-attachment-retained");
    verdicts.push({ scenario: "durable ACK clears compact input before completion; actual compacting/completed row; new draft, quote, attachment and original transcript remain", passed: true });

    // The second command exercises the actual send button with the slash menu open.
    await composer.fill("/compact");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => provider.gates.has(1), { timeout: 30_000 }).toBe(true);
    expect(compactCommands()).toHaveLength(2);
    const secondId = compactCommands()[1].client_command_id;
    await expect(composer).toHaveValue("");
    await expect(composer).toBeEditable();
    await expect(page.locator(".status-notice-title").filter({ hasText: /^正在压缩上下文$/ })).toBeVisible();
    await composer.fill("FAILED_COMPACT_NEW_UNSENT_DRAFT");
    await capture(app, page, "03-real-second-compacting-from-send-button");
    provider.release(1, true);
    await expect(page.locator(".status-notice-title").filter({ hasText: /^压缩失败$/ })).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => incoming.some(event => event.type === "command.result" && event.client_command_id === secondId && event.data?.native_completion === "refused"), { timeout: 30_000 }).toBe(true);
    const failureResult = incoming.find(event => event.type === "command.result" && event.client_command_id === secondId);
    expect(failureResult?.level).toBe("error");
    expect(failureResult?.message).toContain("status=400");
    expect(failureResult?.message).toContain("provider_error_schema_type=controlled_compaction_rejection");
    await expect(page.locator(".status-notice-cell[role='alert']")).toContainText("400");
    await expect(page.locator(".status-notice-cell[data-processing='true']")).toHaveCount(0);
    await expect(composer).toHaveValue("FAILED_COMPACT_NEW_UNSENT_DRAFT");
    expect(await composerContext(page)).toEqual(originalComposer);
    const historyAfterFailure = await durableContext(data, conversationId);
    expect(historyAfterFailure.transcript).toBe(historyAfterSuccess.transcript);
    expect(historyAfterFailure.snapshot.history).toEqual(historyAfterSuccess.snapshot.history);
    expect(historyAfterFailure.snapshot.compaction_count).toBe(historyAfterSuccess.snapshot.compaction_count);
    expect(incoming.filter(event => event.type === "conversation.compaction.updated").map(event => event.state)).toEqual(["compacting", "compacted", "compacting", "failed"]);
    expect(provider.requests.filter(request => request.route === "foreground")).toHaveLength(8);
    expect(provider.failures).toEqual([]);
    await capture(app, page, "04-real-compaction-failure-keeps-new-draft-and-history");
    verdicts.push({ scenario: "send-button compact receives promptly; real HTTP failure terminates visible processing while preserving new draft, original quote/attachment and compressed history", passed: true });
  } catch (error) {
    testFailure = error instanceof Error ? error.stack || error.message : String(error);
    throw error;
  } finally {
    for (const gate of provider.gates.values()) gate(false);
    provider.gates.clear();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidenceRoot, "compact-composer-evidence.json"), JSON.stringify({ environment: "CONTROLLED model HTTP; production built Electron/frontend/backend/dispatcher/ACK/compaction/CAS; no message or event/store fixtures",
      passed: !testFailure && verdicts.length === 2, testFailure, testRoot, workspace, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});
