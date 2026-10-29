/** Controlled loopback Provider; production Electron, WebSocket and harness.
 * No store fixtures and no simulated backend/lifecycle events are used here.
 */
import { _electron as electron, expect, test, type Dialog, type ElectronApplication, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

type WireMessage = { role: string; content: string | Array<{ text?: string }>; tool_call_id?: string };
type ProviderRequest = { model: string; messages: WireMessage[]; tools?: Array<{ function: { name: string } }> };
type Fact = Record<string, any>;
const repoRoot = path.resolve(import.meta.dirname, "../../..");
const desktopRoot = path.join(repoRoot, "desktop");
const electronExecutable = createRequire(path.join(desktopRoot, "package.json"))("electron") as string;
const evidenceRoot = path.resolve(process.env.MINICODE_CONTROLLED_E2E_EVIDENCE_ROOT || path.join(repoRoot, "artifacts", "controlled-multiagent-e2e"));
const fixtureModel = "minicode-controlled-loopback";

class ControlledProvider {
  constructor(readonly version: "v1" | "v2" | "v2-error" | "queue-regen" | "root-provider-error" | "root-provider-retry" | "question-input" | "approval-timeout" = "v1") {}
  readonly requests: Fact[] = [];
  readonly failures: Fact[] = [];
  readonly counts = new Map<string, number>();
  readonly gates = new Map<string, () => void>();
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ object: "list", data: [{ id: fixtureModel, object: "model", owned_by: "controlled-test" }] }));
      return;
    }
    const buffers: Buffer[] = [];
    for await (const chunk of request) buffers.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(buffers).toString("utf8")) as ProviderRequest;
    const userText = body.messages.filter((message) => message.role === "user")
      .map((message) => typeof message.content === "string" ? message.content : message.content.map((part) => part.text || "").join("\n"))
      .findLast((text) => this.version === "approval-timeout" ? /CONTROLLED_APPROVAL_PARENT:/.test(text)
        : this.version === "question-input" ? /CONTROLLED_QUESTION_PARENT:/.test(text)
        : this.version === "root-provider-retry" ? /CONTROLLED_ROOT_PROVIDER_RETRY:/.test(text)
        : this.version === "root-provider-error" ? /CONTROLLED_ROOT_PROVIDER_ERROR:/.test(text)
        : this.version === "queue-regen" ? /CONTROLLED_QUEUE_(FIRST|BLOCK):/.test(text)
        : this.version === "v2-error" ? /CONTROLLED_WAIT_ERROR_PARENT:/.test(text)
        : this.version === "v2" ? /CONTROLLED_WAIT_(CHILD|PARENT):/.test(text)
        : /CONTROLLED_(FOLLOWUP_A|WORKER_A|WORKER_B|PROVIDER_FAILURE|PREFLIGHT_REJECT|FAILURE_PARENT|MAIN|SHOULD_NOT_RUN)/.test(text)) || "";
    const route = !body.tools?.length ? "auxiliary"
      : userText.includes("CONTROLLED_APPROVAL_PARENT") ? "approval-parent"
      : userText.includes("CONTROLLED_QUESTION_PARENT") ? "question-parent"
      : userText.includes("CONTROLLED_ROOT_PROVIDER_RETRY") ? "root-provider-retry"
      : userText.includes("CONTROLLED_ROOT_PROVIDER_ERROR") ? "root-provider-error"
      : userText.includes("CONTROLLED_QUEUE_FIRST") ? "queue-first"
      : userText.includes("CONTROLLED_QUEUE_BLOCK") ? "queue-block"
      : userText.includes("CONTROLLED_WAIT_ERROR_PARENT") ? "wait-error-parent"
      : userText.includes("CONTROLLED_WAIT_CHILD") ? "wait-child"
      : userText.includes("CONTROLLED_WAIT_PARENT") ? "wait-parent"
      : userText.includes("CONTROLLED_FOLLOWUP_A") ? "followup"
      : userText.includes("CONTROLLED_WORKER_A") ? "worker-a"
      : userText.includes("CONTROLLED_WORKER_B") ? "worker-b"
      : userText.includes("CONTROLLED_PROVIDER_FAILURE") ? "provider-failure"
      : userText.includes("CONTROLLED_PREFLIGHT_REJECT") ? "reject-parent"
      : userText.includes("CONTROLLED_FAILURE_PARENT") ? "failure-parent"
      : userText.includes("CONTROLLED_SHOULD_NOT_RUN") ? "unexpected-child" : "parent";
    const index = this.counts.get(route) || 0;
    this.counts.set(route, index + 1);
    this.requests.push({ at: Date.now(), route, index, model: body.model,
      roles: body.messages.map((message) => message.role), tools: body.tools?.map((tool) => tool.function.name) || [],
      toolReceipts: body.messages.filter((message) => message.role === "tool").map((message) => ({ id: message.tool_call_id, content: message.content })) });
    if (route === "root-provider-error") {
      response.writeHead(403, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: "CONTROLLED_GATEWAY_POLICY_REASON: project policy disallows this model", type: "permission_error", code: "controlled_policy" } }));
      return;
    }
    if (route === "root-provider-retry" && index < 2) {
      response.writeHead(index === 0 ? 429 : 503, { "content-type": "application/json", "retry-after": "0" });
      response.end(JSON.stringify({ error: index === 0
        ? { message: "CONTROLLED_RATE_LIMIT_REASON: regional quota is exhausted", type: "rate_limit_error", code: "controlled_quota" }
        : { message: "CONTROLLED_RETRY_GATEWAY_REASON: upstream queue was reset", type: "server_error", code: "controlled_retry" } }));
      return;
    }
    if (route === "provider-failure" || route === "unexpected-child") {
      response.writeHead(400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: "CONTROLLED_PROVIDER_ERROR", type: "invalid_request_error", code: "controlled_fixture" } }));
      return;
    }
    if (["worker-a", "worker-b", "wait-child", "queue-block", "question-parent", "approval-parent"].includes(route) && index === 0) {
      await new Promise<void>((resolve) => this.gates.set(route, resolve));
    }
    let content = "";
    let contractFailure = "";
    let calls: Array<{ id: string; type: "function"; function: { name: string; arguments: string } }> = [];
    const call = (name: string, args: Fact) => {
      if (!body.tools?.some((tool) => tool.function.name === name)) {
        contractFailure = `${route}: real tool schema ${name} was not exposed`;
        return;
      }
      calls = [{ id: `controlled-${route}-${index}`, type: "function", function: { name, arguments: JSON.stringify(args) } }];
    };
    if (this.version === "approval-timeout" && route === "approval-parent") {
      if (index === 0) call("run_command", { command: 'Set-Content -LiteralPath approval-must-not-run.txt -Value "unexpected"', login: false });
      else content = "CONTROLLED_APPROVAL_FINAL";
    } else if (this.version === "question-input" && route === "question-parent") {
      if (index === 0) call("request_user_input", { questions: [{ id: "detail", header: "Detail",
        question: "How should the exact input be retained?", options: [{ label: "Keep", description: "Keep the supplied value." }] }] });
      else content = "CONTROLLED_QUESTION_FINAL";
    } else if (this.version === "root-provider-retry" && route === "root-provider-retry") {
      content = "CONTROLLED_ROOT_PROVIDER_RETRY_FINAL";
    } else if (this.version === "queue-regen" && route === "queue-first") {
      content = index === 0
        ? `CONTROLLED_QUEUE_FIRST_FINAL${userText.includes("workspace reference") ? "\n\n[工作区文件](controlled-workspace.md)" : ""}`
        : "CONTROLLED_QUEUE_REGENERATED";
    } else if (this.version === "queue-regen" && route === "queue-block") {
      content = "CONTROLLED_QUEUE_BLOCK_FINAL";
    } else if (this.version === "v2-error" && route === "wait-error-parent") {
      if (index === 0) call("wait_agent", { timeout_ms: 30001 });
      else {
        if (!JSON.stringify(body.messages).includes("timeout_ms must be at most 30000")) {
          contractFailure = "V2 parent did not receive the real wait-agent error";
        }
        content = "CONTROLLED_WAIT_ERROR_FINAL";
      }
    } else if (this.version === "v2" && route === "wait-parent") {
      if (index === 0) call("spawn_agent", { task_name: "wait_audit", fork_turns: "none",
        message: "CONTROLLED_WAIT_CHILD: Read fact-a.txt and return the exact fact." });
      else if (index <= 5) call("wait_agent", { timeout_ms: index === 5 ? 30000 : 100 });
      else {
        const history = JSON.stringify(body.messages);
        if (!history.includes("CONTROLLED_WAIT_CHILD_FINAL") || !history.includes("CONTROLLED_FACT_A=alpha")) {
          contractFailure = "V2 parent did not consume its actual child's completed file result";
        }
        content = "CONTROLLED_WAIT_PARENT_FINAL";
      }
    } else if (this.version === "v2" && route === "wait-child") {
      if (index === 0) call("read_file", { file_path: "fact-a.txt" });
      else {
        const output = body.messages.filter(message => message.role === "tool").map(message => String(message.content)).join("\n");
        if (!output.includes("CONTROLLED_FACT_A=alpha")) contractFailure = "V2 child did not receive the actual read_file result";
        content = "CONTROLLED_WAIT_CHILD_FINAL: CONTROLLED_FACT_A=alpha";
      }
    } else if (route === "parent" && index === 0) {
      content = "受控环境：让两个子智能体实际读取文件并向父代理报告。";
      call("task", { parallel_tasks: [
        { description: "Audit A", prompt: "CONTROLLED_WORKER_A: Read fact-a.txt, report to parent, then finish.", agent_type: "explorer", read_only: true },
        { description: "Audit B", prompt: "CONTROLLED_WORKER_B: Read fact-b.txt, report to parent, then finish.", agent_type: "explorer", read_only: true },
      ] });
    } else if (route === "parent" && index === 1) {
      const toolOutput = body.messages.filter((message) => message.role === "tool").map((message) => String(message.content)).join("\n");
      const target = /subagent_id:\s*(subagent[-_][\w-]+)/.exec(toolOutput)?.[1];
      if (!target) contractFailure = "Real TaskTool result did not expose an actual recipient id";
      call("followup_task", { target, message: "CONTROLLED_FOLLOWUP_A: Read fact-a.txt once more and report the new final result." });
    } else if (route === "worker-a" || route === "worker-b" || route === "followup") {
      const letter = route === "worker-b" ? "b" : "a";
      if (index === 0) call("read_file", { file_path: `fact-${letter}.txt` });
      else if (index === 1) {
        const toolOutput = body.messages.filter((message) => message.role === "tool").map((message) => String(message.content)).join("\n");
        const fact = new RegExp(`CONTROLLED_FACT_${letter.toUpperCase()}=\\w+`).exec(toolOutput)?.[0];
        if (!fact) contractFailure = `${route}: real read_file result did not contain its fixture fact`;
        call("send_message", { target: "parent", message: `CONTROLLED_REPORT_${letter.toUpperCase()}: ${fact}`, summary: `Audit ${letter.toUpperCase()} report` });
      }
      else content = route === "followup" ? "CONTROLLED_FOLLOWUP_FINAL" : `CONTROLLED_CHILD_${letter.toUpperCase()}_FINAL`;
    } else if (route === "reject-parent" && index === 0) {
      call("task", { description: "Refused worktree", prompt: "CONTROLLED_SHOULD_NOT_RUN", agent_type: "worker", isolation: "worktree" });
    } else if (route === "failure-parent" && index === 0) {
      call("task", { description: "Provider failure", prompt: "CONTROLLED_PROVIDER_FAILURE", agent_type: "worker" });
    } else content = route === "auxiliary" ? "受控多 agent 验收" : route === "reject-parent" ? "CONTROLLED_REJECT_FINAL"
      : route === "failure-parent" ? "CONTROLLED_FAILURE_FINAL" : "CONTROLLED_MAIN_FINAL";
    if (contractFailure) {
      this.failures.push({ at: Date.now(), route, index, message: contractFailure });
      response.writeHead(400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: contractFailure, type: "controlled_fixture_contract_error" } }));
      return;
    }
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const stream = (delta: Fact, finishReason: string | null) => response.write(`data: ${JSON.stringify({ id: `chatcmpl-controlled-${route}-${index}`, object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model: fixtureModel,
      choices: [{ index: 0, delta, finish_reason: finishReason }] })}\n\n`);
    stream({ role: "assistant" }, null);
    if (content) stream({ content }, null);
    if (calls.length) stream({ tool_calls: calls.map((item, toolIndex) => ({ index: toolIndex, ...item })) }, null);
    stream({}, calls.length ? "tool_calls" : "stop");
    response.end("data: [DONE]\n\n");
  });
  async start() {
    await new Promise<void>((resolve) => this.server.listen(0, "127.0.0.1", resolve));
    return `http://127.0.0.1:${(this.server.address() as { port: number }).port}/v1`;
  }
  releaseChildren() { for (const release of this.gates.values()) release(); this.gates.clear(); }
  async close() { this.releaseChildren(); await new Promise<void>((resolve, reject) => this.server.close((error) => error ? reject(error) : resolve())); }
}

