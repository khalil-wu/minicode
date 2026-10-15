/** Controlled loopback Provider; production Electron, WebSocket and harness.
 * No store fixtures and no simulated backend/lifecycle events are used here.
 */
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
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
      .findLast((text) => /CONTROLLED_(FOLLOWUP_A|WORKER_A|WORKER_B|PROVIDER_FAILURE|PREFLIGHT_REJECT|FAILURE_PARENT|MAIN|SHOULD_NOT_RUN)/.test(text)) || "";
    const route = !body.tools?.length ? "auxiliary"
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
      roles: body.messages.map((message) => message.role), tools: body.tools?.map((tool) => tool.function.name) || [] });
    if (route === "provider-failure" || route === "unexpected-child") {
      response.writeHead(400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: "CONTROLLED_PROVIDER_ERROR", type: "invalid_request_error", code: "controlled_fixture" } }));
      return;
    }
    if ((route === "worker-a" || route === "worker-b") && index === 0) {
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
    if (route === "parent" && index === 0) {
      content = "受控环境：让两个子智能体实际读取文件并向父代理报告。";
      call("task", { parallel_tasks: [
        { description: "Audit A", prompt: "CONTROLLED_WORKER_A: Read fact-a.txt, report to parent, then finish.", agent_type: "general-purpose", read_only: true },
        { description: "Audit B", prompt: "CONTROLLED_WORKER_B: Read fact-b.txt, report to parent, then finish.", agent_type: "general-purpose", read_only: true },
      ] });
    } else if (route === "parent" && index === 1) {
      const toolOutput = body.messages.filter((message) => message.role === "tool").map((message) => String(message.content)).join("\n");
      const target = /subagent_id:\s*(subagent[-_][\w-]+)/.exec(toolOutput)?.[1];
      if (!target) contractFailure = "Real TaskTool result did not expose an actual recipient id";
      call("send_message", { recipient: target, message: "CONTROLLED_FOLLOWUP_A: Read fact-a.txt once more and report the new final result." });
    } else if (route === "worker-a" || route === "worker-b" || route === "followup") {
      const letter = route === "worker-b" ? "b" : "a";
      if (index === 0) call("read_file", { file_path: `fact-${letter}.txt` });
      else if (index === 1) call("send_message", { recipient: "parent", message: `CONTROLLED_REPORT_${letter.toUpperCase()}`, summary: `Audit ${letter.toUpperCase()} report` });
      else content = route === "followup" ? "CONTROLLED_FOLLOWUP_FINAL" : `CONTROLLED_CHILD_${letter.toUpperCase()}_FINAL`;
    } else if (route === "reject-parent" && index === 0) {
      call("task", { description: "Refused worktree", prompt: "CONTROLLED_SHOULD_NOT_RUN", agent_type: "general-purpose", isolation: "worktree" });
    } else if (route === "failure-parent" && index === 0) {
      call("task", { description: "Provider failure", prompt: "CONTROLLED_PROVIDER_FAILURE", agent_type: "general-purpose" });
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

async function launch(profile: string, providerUrl: string) {
  const { ELECTRON_RUN_AS_NODE: _nodeMode, ...environment } = process.env;
  return electron.launch({ executablePath: electronExecutable, args: [path.join(desktopRoot, "main.js")], cwd: desktopRoot,
    env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await availablePort()),
      MINICODE_FRONTEND_URL: `http://127.0.0.1:${process.env.MINICODE_E2E_PORT || "43173"}/`,
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
    if (/^(subagent\.|tool_call$|tool_result$|done$|error$|agent\.run\.)/.test(event.type)) incoming.push(event);
  });
  cdp.on("Network.webSocketFrameSent", ({ response }) => {
    if (response.opcode !== 1) return;
    const command = JSON.parse(response.payloadData);
    if (/^(user_message|conversation\.(create|switch)|subagent\.(status|transcript)|session\.restore)$/.test(command.type)) outgoing.push(command);
  });
}

async function sendPrompt(page: Page, content: string, final: string) {
  const composer = page.locator(".composer-input-region textarea").first();
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

test("CONTROLLED environment: real multi-agent start, follow-up, refusal, failure and durable desktop replay", async () => {
  test.setTimeout(240_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-controlled-multiagent-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  // The user home can itself be a Git checkout; its AppData temp tree is not
  // a non-Git workspace. Windows Temp is outside that actual repository.
  const fixtureRoot = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-controlled-workspace-"));
  const workspace = path.join(fixtureRoot, "workspace");
  await mkdir(data, { recursive: true });
  await mkdir(workspace, { recursive: true });
  const gitProbe = spawnSync("git", ["-C", workspace, "rev-parse", "--show-toplevel"], { encoding: "utf8" });
  expect(gitProbe.status, "The refusal fixture must have no real ancestor Git repository").toBe(128);
  expect(gitProbe.stderr).toContain("not a git repository");
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(path.join(workspace, "fact-a.txt"), "CONTROLLED_FACT_A=alpha\n");
  await writeFile(path.join(workspace, "fact-b.txt"), "CONTROLLED_FACT_B=beta\n");
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 12 }, memory: { enabled: false } }));
  const provider = new ControlledProvider(), incoming: Fact[] = [], outgoing: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  const verdicts: Fact[] = [];
  try {
    app = await launch(profile, providerUrl);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    // These are the ordinary production workspace/conversation/permission
    // actions. Agent and message state is only read, never populated by tests.
    await page.evaluate(async (root) => {
      const store = (window as any).__zustandStore;
      store.getState().setWorkingDirectory(root);
      store.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
    }, workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBeTruthy();
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"))).toBe(true);
    const composer = page.locator(".composer-input-region textarea").first();
    await composer.fill("CONTROLLED_MAIN: Exercise the local deterministic multi-agent audit.");
    await composer.press("Enter");
    await expect.poll(() => provider.gates.size, { timeout: 45_000 }).toBe(2);
    await expect.poll(() => incoming.filter((event) => event.type === "subagent.start").length, { timeout: 30_000 }).toBe(2);
    const live = await readAgents(page);
    expect(live.filter((agent) => agent.status === "running" && agent.startedAt > 0)).toHaveLength(2);
    await expect(page.locator(".collaboration-delegated-start")).toHaveCount(2);
    await page.getByRole("button", { name: "切换摘要", exact: true }).click();
    await expect(page.getByRole("complementary", { name: "工作区上下文摘要" })).toBeVisible();
    await expect(page.getByRole("region", { name: "子智能体摘要" })).toContainText("2 运行中");
    await capture(app, page, "01-real-start-main-and-summary");
    await page.getByRole("button", { name: "子智能体", exact: true }).click();
    await expect(page.locator("#right-panel-subagents")).toBeVisible();
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
    await expect(page.locator(".subagents-detail textarea, .subagents-detail .user-cell-wrap")).toHaveCount(0);
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
    await page.getByRole("button", { name: `打开子智能体任务：${refusedAgent.displayName || refusedAgent.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toBeVisible();
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
    expect(failed.record.started_at).toBeGreaterThan(0);
    expect(failureEvents.some((event) => event.type === "subagent.start" && event.subagent_id === failed.subagent_id)).toBe(true);
    expect(failed.result.error).toContain("模型调用失败");
    const providerFailureBlocks = failed.transcript_snapshot.messages.flatMap((message: Fact) => message.blocks ?? []);
    expect(providerFailureBlocks.some((block: Fact) => block.providerState === "failed"
      && block.detail.includes("HTTP=400") && block.detail.includes("code=controlled_fixture"))).toBe(true);
    const failedAgent = (await readAgents(page)).find((agent) => agent.id === failed.subagent_id)!;
    await page.getByRole("button", { name: `打开子智能体任务：${failedAgent.displayName || failedAgent.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toBeVisible();
    const failureDetails = page.locator(".subagents-detail .activity-cell-failed").getByRole("button", { name: /活动详情/ });
    if (await failureDetails.getAttribute("aria-expanded") === "false") await failureDetails.click();
    await expect(page.locator(".subagents-detail")).toContainText("HTTP=400");
    await expect(page.locator(".subagents-detail")).toContainText("controlled_fixture");
    await capture(app, page, "05-real-provider-failure");
    verdicts.push({ scenario: "real HTTP 400 from local Provider", passed: true, terminal: failed });
    const beforeRestart = await readAgents(page);
    const conversationId = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await app.close();
    app = undefined;
    app = await launch(profile, providerUrl);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await attachWireEvidence(page, incoming, outgoing);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBe(conversationId);
    await expect.poll(async () => (await readAgents(page)).length, { timeout: 30_000 }).toBe(4);
    const afterRestart = await readAgents(page);
    const facts = (agents: Fact[]) => agents.map(({ id, status, agentPath, mailboxEpoch, displayName, startedAt, completedAt, model }) => ({ id, status, agentPath, mailboxEpoch, displayName, startedAt, completedAt, model })).sort((a, b) => a.id.localeCompare(b.id));
    expect(facts(afterRestart)).toEqual(facts(beforeRestart));
    await page.getByRole("button", { name: "更多工作区视图", exact: true }).click();
    await page.getByRole("menuitem", { name: "子智能体", exact: true }).click();
    await expect(page.locator("#right-panel-subagents")).toBeVisible();
    await expect(page.locator(".subagents-tab")).toBeVisible();
    await expect(page.locator(".subagents-row").first()).toBeVisible();
    await expect(page.locator(".subagents-tab")).toContainText("已开启 · 0");
    await capture(app, page, "06-real-restored-terminal-list");
    await page.getByRole("button", { name: `打开子智能体任务：${workerA.displayName || workerA.objective}`, exact: true }).click();
    await expect(page.locator(".subagents-detail")).toContainText("CONTROLLED_FOLLOWUP_FINAL", { timeout: 30_000 });
    await expect(page.locator(".subagents-detail")).toBeVisible();
    await capture(app, page, "07-real-restored-child-detail");
    verdicts.push({ scenario: "desktop/backend restart with durable journal and UI state", passed: true, before: beforeRestart, after: afterRestart });
    expect(provider.failures).toEqual([]);
    // A real project was registered by the workspace lifecycle. Open its
    // ordinary new-chat action; do not seed recent projects for screenshots.
    await expect.poll(() => page.evaluate((root) => (window as any).__zustandStore.getState().recentWorkspaces.some((project: any) => project.path.replaceAll("\\", "/").toLowerCase() === root.replaceAll("\\", "/").toLowerCase()), workspace), { timeout: 30_000 }).toBe(true);
    await page.getByRole("button", { name: "在 workspace 中新建任务", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).not.toBe(conversationId);
    await expect(page.locator(".composer-input-region textarea").first()).toBeVisible();
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
    await writeFile(path.join(evidenceRoot, "controlled-evidence.json"), JSON.stringify({ environment: "CONTROLLED: deterministic loopback Provider, real MiniCode desktop/backend/harness; no external Provider claim", testRoot, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures, outgoingCommands: outgoing, incomingEvents: incoming }, null, 2));
  }
});