async function availablePort() {
  const server = createPortServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return port;
}

async function launch(profile: string, providerUrl: string, offscreen = false) {
  const { ELECTRON_RUN_AS_NODE: _nodeMode, ...environment } = process.env;
  const sandboxTemp = path.join(profile, "sandbox-temp");
  await mkdir(sandboxTemp, { recursive: true });
  const entry = offscreen ? path.join(profile, "offscreen-entry.cjs") : path.join(desktopRoot, "main.js");
  if (offscreen) await writeFile(entry, `const { app } = require('electron');\napp.on('browser-window-created', (_, win) => { win.setPosition(-16000, -16000); win.show = win.showInactive.bind(win); });\nrequire(${JSON.stringify(path.join(desktopRoot, "main.js"))});\n`);
  return electron.launch({ executablePath: electronExecutable, args: [entry], cwd: desktopRoot,
    env: { ...environment, TEMP: sandboxTemp, TMP: sandboxTemp, TMPDIR: sandboxTemp,
      MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await availablePort()),
      MINICODE_FRONTEND_URL: "", MINICODE_FRONTEND_DIST_FILE: process.env.MINICODE_CONTROLLED_FRONTEND_FILE || "",
      MINICODE_CONFIG_DIR: path.join(profile, "config"),
      MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"),
      MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
      LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key", CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: fixtureModel,
      CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "", CUSTOM_RESPONSES_REASONING_SUMMARY: "off",
      OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
}

async function isolateQaWindow(app: ElectronApplication) {
  await app.evaluate(({ BrowserWindow }) => {
    const qaWindow = BrowserWindow.getAllWindows()[0];
    qaWindow.webContents.setBackgroundThrottling(false);
    qaWindow.setIgnoreMouseEvents(true);
    qaWindow.hide();
  });
}

async function capture(app: ElectronApplication, page: Page, name: string) {
  await app.evaluate(({ BrowserWindow }) => {
    const qaWindow = BrowserWindow.getAllWindows()[0];
    qaWindow.webContents.setBackgroundThrottling(false);
    if (qaWindow.isMinimized()) qaWindow.restore();
    qaWindow.showInactive();
  });
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.waitForTimeout(250);
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
  await writeFile(path.join(evidenceRoot, `${name}.png`), Buffer.from(png, "base64"));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].hide());
}

async function attachWireEvidence(page: Page, incoming: Fact[], outgoing: Fact[]) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  cdp.on("Network.webSocketFrameReceived", ({ response }) => {
    if (response.opcode !== 1) return;
    const event = JSON.parse(response.payloadData);
    if (/^(subagent\.|tool_call$|tool_result$|done$|error$|control_request$|approval\.cancelled$|agent\.progress$|agent\.run\.)/.test(event.type)
      || event.type === "command.result" && event.command === "subagent.transcript") incoming.push(event);
  });
  cdp.on("Network.webSocketFrameSent", ({ response }) => {
    if (response.opcode !== 1) return;
    const command = JSON.parse(response.payloadData);
    if (/^(user_message|interrupt|control_response|conversation\.(create|switch)|subagent\.(status|transcript)|session\.restore)$/.test(command.type)) outgoing.push(command);
  });
}

async function sendPrompt(page: Page, content: string, final: string) {
  const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
  await expect(composer).toBeVisible();
  await expect(composer).toHaveAttribute("data-rich-composer", "");
  await composer.fill(content);
  await composer.press("Enter");
  await expect.poll(() => page.evaluate((marker) => (window as any).__zustandStore.getState().messages.some((message: any) => message.role === "assistant" && message.content.includes(marker)), final), { timeout: 60_000 }).toBe(true);
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 60_000 }).toBe(false);
}

async function readAgents(page: Page): Promise<Fact[]> {
  return page.evaluate(() => (window as any).__zustandStore.getState().subagents.map((agent: any) => ({
    id: agent.id, displayName: agent.displayName, objective: agent.objective, status: agent.status, agentPath: agent.agentPath,
    mailboxEpoch: agent.mailboxEpoch, startedAt: agent.startedAt, completedAt: agent.completedAt, workspaceRoot: agent.workspaceRoot,
    model: agent.model, resultContent: agent.resultContent, resultError: agent.resultError,
  })));
}

async function openAgentRecord(page: Page, name: string) {
  const summary = page.locator("#thread-summary-panel");
  if (!await summary.isVisible()) await page.getByRole("button", { name: "切换摘要", exact: true }).click();
  await expect(summary).toBeVisible();
  await summary.getByRole("button", { name: `打开子智能体：${name}`, exact: true }).click();
  await expect(page.locator(".subagents-detail")).toBeVisible();
}

test("CONTROLLED environment: real multi-agent start, follow-up, refusal, failure and durable desktop replay", async () => {
  test.setTimeout(240_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-controlled-multiagent-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  // The user home can itself be a Git checkout; its AppData temp tree is not
  // a non-Git workspace. Windows Temp is outside that actual repository.
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-controlled-workspace-"));
  const workspace = path.join(fixtureRoot, "workspace");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  const gitProbe = spawnSync("git", ["-C", workspace, "rev-parse", "--show-toplevel"], { encoding: "utf8" });
  expect(gitProbe.status, "The refusal fixture must have no real ancestor Git repository").toBe(128);
  expect(gitProbe.stderr).toContain("not a git repository");
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(path.join(workspace, "fact-a.txt"), "CONTROLLED_FACT_A=alpha\n");
  await writeFile(path.join(workspace, "fact-b.txt"), "CONTROLLED_FACT_B=beta\n");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 }, features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider(), incoming: Fact[] = [], outgoing: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const verdicts: Fact[] = [];
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    expect(await app.evaluate(({ app }) => app.getPath("userData"))).toBe(profile);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(new URL(page.url()).protocol).toBe("file:");
    await attachWireEvidence(page, incoming, outgoing);
    // These are the ordinary production workspace/conversation/permission
    // actions. Agent and message state is only read, never populated by tests.
    await page.evaluate(async (root) => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
    }, workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBeTruthy();
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"))).toBe(true);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await expect(composer).toBeVisible();
    await expect(composer).toHaveAttribute("data-rich-composer", "");
    await composer.fill("CONTROLLED_MAIN: Exercise the local deterministic multi-agent audit.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.size, { timeout: 45_000 }).toBe(2);
    await expect.poll(() => incoming.filter((event) => event.type === "subagent.start").length, { timeout: 30_000 }).toBe(2);
    const live = await readAgents(page);
    expect(live.filter((agent) => agent.status === "running" && agent.startedAt > 0)).toHaveLength(2);
    await expect(page.locator(".collaboration-delegated-start")).toHaveCount(2);
    await page.getByRole("button", { name: "切换摘要", exact: true }).click();
    const summary = page.locator("#thread-summary-panel");
    await expect(summary).toBeVisible();
    await expect(summary).toHaveAttribute("data-surface", /^(inline|popover)$/);
    await expect(summary.getByRole("region", { name: "子智能体摘要" })).toContainText("2 运行中");
    await capture(app, page, "01-real-start-main-and-summary");
    await summary.getByRole("button", { name: "子智能体", exact: true }).click();
    await expect(page.locator("#right-panel-subagents")).toBeVisible();
    await expect(summary).toHaveCount(0);
    await expect(page.getByRole("button", { name: "切换摘要", exact: true })).toHaveAttribute("aria-pressed", "false");
    await expect(page.locator(".subagents-tab")).toBeVisible();
    await expect(page.locator(".subagents-row").first()).toBeVisible();
    await expect(page.locator(".subagents-row")).toHaveCount(2);
    await capture(app, page, "02-real-running-list");
    const activeWorker = live[0];
    await page.getByRole("button", { name: `打开子智能体任务：${activeWorker.displayName || activeWorker.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toBeVisible();
    await expect(page.locator(".subagents-context")).not.toContainText("运行中");
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    await expect(page.locator(".subagents-row").first()).toBeVisible();
    provider.releaseChildren();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().messages.some((message: any) => message.role === "assistant" && message.content.includes("CONTROLLED_MAIN_FINAL"))), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => incoming.some((event) => event.type === "subagent.done" && event.result?.content?.includes("CONTROLLED_FOLLOWUP_FINAL")), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 60_000 }).toBe(false);
    const finished = await readAgents(page);
    expect(finished).toHaveLength(2);
    expect(finished.every((agent) => agent.status === "done")).toBe(true);
    expect(finished.some((agent) => agent.mailboxEpoch === 2)).toBe(true);
    const workerA = finished.find((agent) => agent.mailboxEpoch === 2)!;
    await page.getByRole("button", { name: `打开子智能体任务：${workerA.displayName || workerA.objective}`, exact: true }).click();
    await expect(page.locator("#right-panel-subagents")).toBeVisible();
    await expect(page.locator(".subagents-detail")).toBeVisible();
    await expect(page.locator(".subagents-detail")).toContainText("CONTROLLED_FOLLOWUP_FINAL", { timeout: 30_000 });
    const processRegions = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(processRegions).toHaveCount(2);
    for (let turn = 0; turn < 2; turn += 1) {
      const disclosure = processRegions.nth(turn).locator("button[aria-expanded]").first();
      if (await disclosure.getAttribute("aria-expanded") === "false") await disclosure.click();
      await expect(disclosure).toHaveAttribute("aria-expanded", "true");
    }
    await expect(page.locator(".subagents-detail")).toContainText("已向父代理发送消息");
    await expect(page.locator(".subagents-detail textarea, .subagents-detail [role=textbox], .subagents-detail .user-cell-wrap")).toHaveCount(0);
    const completedWorker = incoming.findLast((event) => event.type === "subagent.done" && event.subagent_id === workerA.id)!;
    const childRecords = completedWorker.transcript_snapshot.messages.flatMap((message: Fact) => message.blocks ?? [])
      .filter((block: Fact) => block.type === "tool_call").map((block: Fact) => block.record);
    const reports = childRecords.filter((record: Fact) => record.name === "send_message");
    expect(reports).toHaveLength(2);
    expect(reports.every((record: Fact) => record.status === "success" && record.recipientId === "parent"
      && record.args.target === "parent" && record.args.message.includes("CONTROLLED_FACT_A=alpha"))).toBe(true);
    expect(childRecords.filter((record: Fact) => record.name === "read_file" && record.status === "success"
      && record.summary.includes("CONTROLLED_FACT_A=alpha"))).toHaveLength(2);
    await capture(app, page, "03-real-child-transcript-and-followup");
    verdicts.push({ scenario: "two children and parent send/follow-up", passed: true, agents: finished });
    await page.getByRole("button", { name: "返回子智能体列表" }).click();
    const refusalStart = incoming.length;
    await sendPrompt(page, "CONTROLLED_PREFLIGHT_REJECT: Request a git worktree in this deliberate non-git fixture workspace.", "CONTROLLED_REJECT_FINAL");
    const refusalEvents = incoming.slice(refusalStart);
    const refused = refusalEvents.find((event) => event.type === "subagent.done" && event.record?.prompt_summary === "Refused worktree")!;
    expect(refused).toBeTruthy();
    expect(refused.status).toBe("failed");
    expect(refused.record.started_at).toBe(0);
    expect(refusalEvents.some((event) => event.type === "subagent.start" && event.subagent_id === refused.subagent_id)).toBe(false);
    expect(provider.requests.some((request) => request.route === "unexpected-child")).toBe(false);
    const refusedAgent = (await readAgents(page)).find((agent) => agent.id === refused.subagent_id)!;
    await openAgentRecord(page, refusedAgent.displayName || refusedAgent.objective);
    await expect(page.locator(".subagents-detail [role=alert]")).toBeVisible();
    await capture(app, page, "04-real-preflight-refusal");
    verdicts.push({ scenario: "actual non-git worktree preflight refused", passed: true, terminal: refused });
    await page.getByRole("button", { name: "返回子智能体列表" }).click();
    const failureStart = incoming.length;
    await sendPrompt(page, "CONTROLLED_FAILURE_PARENT: Delegate the loopback Provider error scenario.", "CONTROLLED_FAILURE_FINAL");
    const failureEvents = incoming.slice(failureStart);
    const failed = failureEvents.find((event) => event.type === "subagent.done" && event.record?.prompt_summary === "Provider failure")!;
    expect(failed).toBeTruthy();
    expect(failed.status).toBe("failed");
    expect(failed.termination_reason).toBe("api_error");
    expect(failed.record.started_at).toBeGreaterThan(0);
    expect(failureEvents.some((event) => event.type === "subagent.start" && event.subagent_id === failed.subagent_id)).toBe(true);
    expect(provider.requests.filter((request) => request.route === "provider-failure")).toHaveLength(1);
    expect(provider.requests.some((request) => request.route === "unexpected-child")).toBe(false);
    expect(failed.result.status).toBe("failed");
    expect(failed.result.terminal_reason).toBe("api_error");
    expect(failed.result.error).toContain("HTTP 400");
    expect(failed.result.error).toContain("CONTROLLED_PROVIDER_ERROR");
    const providerFailure = failed.transcript_snapshot.messages.findLast((message: Fact) => message.role === "assistant")!;
    expect(providerFailure).toMatchObject({ is_streaming: false, terminal_status: "failed", termination_reason: "api_error", failure_recoverable: true });
    expect(providerFailure.content).toBe("");
    expect(providerFailure.failure_message).toContain("HTTP 400");
    expect(providerFailure.failure_message).toContain("CONTROLLED_PROVIDER_ERROR");
    const failedAgent = (await readAgents(page)).find((agent) => agent.id === failed.subagent_id)!;
    await openAgentRecord(page, failedAgent.displayName || failedAgent.objective);
    const failureNotice = page.locator(".subagents-detail .subagents-transcript .error-cell-notice");
    await expect(failureNotice).toBeVisible();
    await expect(failureNotice).toContainText(providerFailure.failure_message);
    await expect(page.locator(".subagents-detail [role=alert]")).toHaveCount(1);
    await capture(app, page, "05-real-provider-failure");
    verdicts.push({ scenario: "real HTTP 400 from local Provider", passed: true, terminal: failed });
    const beforeRestart = await readAgents(page);
    const conversationId = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await app.close();
    app = undefined;
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBe(conversationId);
    await expect.poll(async () => (await readAgents(page)).length, { timeout: 30_000 }).toBe(4);
    const afterRestart = await readAgents(page);
    const facts = (agents: Fact[]) => agents.map(({ id, status, agentPath, mailboxEpoch, displayName, startedAt, completedAt, model }) => ({ id, status, agentPath, mailboxEpoch, displayName, startedAt, completedAt, model })).sort((a, b) => a.id.localeCompare(b.id));
    expect(facts(afterRestart)).toEqual(facts(beforeRestart));
    const restoredSummary = page.locator("#thread-summary-panel");
    if (!await restoredSummary.isVisible()) await page.getByRole("button", { name: "切换摘要", exact: true }).click();
    await expect(restoredSummary).toBeVisible();
    await restoredSummary.getByRole("button", { name: "子智能体", exact: true }).click();
    await expect(page.locator("#right-panel-subagents")).toBeVisible();
    await expect(page.locator(".subagents-tab")).toBeVisible();
    await expect(page.locator(".subagents-row").first()).toBeVisible();
    await expect(page.locator(".subagents-tab")).toContainText("已开启 · 0");
    await capture(app, page, "06-real-restored-terminal-list");
    await openAgentRecord(page, failedAgent.displayName || failedAgent.objective);
    const restoredFailureNotice = page.locator(".subagents-detail .subagents-transcript .error-cell-notice");
    await expect(restoredFailureNotice).toHaveCount(1, { timeout: 30_000 });
    await expect(restoredFailureNotice).toContainText(providerFailure.failure_message);
    await expect(page.locator(".subagents-detail [role=alert]")).toHaveCount(1);
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    const restoredTranscriptStart = incoming.length;
    await page.getByRole("button", { name: `打开子智能体任务：${workerA.displayName || workerA.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toContainText("CONTROLLED_FOLLOWUP_FINAL", { timeout: 30_000 });
    await expect(page.locator(".subagents-detail")).toBeVisible();
    await expect(page.locator(".subagents-detail textarea, .subagents-detail [role=textbox], .subagents-detail .user-cell-wrap")).toHaveCount(0);
    await expect.poll(() => incoming.slice(restoredTranscriptStart).some(event => event.type === "command.result"
      && event.command === "subagent.transcript" && event.data.subagent_id === workerA.id)).toBe(true);
    const restoredTranscript = incoming.slice(restoredTranscriptStart).findLast(event => event.type === "command.result"
      && event.command === "subagent.transcript" && event.data.subagent_id === workerA.id)!;
    expect(restoredTranscript.level).toBe("info");
    const restoredReports = restoredTranscript.data.messages.flatMap((message: Fact) => message.blocks ?? [])
      .filter((block: Fact) => block.type === "tool_call" && block.record.name === "send_message")
      .map((block: Fact) => ({ id: block.record.id, recipientId: block.record.recipientId, target: block.record.args.target, message: block.record.args.message }));
    expect(restoredReports).toEqual(reports.map((record: Fact) => ({ id: record.id, recipientId: record.recipientId, target: record.args.target, message: record.args.message })));
    const restoredProcess = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(restoredProcess).toHaveCount(2);
    for (let turn = 0; turn < 2; turn++) {
      const disclosure = restoredProcess.nth(turn).locator("button[aria-expanded]").first();
      if (await disclosure.getAttribute("aria-expanded") === "false") await disclosure.click();
    }
    await expect(page.locator(".subagents-detail").getByText("已向父代理发送消息", { exact: true })).toHaveCount(2);
    await capture(app, page, "07-real-restored-child-detail");
    verdicts.push({ scenario: "desktop/backend restart with durable journal and UI state", passed: true, before: beforeRestart, after: afterRestart });
    expect(provider.failures).toEqual([]);
    // A real project was registered by the workspace lifecycle. Open its
    // ordinary new-chat action; do not seed recent projects for screenshots.
    await expect.poll(() => page.evaluate((root) => (window as any).__zustandStore.getState().recentWorkspaces.some((project: any) => project.path.replaceAll("\\", "/").toLowerCase() === root.replaceAll("\\", "/").toLowerCase()), workspace), { timeout: 30_000 }).toBe(true);
    await page.getByRole("button", { name: "在 workspace 中新建任务", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).not.toBe(conversationId);
    await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "选择项目", exact: true }).click();
    const projectPicker = page.getByRole("dialog", { name: "选择项目", exact: true });
    await expect(projectPicker).toBeVisible();
    await expect(projectPicker.getByRole("button", { name: "workspace", exact: true })).toHaveAttribute("aria-pressed", "true");
    await capture(app, page, "08-real-project-new-chat-picker");
    verdicts.push({ scenario: "registered real project in new-chat picker", passed: true, workspace });
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidenceRoot, "controlled-evidence.json"), JSON.stringify({ environment: "CONTROLLED V1: deterministic loopback Provider, production frontend, real MiniCode desktop/backend/harness; no external Provider or V2 claim", testRoot, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures, outgoingCommands: outgoing, incomingEvents: incoming }, null, 2));
  }
});

test("CONTROLLED V2: real waiting survives renderer reload and desktop restart without wait rows", async () => {
  test.setTimeout(180_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-controlled-wait-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-controlled-wait-"));
  const workspace = path.join(fixtureRoot, "workspace");
  const evidence = path.join(evidenceRoot, "v2-wait");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(workspace, "fact-a.txt"), "CONTROLLED_FACT_A=alpha\n");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent_v2: { enabled: true, min_wait_timeout_ms: 0,
      default_wait_timeout_ms: 100, max_wait_timeout_ms: 30000 } } }));
  const provider = new ControlledProvider("v2"), incoming: Fact[] = [], outgoing: Fact[] = [], verdicts: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const waitRecords = (page: Page) => page.evaluate(() => (window as any).__zustandStore.getState().messages
    .flatMap((message: Fact) => message.blocks ?? []).filter((block: Fact) => block.type === "tool_call" && block.record.name === "wait_agent")
    .map((block: Fact) => block.record));
  const assertNoWaitRows = async (page: Page) => {
    for (const toggle of await page.getByRole("region", { name: "Agent 处理进度", exact: true })
      .locator("button[aria-expanded]").all()) {
      if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
    }
    await expect(page.getByText("Wait for agents", { exact: true })).toHaveCount(0);
    await expect(page.getByText('"timed_out"', { exact: false })).toHaveCount(0);
  };
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    expect(new URL(page.url()).protocol).toBe("file:");
    expect(await app.evaluate(({ app }) => app.getPath("userData"))).toBe(profile);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_WAIT_PARENT: Wait for the actual child to read its file and return the result.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has("wait-child"), { timeout: 45_000 }).toBe(true);
    await expect.poll(async () => (await waitRecords(page)).length, { timeout: 30_000 }).toBe(5);
    await expect.poll(async () => (await waitRecords(page)).at(-1)?.status).toBe("running");
    expect((await readAgents(page)).filter(agent => agent.status === "running")).toHaveLength(1);
    await expect(page.locator(".collaboration-delegated-start")).toHaveCount(1);
    await assertNoWaitRows(page);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    const beforeReload = await waitRecords(page);
    expect(beforeReload.slice(0, 4).every((record: Fact) => record.status === "success")).toBe(true);
    // Electron resumes its own unload after committing drafts; CDP must not dismiss that native intent.
    const observeUnload = (dialog: Dialog) => { expect(dialog.type()).toBe("beforeunload"); };
    page.once("dialog", observeUnload);
    await page.reload();
    page.off("dialog", observeUnload);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect.poll(async () => (await waitRecords(page)).length).toBe(5);
    await expect.poll(async () => (await waitRecords(page)).at(-1)?.status).toBe("running");
    await assertNoWaitRows(page);
    expect(provider.counts.get("wait-parent")).toBe(6);
    verdicts.push({ scenario: "five real waits, no rows, same running turn after renderer reload", passed: true });
    provider.releaseChildren();
    await expect(page.getByText("CONTROLLED_WAIT_PARENT_FINAL", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    const finished = await waitRecords(page);
    expect(finished).toHaveLength(5);
    expect(finished.every((record: Fact) => record.status === "success")).toBe(true);
    const parentRequest = provider.requests.findLast(request => request.route === "wait-parent")!;
    const receipts = parentRequest.toolReceipts.filter((receipt: Fact) => receipt.id?.startsWith("controlled-wait-parent-")
      && receipt.id !== "controlled-wait-parent-0").map((receipt: Fact) => {
        const wrapped = /^<function_call_result[^>]*>\n([\s\S]*?)\n\[wall_time:[^\n]*\]\n<\/function_call_result>$/.exec(receipt.content);
        expect(wrapped, "Actual chat tool receipts retain their canonical function_call_result envelope").toBeTruthy();
        return JSON.parse(wrapped![1]);
      });
    expect(receipts).toHaveLength(5);
    expect(receipts.some((receipt: Fact) => receipt.timed_out === true)).toBe(true);
    expect(receipts.at(-1)).toMatchObject({ message: "Wait completed.", timed_out: false });
    const [child] = await readAgents(page);
    expect(child.status).toBe("done");
    await assertNoWaitRows(page);
    await page.locator(".collaboration-delegated").getByRole("button", { name: `打开子智能体：${child.displayName || child.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toContainText("CONTROLLED_WAIT_CHILD_FINAL: CONTROLLED_FACT_A=alpha");
    await assertNoWaitRows(page);
    await capture(app, page, "09-v2-real-wait-child-result");
    verdicts.push({ scenario: "real timeout and completion receipts consumed by provider, child file result visible", passed: true });
    await app.close();
    app = undefined;
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByText("CONTROLLED_WAIT_PARENT_FINAL", { exact: true })).toBeVisible();
    await expect.poll(async () => (await waitRecords(page)).length).toBe(5);
    expect((await waitRecords(page)).map((record: Fact) => ({ id: record.id, status: record.status })))
      .toEqual(finished.map((record: Fact) => ({ id: record.id, status: record.status })));
    expect(provider.counts.get("wait-parent")).toBe(7);
    await assertNoWaitRows(page);
    expect(provider.failures).toEqual([]);
    await capture(app, page, "10-v2-restored-no-wait-rows");
    verdicts.push({ scenario: "desktop/backend cold restart preserves all wait receipts without re-sampling or rows", passed: true });
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({
      environment: "CONTROLLED V2: loopback Provider, real production desktop/backend/harness, no seeded message/agent state",
      testRoot, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures,
      incomingEvents: incoming, outgoingCommands: outgoing,
    }, null, 2));
  }
});

test("CONTROLLED V2: real wait error remains readable after desktop restart", async () => {
  test.setTimeout(180_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-controlled-wait-error-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-controlled-wait-error-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "v2-wait-error");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent_v2: { enabled: true, min_wait_timeout_ms: 0,
      default_wait_timeout_ms: 100, max_wait_timeout_ms: 30000 } } }));
  const provider = new ControlledProvider("v2-error"), incoming: Fact[] = [], outgoing: Fact[] = [], verdicts: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const waitRecord = (page: Page) => page.evaluate(() => (window as any).__zustandStore.getState().messages
    .flatMap((message: Fact) => message.blocks ?? []).find((block: Fact) => block.type === "tool_call" && block.record.name === "wait_agent")?.record);
  const showError = async (page: Page) => {
    const process = page.getByRole("region", { name: "Agent 处理进度", exact: true });
    const processToggle = page.getByRole("button", { name: "展开处理步骤" });
    if (await processToggle.isVisible()) await processToggle.click();
    await expect(process).toBeVisible();
    const activity = process.locator(".activity-cell").filter({ hasText: "Wait for agents" });
    await expect(activity).toHaveCount(1);
    const toggle = activity.getByRole("button", { name: "展开活动详情" });
    if (await toggle.isVisible()) await toggle.click();
    await expect(activity).toContainText("timeout_ms must be at most 30000");
  };
  try {
    app = await launch(profile, providerUrl);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    expect(await app.evaluate(({ app }) => app.getPath("userData"))).toBe(profile);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    await sendPrompt(page, "CONTROLLED_WAIT_ERROR_PARENT: Test the wait limit and report the actual result.", "CONTROLLED_WAIT_ERROR_FINAL");
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    const original = await waitRecord(page);
    expect(original).toMatchObject({ name: "wait_agent", status: "failed" });
    expect(JSON.stringify(original)).toContain("timeout_ms must be at most 30000");
    expect(incoming.some(event => event.type === "tool_result" && event.id === original.id && event.status === "failed")).toBe(true);
    await showError(page);
    await capture(app, page, "11-v2-wait-error-live");
    verdicts.push({ scenario: "real wait error visible in expandable process", passed: true });
    await app.close();
    app = await launch(profile, providerUrl);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByText("CONTROLLED_WAIT_ERROR_FINAL", { exact: true })).toBeVisible();
    await expect.poll(async () => (await waitRecord(page))?.status).toBe("failed");
    expect(JSON.stringify(await waitRecord(page))).toContain("timeout_ms must be at most 30000");
    await showError(page);
    await capture(app, page, "12-v2-wait-error-restored");
    expect(provider.counts.get("wait-error-parent")).toBe(2);
    expect(provider.failures).toEqual([]);
    verdicts.push({ scenario: "real wait error survives desktop/backend restart", passed: true });
  } finally {
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({
      environment: "CONTROLLED V2: real production desktop/backend/harness, loopback Provider",
      testRoot, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures,
      incomingEvents: incoming, outgoingCommands: outgoing,
    }, null, 2));
  }
});

test("CONTROLLED: queued follow-ups survive regeneration and desktop restart", async () => {
  test.setTimeout(180_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-controlled-regeneration-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-controlled-regeneration-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "queue-regeneration");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("queue-regen"), incoming: Fact[] = [], outgoing: Fact[] = [], verdicts: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const queued = (page: Page) => page.evaluate(() => (window as any).__zustandStore.getState().messages
    .filter((message: Fact) => message.role === "user" && message.queueState === "queued")
    .sort((left: Fact, right: Fact) => left.queuePosition - right.queuePosition)
    .map((message: Fact) => ({ id: message.id, messageId: message.queueMessageId,
      content: message.content, position: message.queuePosition, paused: message.queuePaused,
      attachments: (message.attachmentRefs ?? []).map((item: Fact) => ({ id: item.id, name: item.name,
        kind: item.kind, mediaType: item.mediaType, artifactId: item.artifactId })) })));
  const expectedContents = ["CONTROLLED_QUEUE_FOLLOWUP_ONE", "CONTROLLED_QUEUE_FOLLOWUP_TWO"];
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    expect(await app.evaluate(({ app }) => app.getPath("userData"))).toBe(profile);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "chat" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    await sendPrompt(page, "CONTROLLED_QUEUE_FIRST: Produce the first answer.", "CONTROLLED_QUEUE_FIRST_FINAL");
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_QUEUE_BLOCK: Hold this turn while two follow-ups arrive.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has("queue-block"), { timeout: 45_000 }).toBe(true);
    for (const content of expectedContents) {
      if (content === expectedContents[0]) {
        await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
        const chooser = page.waitForEvent("filechooser");
        await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
        await (await chooser).setFiles(path.join(repoRoot, "docs/assets/screenshot-chat.png"));
        await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
      }
      await composer.fill(content);
      await composer.press("Enter");
      await expect.poll(async () => (await queued(page)).some((item: Fact) => item.content === content)).toBe(true);
    }
    expect((await queued(page)).map((item: Fact) => item.content)).toEqual(expectedContents);
    expect((await queued(page))[0].attachments).toHaveLength(1);
    await expect.poll(() => page.locator(".queued-message-row img").first().evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    await composer.fill("CONTROLLED_UNSENT_DRAFT");
    await page.getByRole("button", { name: "停止当前回复" }).last().click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 60_000 }).toBe(false);
    await expect.poll(async () => (await queued(page)).every((item: Fact) => item.paused === true)).toBe(true);
    expect((await queued(page)).map((item: Fact) => item.content)).toEqual(expectedContents);
    const before = await queued(page);
    provider.releaseChildren();
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await page.locator(".assistant-cell-wrap").filter({ hasText: "CONTROLLED_QUEUE_FIRST_FINAL" }).hover();
    await page.getByRole("button", { name: "重新生成", exact: true }).first().click();
    await page.getByRole("dialog").getByRole("button", { name: "重新生成", exact: true }).click();
    await expect(page.getByText("CONTROLLED_QUEUE_REGENERATED", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    expect(await queued(page)).toEqual(before);
    await expect(composer).toHaveText("CONTROLLED_UNSENT_DRAFT");
    await expect.poll(() => page.locator(".queued-message-row img").first().evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    expect(provider.requests.filter(request => request.route !== "auxiliary").map(request => request.route))
      .toEqual(["queue-first", "queue-block", "queue-first"]);
    verdicts.push({ scenario: "accepted follow-ups survive regeneration with FIFO and paused state", passed: true, queue: await queued(page) });
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect.poll(async () => (await queued(page)).map((item: Fact) => item.content)).toEqual(expectedContents);
    expect(await queued(page)).toEqual(before);
    await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toHaveText("CONTROLLED_UNSENT_DRAFT");
    await expect(page.locator(".queued-message-row")).toHaveCount(2);
    await expect.poll(() => page.locator(".queued-message-row img").first().evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    expect(provider.failures).toEqual([]);
    verdicts.push({ scenario: "desktop/backend restart preserves queued follow-ups", passed: true, queue: await queued(page) });
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({
      environment: "CONTROLLED: real production desktop/backend/harness, loopback Provider",
      testRoot, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures,
      incomingEvents: incoming, outgoingCommands: outgoing,
    }, null, 2));
  }
});

test("CONTROLLED: Escape closes a narrow queued attachment preview without interrupting the active turn", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-preview-escape-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-preview-escape-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "preview-escape");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("queue-regen"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const observe = (page: Page) => page.evaluate(() => {
    const state = (window as any).__zustandStore.getState();
    const active = document.activeElement as HTMLElement | null;
    const drawer = document.querySelector<HTMLElement>('[role="dialog"][aria-label="右侧面板"]');
    return { width: innerWidth, streaming: state.isStreaming, compactPanel: state.compactPanel,
      activeRole: active?.getAttribute("role"), activeLabel: active?.getAttribute("aria-label"), activeClass: active?.className,
      drawerVisible: Boolean(drawer && !drawer.closest("[hidden]") && getComputedStyle(drawer).visibility !== "hidden") };
  });
  try {
    app = await launch(profile, providerUrl, true);
    const page = await app.firstWindow();
    await isolateQaWindow(app);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1600, 900));
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_QUEUE_BLOCK: Hold the actual turn while its attachment preview opens.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has("queue-block"), { timeout: 45_000 }).toBe(true);
    await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
    await (await chooser).setFiles(path.join(repoRoot, "docs/assets/screenshot-chat.png"));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
    await composer.fill("Queued attachment must survive preview");
    await composer.press("Enter");
    await composer.fill("Unsent draft must survive preview");
    const preview = page.locator(".queued-message-row").getByRole("button", { name: "预览 screenshot-chat.png" });
    await expect(preview).toBeVisible();
    await preview.click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().rightStackTab)).toBe("preview");
    observations.push({ phase: "wide-preview", ...await observe(page) });
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1000, 800));
    await expect.poll(() => page.evaluate(() => innerWidth)).toBeLessThanOrEqual(1023);
    observations.push({ phase: "narrow-before-escape", ...await observe(page) });
    await page.keyboard.press("Escape");
    observations.push({ phase: "after-escape", ...await observe(page) });
    await page.waitForTimeout(300);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(true);
    await expect(page.getByRole("dialog", { name: "右侧面板" })).toHaveCount(0);
    expect(outgoing.filter(command => command.type === "interrupt")).toEqual([]);
    await expect(page.locator(".queued-message-row img")).toHaveCount(1);
    await expect(composer).toHaveText("Unsent draft must survive preview");
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, fixtureFailures: provider.failures, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: first draft failure restores its error, capabilities and unsent next input", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-first-denial-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-first-denial-"));
  const workspace = path.join(fixtureRoot, "workspace");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("root-provider-error"), incoming: Fact[] = [], outgoing: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_ROOT_PROVIDER_ERROR: First draft provider denial.");
    await composer.press("Enter");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().messages
      .some((message: Fact) => message.role === "assistant" && message.terminalStatus === "failed")), { timeout: 60_000 }).toBe(true);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    expect(conversation).toBeTruthy();
    expect(outgoing.find(command => command.type === "conversation.switch" && command.conversation_id === conversation))
      .toMatchObject({ defer_runtime_capabilities: true });
    await expect.poll(() => page.evaluate(() => Array.isArray((window as any).__zustandStore.getState().runtimeCapabilities?.tools)), { timeout: 30_000 }).toBe(true);
    const expand = page.getByRole("button", { name: "展开处理步骤" });
    if (await expand.isVisible()) await expand.click();
    await expect(page.locator(".error-cell-notice")).toHaveCount(1);
    await expect(page.locator(".error-cell-notice")).toContainText("CONTROLLED_GATEWAY_POLICY_REASON");
    await composer.fill("保留尚未发送的下一条输入");
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toContainText("保留尚未发送的下一条输入");
    const restoredExpand = page.getByRole("button", { name: "展开处理步骤" });
    if (await restoredExpand.isVisible()) await restoredExpand.click();
    await expect(page.locator(".error-cell-notice")).toHaveCount(1);
    await expect(page.locator(".error-cell-notice")).toContainText("CONTROLLED_GATEWAY_POLICY_REASON");
  } finally {
    if (app) await app.close();
    await provider.close();
  }
});

test("CONTROLLED: root provider denial retains the real reason through restart", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-provider-denial-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-provider-denial-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "provider-denial");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("root-provider-error"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const messages = (page: Page) => page.evaluate(() => (window as any).__zustandStore.getState().messages);
  const showReason = async (page: Page) => {
    const processToggle = page.getByRole("button", { name: "展开处理步骤" });
    if (await processToggle.isVisible()) await processToggle.click();
    const process = page.getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(process).toBeVisible();
    for (const toggle of await process.locator("button[aria-expanded=false]").all()) await toggle.click();
    await expect(process).toContainText("CONTROLLED_GATEWAY_POLICY_REASON: project policy disallows this model");
  };
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_ROOT_PROVIDER_ERROR: Preserve the gateway denial reason.");
    await composer.press("Enter");
    await expect.poll(() => provider.counts.get("root-provider-error"), { timeout: 45_000 }).toBe(1);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 60_000 }).toBe(false);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    observations.push({ phase: "terminal", messages: await messages(page) });
    await expect(page.getByText("CONTROLLED_ROOT_PROVIDER_ERROR: Preserve the gateway denial reason.")).toBeVisible();
    await showReason(page);
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    observations.push({ phase: "restored", messages: await messages(page) });
    await showReason(page);
  } finally {
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: root provider retry retains the real reason through restart", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-provider-retry-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-provider-retry-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "provider-retry");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("root-provider-retry"), incoming: Fact[] = [], outgoing: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const showReason = async (page: Page) => {
    const toggle = page.getByRole("button", { name: "展开处理步骤" });
    if (await toggle.isVisible()) await toggle.click();
    const process = page.getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(process).toBeVisible();
    for (const disclosure of await process.locator("button[aria-expanded=false]").all()) await disclosure.click();
    await expect(process).toContainText("CONTROLLED_RATE_LIMIT_REASON: regional quota is exhausted");
    await expect(process).toContainText("CONTROLLED_RETRY_GATEWAY_REASON: upstream queue was reset");
  };
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    await sendPrompt(page, "CONTROLLED_ROOT_PROVIDER_RETRY: Preserve the real retry reason.", "CONTROLLED_ROOT_PROVIDER_RETRY_FINAL");
    expect(provider.counts.get("root-provider-retry")).toBe(3);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await showReason(page);
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByText("CONTROLLED_ROOT_PROVIDER_RETRY_FINAL", { exact: true })).toBeVisible();
    await showReason(page);
  } finally {
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot,
      providerRequests: provider.requests, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: uploaded Markdown opens its real rendered attachment preview", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-document-preview-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-document-preview-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "document-preview");
  const name = "controlled-report.md", heading = "受控文档预览", paragraph = "这段内容来自真实上传的 Markdown 附件。";
  const markdown = `# ${heading}\n\n${paragraph}\n\n## 详细内容\n\n- 保留原始文本和附件归属\n- 验证源码与渲染切换\n`;
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(fixtureRoot, name), markdown, "utf8");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("queue-regen"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  try {
    app = await launch(profile, providerUrl, true);
    const page = await app.firstWindow();
    await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      window.webContents.setBackgroundThrottling(false);
      window.setContentSize(2048, 1104);
    });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
    await (await chooser).setFiles(path.join(fixtureRoot, name));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
    await sendPrompt(page, "CONTROLLED_QUEUE_FIRST: Review the attached report.", "CONTROLLED_QUEUE_FIRST_FINAL");
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(paragraph, { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "查看源代码", exact: true })).toBeVisible();
    observations.push({ phase: "rendered", preview: await page.evaluate(() => (window as any).__zustandStore.getState().previewArtifact) });
    await page.screenshot({ path: path.join(evidence, "rendered-attachment.png") });
    await page.getByRole("button", { name: "查看源代码", exact: true }).click();
    await expect(page.locator("[data-preview-mode=artifact] pre")).toContainText(`# ${heading}`);
    await page.getByRole("button", { name: "查看预览", exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    expect(provider.failures).toEqual([]);
  } finally {
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: workspace Markdown stays usable in the narrow drawer without saving the editor", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-document-preview-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-document-preview-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "workspace-markdown-narrow");
  const name = "controlled-report.md", heading = "受控文档预览", paragraph = "这段内容来自真实上传的 Markdown 附件。";
  const markdown = `# ${heading}\n\n${paragraph}\n\n## 详细内容\n\n- 保留原始文本和附件归属\n- 验证源码与渲染切换\n`;
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(fixtureRoot, name), markdown, "utf8");
  await writeFile(path.join(workspace, "controlled-workspace.md"), markdown, "utf8");
  await writeFile(path.join(workspace, "draft-source.ts"), "export const base = 42;\n", "utf8");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("queue-regen"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  try {
    app = await launch(profile, providerUrl, true);
    const page = await app.firstWindow();
    await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      window.webContents.setBackgroundThrottling(false);
      window.setContentSize(1638, 889);
      window.webContents.setZoomFactor(1.1);
    });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
    }, workspace);
    await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
    await (await chooser).setFiles(path.join(fixtureRoot, name));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
    await sendPrompt(page, "CONTROLLED_QUEUE_FIRST: Review the attached report and include the workspace reference.", "CONTROLLED_QUEUE_FIRST_FINAL");
    const editorPath = path.join(workspace, "draft-source.ts");
    await page.evaluate(file => (window as any).__zustandStore.getState().openEditorFile(file), editorPath);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().editorTabs.find((tab: any) => tab.path.endsWith("draft-source.ts"))?.content), { timeout: 30000 }).toContain("export const base = 42;");
    await page.locator(".monaco-editor").first().click({ position: { x: 100, y: 25 } });
    await page.keyboard.press("Control+End");
    await page.keyboard.type("UNSAVED_EDITOR_CHANGE");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().editorTabs.find((tab: any) => tab.path.endsWith("draft-source.ts"))?.content)).toContain("UNSAVED_EDITOR_CHANGE");
    await page.evaluate(() => (window as any).__zustandStore.getState().setAppMode("cowork"));
    const left = page.getByRole("separator", { name: "调整左侧栏宽度" });
    await left.focus();
    for (let i = 0; i < 10; i++) await left.press("ArrowLeft");
    const fileLink = page.locator(".md-file-chip").filter({ hasText: "工作区文件" });
    await expect(fileLink).toBeVisible();
    await fileLink.click();
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "查看源代码", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "复制文件内容", exact: true })).toBeVisible();
    const resize = page.getByRole("separator", { name: "调整右侧栏宽度" });
    await resize.focus();
    await resize.press("Home");
    for (let i = 0; i < 7; i++) await resize.press("ArrowLeft");
    await page.getByRole("button", { name: "查看源代码", exact: true }).click();
    await expect(page.getByText(`# ${heading}`, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "查看预览", exact: true }).click();
    await page.getByRole("button", { name: "切换文件树", exact: true }).click();
    await expect(page.getByRole("treeitem", { name: /controlled-workspace.md/ })).toBeVisible();
    await page.getByRole("button", { name: "切换文件树", exact: true }).click();
    await page.getByRole("button", { name: "打开选项菜单", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "另存为…" })).toBeVisible();
    await page.keyboard.press("Escape");
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(440, 850));
    await expect.poll(() => page.evaluate(() => innerWidth)).toBeLessThanOrEqual(420);
    await fileLink.click();
    await expect(page.getByRole("dialog", { name: "右侧面板" })).toBeVisible();
    await expect(page.locator(".mc-file-viewer-header")).toBeVisible();
    const boxes = await page.evaluate(() => {
      const header = document.querySelector(".mc-file-viewer-header")!.getBoundingClientRect();
      const controls = [...document.querySelectorAll(".mc-file-viewer-header button")].map(element => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      });
      return { header: { left: header.left, right: header.right }, controls };
    });
    expect(boxes.header.right).toBeGreaterThan(boxes.header.left);
    for (const control of boxes.controls) {
      expect(control.right).toBeGreaterThan(control.left);
      expect(control.left).toBeGreaterThanOrEqual(boxes.header.left);
      expect(control.right).toBeLessThanOrEqual(boxes.header.right);
    }
    const tab = await page.evaluate(() => (window as any).__zustandStore.getState().editorTabs.find((item: any) => item.path.endsWith("draft-source.ts")));
    expect(tab.content).toContain("UNSAVED_EDITOR_CHANGE");
    expect(tab.original).not.toContain("UNSAVED_EDITOR_CHANGE");
    expect(await (await import("node:fs/promises")).readFile(editorPath, "utf8")).toBe("export const base = 42;\n");
  } finally {
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: question response keeps exact whitespace and the unsent draft", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-question-input-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-question-input-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "question-input");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 },
    features: { memories: false, multi_agent: true } }));
  const provider = new ControlledProvider("question-input"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  const answer = "  exact first line\n    indented second line  ";
  let app: ElectronApplication | undefined;
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      store.getState().setPermissionMode("bypass");
      store.getState().setAgentMode("plan");
    }, workspace);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_QUESTION_PARENT: Ask a structured question.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has("question-parent"), { timeout: 45_000 }).toBe(true);
    await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
    await (await chooser).setFiles(path.join(repoRoot, "docs/assets/screenshot-chat.png"));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
    await composer.fill("UNSENT_QUESTION_DRAFT");
    const draftAttachment = await page.evaluate(() => (window as any).__zustandStore.getState().attachments[0].artifactId);
    expect(draftAttachment).toBeTruthy();
    provider.releaseChildren();
    await expect(page.getByText("How should the exact input be retained?", { exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(composer).toBeHidden();
    const input = page.getByRole("textbox", { name: "回答当前问题" });
    await input.fill(answer);
    await input.press("Enter");
    await expect(page.getByText("CONTROLLED_QUESTION_FINAL", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    expect(outgoing.find(command => command.type === "control_response")?.response?.response?.answers?.detail?.answers).toEqual([answer]);
    const followup = provider.requests.find(request => request.route === "question-parent" && request.index === 1)!;
    expect(JSON.parse(String(followup.toolReceipts.find((item: Fact) => item.id === "controlled-question-parent-0")?.content)).answers.detail.answers).toEqual([answer]);
    await expect(composer).toHaveText("UNSENT_QUESTION_DRAFT");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.artifactId)).toBe(draftAttachment);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    observations.push({ phase: "submitted", answers: [answer], draftAttachment });
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toHaveText("UNSENT_QUESTION_DRAFT");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.artifactId)).toBe(draftAttachment);
    const restored = await page.evaluate(() => (window as any).__zustandStore.getState().messages);
    const restoredRecord = restored.flatMap((message: Fact) => message.blocks ?? [])
      .find((block: Fact) => block.type === "tool_call" && block.record.name === "request_user_input")?.record;
    expect(restoredRecord?.status).toBe("success");
    expect(JSON.parse(restoredRecord.summary).answers.detail.answers).toEqual([answer]);
    observations.push({ phase: "restored", draftAttachment });
    expect(provider.failures).toEqual([]);
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, fixtureFailures: provider.failures, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});

test("CONTROLLED: native command approval timeout retains its diagnostic and draft through restart", async () => {
  test.setTimeout(120_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-approval-timeout-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-approval-timeout-"));
  const workspace = path.join(fixtureRoot, "workspace"), evidence = path.join(evidenceRoot, "approval-timeout");
  await mkdir(data, { recursive: true });
  await mkdir(path.join(profile, "config"), { recursive: true });
  await mkdir(workspace, { recursive: true });
  await mkdir(evidence, { recursive: true });
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never",
    turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ approval_policy: "untrusted", agent: { code_mode_only: false, max_iterations: 12, approval_timeout_seconds: 4 },
    features: { memories: false, multi_agent: true, guardian_approval: false } }));
  const provider = new ControlledProvider("approval-timeout"), incoming: Fact[] = [], outgoing: Fact[] = [], observations: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const showDiagnostic = async (page: Page) => {
    const toggle = page.getByRole("button", { name: "展开处理步骤" });
    if (await toggle.isVisible()) await toggle.click();
    const process = page.getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(process).toBeVisible();
    for (const disclosure of await process.locator("button[aria-expanded=false]").all()) await disclosure.click();
    await expect(process).toContainText("approval timed out after 4 seconds");
    await expect(process.locator(".exec-cell-output-stdout")).toHaveCount(0);
    await expect(process.locator(".exec-cell-output-status")).toHaveCount(0);
  };
  try {
    app = await launch(profile, providerUrl, true);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await page.evaluate(async root => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      await store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
      if (!await store.getState().setPermissionMode("confirm")) throw new Error("Fixture permission mode was not accepted");
    }, workspace);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("CONTROLLED_APPROVAL_PARENT: Ask before running the exact command.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.has("approval-parent"), { timeout: 45_000 }).toBe(true);
    await page.locator(".composer-container:visible").getByRole("button", { name: "添加文件等内容" }).click();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("menuitem", { name: "文件和文件夹" }).click();
    await (await chooser).setFiles(path.join(repoRoot, "docs/assets/screenshot-chat.png"));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status), { timeout: 20_000 }).toBe("ready");
    await composer.fill("UNSENT_APPROVAL_DRAFT");
    const draftAttachment = await page.evaluate(() => (window as any).__zustandStore.getState().attachments[0].artifactId);
    expect(draftAttachment).toBeTruthy();
    provider.releaseChildren();
    await expect(page.getByRole("button", { name: "允许使用工具", exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("CONTROLLED_APPROVAL_FINAL", { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    const result = incoming.find(event => event.type === "tool_result" && event.id === "controlled-approval-parent-0");
    expect(result).toMatchObject({ status: "blocked", error_kind: "approval_rejected" });
    expect(result.summary).toContain("approval timed out after 4 seconds");
    expect(result.execution_started_at).toBeUndefined();
    expect(existsSync(path.join(workspace, "approval-must-not-run.txt"))).toBe(false);
    await showDiagnostic(page);
    await expect(composer).toHaveText("UNSENT_APPROVAL_DRAFT");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.artifactId)).toBe(draftAttachment);
    const conversation = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    observations.push({ phase: "timed-out", result, draftAttachment });
    await app.close();
    app = await launch(profile, providerUrl, true);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(conversation);
    await expect(page.getByText("CONTROLLED_APPROVAL_FINAL", { exact: true })).toBeVisible();
    await showDiagnostic(page);
    await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toHaveText("UNSENT_APPROVAL_DRAFT");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.artifactId)).toBe(draftAttachment);
    observations.push({ phase: "restored", draftAttachment });
    expect(provider.failures).toEqual([]);
  } finally {
    provider.releaseChildren();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidence, "controlled-evidence.json"), JSON.stringify({ testRoot, observations,
      providerRequests: provider.requests, fixtureFailures: provider.failures, incomingEvents: incoming, outgoingCommands: outgoing }, null, 2));
  }
});
