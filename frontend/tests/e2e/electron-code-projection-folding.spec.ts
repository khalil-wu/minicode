/** The model alone is deterministic. The desktop, JavaScript VM, nested tools,
 * child agents, WebSocket events and persisted transcript are production code. */
import { _electron as electron, expect, test, type ElectronApplication, type Locator, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
type WireMessage = { role: string; content: string | Array<{ text?: string }>; name?: string };
type ProviderRequest = { model: string; messages: WireMessage[]; tools?: Array<{ function: { name: string } }> };
const repoRoot = path.resolve(import.meta.dirname, "../../..");
const desktopRoot = path.join(repoRoot, "desktop");
const electronExecutable = createRequire(path.join(desktopRoot, "package.json"))("electron") as string;
const evidenceRoot = path.resolve(process.env.MINICODE_PROJECTION_E2E_EVIDENCE_ROOT || path.join(repoRoot, "artifacts", "code-projection-folding-e2e"));
const fixtureModel = "minicode-code-projection-loopback";
const mainFinal = "PROJECTION_MAIN_FINAL";

function messageText(message: WireMessage): string {
  return typeof message.content === "string" ? message.content : message.content.map((part) => part.text || "").join("\n");
}

class ProjectionProvider {
  readonly requests: Fact[] = [];
  readonly failures: Fact[] = [];
  readonly stages = new Map<string, number>();
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
    const userText = body.messages.filter((message) => message.role === "user").map(messageText)
      .findLast((value) => /PROJECTION_(PARENT|CHILD_A|CHILD_B)/.test(value)) || "";
    const memoryConsolidation = body.messages.some((message) => messageText(message).includes("## Memory Writing Agent: Phase 2 (Consolidation)"));
    const route = memoryConsolidation ? "memory-consolidation" : !body.tools?.length ? "auxiliary" : userText.includes("PROJECTION_CHILD_A") ? "child-a"
      : userText.includes("PROJECTION_CHILD_B") ? "child-b" : "parent";
    const tools = body.tools?.map((tool) => tool.function.name) || [];
    const index = this.requests.filter((item) => item.route === route).length;
    const stage = this.stages.get(route) || 0;
    const toolMessage = body.messages.findLast((message) => message.role === "tool");
    // Production context wraps the report in function_call_result XML. The
    // report itself is the actual runtime JSON returned by tool_exec/tool_wait.
    const toolText = toolMessage ? messageText(toolMessage) : "";
    const report = toolText.includes("\"cell_id\"")
      ? JSON.parse(toolText.slice(toolText.indexOf("{"), toolText.lastIndexOf("}") + 1)) as Fact : undefined;
    this.requests.push({ at: Date.now(), route, index, stage, model: body.model, tools, report });
    let content = "";
    let contractFailure = "";
    let calls: Array<{ id: string; type: "function"; function: { name: string; arguments: string } }> = [];
    const call = (name: string, args: Fact) => {
      if (!tools.includes(name)) {
        contractFailure = `${route}: production schema ${name} was not exposed`;
        return;
      }
      calls = [{ id: `projection-${route}-${index}`, type: "function", function: { name, arguments: JSON.stringify(args) } }];
    };
    if (route === "memory-consolidation") {
      expect(tools.slice().sort()).toEqual(["apply_patch", "edit_file", "glob_files", "grep_files", "list_files", "read_artifact", "read_file", "write_file"]);
      content = "Controlled memory consolidation: no file changes needed.";
    } else if (route === "auxiliary") content = "代码投影与子任务折叠验收";
    else if (tools.includes("read_file") || tools.includes("task")) {
      contractFailure = `${route}: code_mode_only unexpectedly exposed direct nested tools`;
    } else if (report?.status === "running") {
      call("tool_wait", { cell_id: report.cell_id, yield_time_ms: 10000, max_chars: route === "parent" && stage === 1 && index === 1 ? 256 : 8000 });
    } else if (route === "parent" && stage === 0) {
      this.stages.set(route, 1);
      call("tool_exec", { yield_time_ms: 0, max_chars: 256, code: [
        'text(await tools.read_file({file_path:"fact-parent.txt"}));',
        'text(await tools.list_files({path:"."}));',
        'text(await tools.run_command({command:"Get-Content -LiteralPath fact-parent.txt -Raw", yield_time_ms:10000}));',
        'text(await tools.run_command({command:"Write-Output \'PROJECTION_COMMAND_STDOUT\'; Start-Sleep -Seconds 5", yield_time_ms:10000}));',
        'text(await tools.run_command({command:"Write-Error \'PROJECTION_COMMAND_STDERR\' -ErrorAction Continue; exit 7", yield_time_ms:10000}));',
        'text(await tools.edit_file({file_path:"fact-parent.txt",old_string:"PROJECTION_FILE_PARENT=actual file bytes",new_string:"PROJECTION_FILE_PARENT=edited file bytes"}));',
        'await new Promise(resolve => setTimeout(resolve, 300)); text("computed 42");',
      ].join("\n") });
    } else if (route === "parent" && stage === 1) {
      if (report?.status !== "completed" || !this.requests.some((item) => item.route === "parent" && item.stage === 1 && (item.report?.output?.includes("computed 42") || item.report?.output_preview?.includes("computed 42")))) {
        contractFailure = "The production JavaScript VM did not retain its actual computation for the model";
      } else {
        this.stages.set(route, 2);
        call("tool_exec", { code: 'throw new Error("PROJECTION_SCRIPT_FAILURE");', yield_time_ms: 10000 });
      }
    } else if (route === "parent" && stage === 2) {
      if (report?.status !== "failed" || !report.error?.includes("PROJECTION_SCRIPT_FAILURE")) {
        contractFailure = "The production JavaScript VM did not retain its independent failure";
      } else {
        this.stages.set(route, 3);
        call("tool_exec", { yield_time_ms: 0, code: `text(await tools.task(${JSON.stringify({ parallel_tasks: [
          { description: "Projection A", prompt: "PROJECTION_CHILD_A: Read fact-child-a.txt, run the specified audit commands and finish.", agent_type: "general-purpose", read_only: false },
          { description: "Projection B", prompt: "PROJECTION_CHILD_B: Read fact-child-b.txt and finish.", agent_type: "general-purpose", read_only: true },
        ] })}));` });
      }
    } else if (route === "parent") {
      if (report?.status !== "completed") contractFailure = "Real nested TaskTool call did not finish";
      else content = mainFinal;
    } else if (stage === 0) {
      this.stages.set(route, 1);
      const childCode = [`text(await tools.read_file({file_path:"fact-${route}.txt"}));`];
      if (route === "child-a") childCode.push(
        'text(await tools.run_command({command:"Get-Content -LiteralPath fact-child-a.txt -Raw", yield_time_ms:10000}));',
        'text(await tools.run_command({command:"Write-Output \'PROJECTION_CHILD_COMMAND_STDOUT\'; Start-Sleep -Seconds 8", yield_time_ms:10000}));',
        'text(await tools.run_command({command:"Write-Error \'PROJECTION_CHILD_COMMAND_STDERR\' -ErrorAction Continue; exit 7", yield_time_ms:10000}));',
      );
      childCode.push('await new Promise(resolve => setTimeout(resolve, 300));');
      call("tool_exec", { yield_time_ms: 0, code: childCode.join("\n") });
    } else {
      if (report?.status !== "completed") contractFailure = `${route}: real file-read code cell did not finish`;
      else {
        await new Promise<void>((resolve) => this.gates.set(route, resolve));
        content = route === "child-a" ? "PROJECTION_CHILD_A_FINAL" : "PROJECTION_CHILD_B_FINAL";
      }
    }
    if (contractFailure) {
      this.failures.push({ at: Date.now(), route, index, message: contractFailure });
      response.writeHead(400, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: contractFailure, type: "projection_fixture_contract_error" } }));
      return;
    }
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const stream = (delta: Fact, finishReason: string | null) => response.write(`data: ${JSON.stringify({ id: `chatcmpl-projection-${route}-${index}`, object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model: fixtureModel,
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
  release(route: string) { this.gates.get(route)!(); this.gates.delete(route); }
  releaseAll() { for (const release of this.gates.values()) release(); this.gates.clear(); }
  async close() { this.releaseAll(); await new Promise<void>((resolve, reject) => this.server.close((error) => error ? reject(error) : resolve())); }
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
  const entry = process.env.MINICODE_PROJECTION_E2E_OFFSCREEN === "1"
    ? path.join(repoRoot, "output/playwright/codex-control-audit-20261013/offscreen-desktop-entry.cjs")
    : path.join(desktopRoot, "main.js");
  return electron.launch({ executablePath: electronExecutable, args: [entry], cwd: desktopRoot,
    env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await availablePort()),
      // Exercise the current production build, including replay after restart.
      MINICODE_FRONTEND_URL: "", MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
      MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"),
      MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key",
      CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: fixtureModel, CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
      CUSTOM_RESPONSES_REASONING_SUMMARY: "off", OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "",
      ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
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
  if (process.env.MINICODE_PROJECTION_E2E_OFFSCREEN !== "1") await app.evaluate(({ BrowserWindow }) => {
      const qaWindow = BrowserWindow.getAllWindows()[0];
      if (qaWindow.isMinimized()) qaWindow.restore();
      qaWindow.showInactive();
    });
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.evaluate(async () => {
    const transitions = document.getAnimations().filter(animation => animation.playState === "running"
      && animation.effect?.getComputedTiming().iterations !== Infinity);
    await Promise.allSettled(transitions.map(animation => animation.finished));
  });
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
  await writeFile(path.join(evidenceRoot, `${name}.png`), Buffer.from(png, "base64"));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].hide());
}

async function attachWireEvidence(page: Page, incoming: Fact[]) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  cdp.on("Network.webSocketFrameReceived", ({ response }) => {
    if (response.opcode !== 1) return;
    const event = JSON.parse(response.payloadData);
    if (/^(subagent\.|tool_call$|tool_result$|done$|error$|agent\.run\.|command\.result$)/.test(event.type)) incoming.push(event);
  });
}

async function readAgents(page: Page): Promise<Fact[]> {
  return page.evaluate(() => (window as any).__zustandStore.getState().subagents.map((agent: any) => ({
    id: agent.id, displayName: agent.displayName, objective: agent.objective, status: agent.status,
    agentPath: agent.agentPath, startedAt: agent.startedAt, completedAt: agent.completedAt,
  })));
}

async function openChild(page: Page, agent: Fact) {
  await page.getByRole("button", { name: `打开子智能体任务：${agent.displayName || agent.objective}`, exact: true }).click();
  await expect(page.locator(".subagents-detail")).toBeVisible();
}

async function expectNoTransport(region: Locator) {
  await expect(region.getByText("输出", { exact: true })).toHaveCount(0);
  await expect(region.locator(".activity-cell-code-output:not(.activity-cell-failed)")).toHaveCount(0);
  expect(await region.innerText()).not.toMatch(/\b(?:cell_id|output_tool_call_ids|pending_tools|is_error|output_bytes|next_cursor|more_output)\b|Script (?:completed|yielded)/);
}

async function expandOperationEvidence(region: Locator) {
  for (const selector of ["button.agent-loop-timeline-group-title[aria-expanded=false]", ".activity-cell-main-button[aria-expanded=false]", ".exec-cell-header-button[aria-expanded=false]"]) {
    const disclosures = region.locator(selector);
    while (await disclosures.count()) await disclosures.first().click();
  }
}

test("CODE PROJECTION: real code VM results and child process disclosures in the production desktop", async () => {
  test.setTimeout(240_000);
  const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-code-projection-"));
  const profile = path.join(testRoot, "profile"), data = path.join(profile, "data");
  const workspace = await mkdtemp(path.join(process.env.SystemRoot!, "Temp", "minicode-code-projection-workspace-"));
  await mkdir(data, { recursive: true });
  await mkdir(evidenceRoot, { recursive: true });
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
  for (const name of ["parent", "child-a", "child-b"]) await writeFile(path.join(workspace, `fact-${name}.txt`), `PROJECTION_FILE_${name.toUpperCase()}=actual file bytes\n${name === "parent" ? "REAL_FILE_PAYLOAD\n".repeat(300) : ""}`);
  await writeFile(path.join(data, "active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(data, "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true, max_iterations: 30 }, memory: { enabled: false } }));
  const provider = new ProjectionProvider(), incoming: Fact[] = [], verdicts: Fact[] = [], rawArtifacts: Fact[] = [];
  const providerUrl = await provider.start();
  let app: ElectronApplication | undefined;
  try {
    app = await launch(profile, providerUrl);
    let page = await app.firstWindow();
    await isolateQaWindow(app);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(page.url()).toContain("/dist/index.html");
    await attachWireEvidence(page, incoming);
    // Ordinary production actions; no message, tool, child or event is seeded.
    await page.evaluate((root) => {
      const state = (window as any).__zustandStore.getState();
      state.setWorkingDirectory(root);
      state.createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" });
    }, workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBeTruthy();
    await expect.poll(() => page.locator('.mc-workspace-group-header').count(), { timeout: 30_000 }).toBeGreaterThan(0);
    await expect.poll(() => page.locator('.session-row-hover[data-active=true]').count()).toBeGreaterThan(0);
    await expect(page.locator('.mc-workspace-group-header[data-active=true]')).toHaveCount(0);
    await capture(app, page, "00-workspace-conversation-has-only-chat-selection");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"))).toBe(true);
    const composer = page.locator(".composer-input-region textarea").first();
    await composer.fill("PROJECTION_PARENT: Exercise code mode output and the child processing disclosure.");
    await composer.press("Enter");
    await expect.poll(() => incoming.some((event) => event.type === "tool_call" && event.name === "run_command" && event.status === "running"), { timeout: 45_000 }).toBe(true);
    const runningGroup = page.locator(".agent-loop-timeline-group-work[data-group-open=true]").first();
    await expect(runningGroup).toBeVisible();
    await expect(runningGroup).toHaveAttribute("data-processing", "true");
    const groupToggle = runningGroup.locator("button.agent-loop-timeline-group-title");
    await expect(groupToggle).toHaveAttribute("aria-expanded", "false");
    await expect(groupToggle).toContainText("正在运行");
    expect(await runningGroup.locator(".agent-loop-timeline-group-live-title").evaluate((element) => getComputedStyle(element).animationName)).toBe("agent-loop-processing-sheen");
    await capture(app, page, "00a-real-running-mixed-group-collapsed-scanning");
    await groupToggle.click();
    await expect(runningGroup).toHaveAttribute("data-processing", "true");
    expect(await runningGroup.locator(".agent-loop-timeline-group-live-title").evaluate((element) => getComputedStyle(element).animationName)).toBe("agent-loop-processing-sheen");
    const runningCommand = runningGroup.locator(".exec-cell-header-button[data-processing=true]");
    await expect(runningCommand).toBeVisible();
    expect(await runningCommand.evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
    await capture(app, page, "00b-real-running-command-expanded-group-scanning");
    await expect.poll(() => incoming.some(event => event.type === "tool_call" && event.command_actions?.[0]?.type === "read" && event.command_actions[0].name === "fact-parent.txt"), { timeout: 30_000 }).toBe(true);
    await expect.poll(() => incoming.some(event => event.type === "subagent.progress" && event.transcript_delta?.events?.some((item: Fact) => item.event_type === "tool_use"
      && item.payload.tool_call?.args?.command?.includes("PROJECTION_CHILD_COMMAND_STDOUT"))), { timeout: 60_000 }).toBe(true);
    await page.getByRole("button", { name: "更多工作区视图", exact: true }).click();
    await page.getByRole("menuitem", { name: "子智能体", exact: true }).click();
    const activeChildA = (await readAgents(page)).find(agent => agent.objective === "Projection A")!;
    await openChild(page, activeChildA);
    const activeChildGroup = page.locator('.subagents-detail .agent-loop-timeline-group-work[data-group-open=true]').first();
    await expect(activeChildGroup).toBeVisible();
    await expect(activeChildGroup.locator('.agent-loop-timeline-group-live-title')).toContainText("PROJECTION_CHILD_COMMAND_STDOUT");
    await activeChildGroup.locator('button.agent-loop-timeline-group-title').click();
    await expect(activeChildGroup).toHaveAttribute("data-processing", "true");
    expect(await activeChildGroup.locator('.agent-loop-timeline-group-live-title').evaluate(element => getComputedStyle(element).animationName)).toBe("agent-loop-processing-sheen");
    expect(await activeChildGroup.locator('.exec-cell-header-button[data-processing=true]').evaluate(element => getComputedStyle(element).animationName)).toBe("none");
    await capture(app, page, "00c-real-child-active-command-head-scanning-static-body");
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    await expect.poll(() => provider.gates.size, { timeout: 60_000 }).toBe(2);
    await expect.poll(async () => (await readAgents(page)).length, { timeout: 30_000 }).toBe(2);
    expect(incoming.some((event) => event.type === "tool_call" && event.name === "read_file" && event.call_source?.kind === "code_mode")).toBe(true);
    expect(incoming.some((event) => event.type === "tool_call" && event.name === "list_files" && event.call_source?.kind === "code_mode")).toBe(true);
    expect(incoming.some((event) => event.type === "tool_call" && event.name === "edit_file" && event.call_source?.kind === "code_mode")).toBe(true);
    expect(await readFile(path.join(workspace, "fact-parent.txt"), "utf8")).toContain("PROJECTION_FILE_PARENT=edited file bytes");
    expect(provider.requests.some((request) => request.report?.status === "running")).toBe(true);
    expect(provider.requests.some((request) => request.report?.status === "failed" && request.report.error.includes("PROJECTION_SCRIPT_FAILURE"))).toBe(true);
    expect(provider.requests.some((request) => (request.report?.output?.includes("computed 42") || request.report?.output_preview?.includes("computed 42")))).toBe(true);
    const truncatedReports = provider.requests.map((request) => request.report).filter((report) => report?.artifact_id && report.output_preview);
    expect(truncatedReports.length).toBeGreaterThan(0);
    for (const truncated of truncatedReports) {
      expect(truncated.output_preview).toContain("truncated; full output in artifact");
      const meta = JSON.parse(await readFile(path.join(data, "artifacts", `${truncated.artifact_id}.meta.json`), "utf8"));
      const raw = JSON.parse(await readFile(path.join(data, "artifacts", `${truncated.artifact_id}.txt`), "utf8"));
      expect(meta.source).toBe("tool_exec.output");
      expect(raw.cell_id).toBe(truncated.cell_id);
      expect(raw.output_tool_call_ids.some((id: string) => id.startsWith("code_"))).toBe(true);
      rawArtifacts.push({ artifactId: truncated.artifact_id, meta, report: raw });
    }
    expect(rawArtifacts.some((artifact) => artifact.report.output.join("\n").includes("PROJECTION_FILE_PARENT=actual file bytes")
      && artifact.report.output.join("\n").includes("REAL_FILE_PAYLOAD"))).toBe(true);
    const toolHistory = await page.evaluate(() => (window as any).__zustandStore.getState().messages.flatMap((message: any) => (message.blocks || [])
      .filter((block: any) => block.type === "tool_call").map((block: any) => block.record)));
    for (const artifact of rawArtifacts) expect(toolHistory.some((record: Fact) => record.artifactId === artifact.artifactId)).toBe(true);
    const mainProcess = page.locator(".chat-turn-process").filter({ hasNot: page.locator(".subagents-detail") }).first();
    await expect(mainProcess).toHaveAttribute("data-collapsed", "false");
    await expectNoTransport(mainProcess);
    await expandOperationEvidence(mainProcess);
    await expect(mainProcess).toContainText("fact-parent.txt");
    await expect(mainProcess.getByRole("region", { name: "命令输出", exact: true }).filter({ hasText: "PROJECTION_COMMAND_STDOUT" })).toHaveCount(1);
    await expect(mainProcess.locator('.exec-cell[data-activity-kind=fileRead] .exec-cell-header-button')).toContainText("fact-parent.txt");
    const readDisclosureGap = await mainProcess.locator('.exec-cell[data-activity-kind=fileRead]').first().evaluate(element => {
      const target = element.querySelector('.exec-cell-command-preview')!.getBoundingClientRect();
      const disclosure = element.querySelector('.exec-cell-toggle')!.getBoundingClientRect();
      return disclosure.left - target.right;
    });
    expect(readDisclosureGap).toBeLessThan(16);
    await expect(mainProcess.locator('.exec-cell[data-status=failed] .exec-cell-header-button')).not.toContainText("失败");
    await expect(mainProcess.locator('.exec-cell[data-status=failed] .exec-cell-output-pre')).toContainText("PROJECTION_COMMAND_STDERR");
    expect(await mainProcess.locator('.exec-cell[data-status=failed] .exec-cell-title').evaluate(element => getComputedStyle(element).color))
      .toBe(await mainProcess.locator('.exec-cell[data-status=success] .exec-cell-title').first().evaluate(element => getComputedStyle(element).color));
    await expect(mainProcess.locator(".exec-cell-header-button[data-processing=true]")).toHaveCount(0);
    expect(await mainProcess.locator(".exec-cell-header-button").first().evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
    await expect(mainProcess).not.toContainText("computed 42");
    await expect(mainProcess).toContainText("PROJECTION_SCRIPT_FAILURE");
    await expectNoTransport(mainProcess);
    await capture(app, page, "01-code-mode-real-leaf-computation-and-failure");
    verdicts.push({ scenario: "real read survives truncated forwarding; computation stays in model wire and original artifacts/history; independent VM failure remains readable", passed: true, toolHistory });
    await page.getByRole("button", { name: "更多工作区视图", exact: true }).click();
    await page.getByRole("menuitem", { name: "子智能体", exact: true }).click();
    const liveAgents = await readAgents(page);
    const workerA = liveAgents.find((agent) => agent.objective === "Projection A")!;
    const workerB = liveAgents.find((agent) => agent.objective === "Projection B")!;
    expect(workerA.status).toBe("running");
    expect(workerB.status).toBe("running");
    await openChild(page, workerA);
    const childAProcess = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(childAProcess).toHaveCount(1);
    await expect(childAProcess).toHaveAttribute("data-collapsed", "false");
    await expandOperationEvidence(childAProcess);
    await expect(childAProcess).toContainText("fact-child-a.txt");
    await expect(childAProcess.locator('.exec-cell[data-activity-kind=fileRead] .exec-cell-header-button')).toContainText("fact-child-a.txt");
    await expect(childAProcess.locator('.exec-cell[data-status=failed] .exec-cell-header-button')).not.toContainText("失败");
    await expect(childAProcess.locator('.exec-cell[data-status=failed] .exec-cell-output-pre')).toContainText("PROJECTION_CHILD_COMMAND_STDERR");
    await expectNoTransport(childAProcess);
    await childAProcess.getByRole("button", { name: "收起处理步骤", exact: true }).click();
    await expect(childAProcess).toHaveAttribute("data-collapsed", "true");
    await expect(childAProcess).not.toContainText("fact-child-a.txt");
    await capture(app, page, "02-running-child-manually-collapsed");
    provider.release("child-a");
    await expect(page.locator(".subagents-detail")).toContainText("PROJECTION_CHILD_A_FINAL", { timeout: 30_000 });
    await expect(childAProcess).toHaveAttribute("data-collapsed", "true");
    await expect(childAProcess.getByRole("button", { name: "展开处理步骤", exact: true })).toHaveAttribute("aria-expanded", "false");
    await capture(app, page, "03-child-completion-preserves-manual-collapse");
    verdicts.push({ scenario: "running child can manually collapse and completion preserves that choice", passed: true, agent: workerA });
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    provider.release("child-b");
    await expect.poll(() => page.evaluate((marker) => (window as any).__zustandStore.getState().messages.some((message: any) => message.role === "assistant" && message.content.includes(marker)), mainFinal), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 30_000 }).toBe(false);
    const changedFile = page.locator(".diff-cell-file-path").filter({ hasText: "fact-parent.txt" }).first();
    await expect(changedFile).toBeVisible();
    await changedFile.click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().diffReview?.diff), { timeout: 30_000 }).toContain("+PROJECTION_FILE_PARENT=edited file bytes");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().rightStackTab), { timeout: 30_000 }).toBe("diff");
    const diffPanel = page.locator(".mc-diff-panel");
    await expect(diffPanel).toBeVisible({ timeout: 30_000 });
    const renderedFile = diffPanel.locator('.mc-diff-file-section[data-path="fact-parent.txt"]');
    await expect(renderedFile).toHaveAttribute("data-open", "true");
    const addedLine = renderedFile.locator(".mc-diff-code-row").filter({
      has: page.locator("[data-diff-text]").filter({ hasText: /^PROJECTION_FILE_PARENT=edited file bytes$/ }),
    });
    await expect(addedLine).toHaveCount(1);
    await expect(addedLine.locator(".mc-diff-code-sign")).toHaveText("+");
    await expect(addedLine.locator(".mc-diff-line-gutter")).toHaveText("1");
    await addedLine.scrollIntoViewIfNeeded();
    await expect(addedLine).toBeVisible();
    await expect(page.getByLabel("正在加载Diff面板", { exact: true })).toHaveCount(0);
    await capture(app, page, "03b-real-edit-file-patch-opened");
    verdicts.push({ scenario: "real edit diff is rendered with added bytes, plus marker and source line 1 before screenshot; no loading skeleton", passed: true });
    await page.getByRole("button", { name: "更多工作区视图", exact: true }).click();
    await page.getByRole("menuitem", { name: "子智能体", exact: true }).click();
    await openChild(page, workerB);
    const childBProcess = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(page.locator(".subagents-detail")).toContainText("PROJECTION_CHILD_B_FINAL", { timeout: 30_000 });
    await expect(childBProcess).toHaveAttribute("data-collapsed", "true");
    await expect(childBProcess).not.toContainText("fact-child-b.txt");
    await capture(app, page, "04-complete-child-first-open-is-collapsed");
    await childBProcess.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expect(childBProcess).toHaveAttribute("data-collapsed", "false");
    await expectNoTransport(childBProcess);
    await expandOperationEvidence(childBProcess);
    await expect(childBProcess).toContainText("fact-child-b.txt");
    await expect(childBProcess).toContainText("PROJECTION_FILE_CHILD-B=actual file bytes");
    await expectNoTransport(childBProcess);
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    await openChild(page, workerB);
    await expect(childBProcess).toHaveAttribute("data-collapsed", "false");
    await capture(app, page, "05-complete-child-explicit-reading-choice-retained");
    verdicts.push({ scenario: "completed child first opens folded; real evidence expands and the reading choice survives returning", passed: true, agent: workerB });
    const conversationId = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await app.close();
    app = undefined;
    app = await launch(profile, providerUrl);
    page = await app.firstWindow();
    await isolateQaWindow(app);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBe(conversationId);
    await expect.poll(async () => (await readAgents(page)).length, { timeout: 30_000 }).toBe(2);
    await page.getByRole("button", { name: "更多工作区视图", exact: true }).click();
    await page.getByRole("menuitem", { name: "子智能体", exact: true }).click();
    await openChild(page, workerB);
    const restoredProcess = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await expect(page.locator(".subagents-detail")).toContainText("PROJECTION_CHILD_B_FINAL", { timeout: 30_000 });
    await expect(restoredProcess).toHaveAttribute("data-collapsed", "true");
    await restoredProcess.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expandOperationEvidence(restoredProcess);
    await expect(restoredProcess).toContainText("fact-child-b.txt");
    await expectNoTransport(restoredProcess);
    await capture(app, page, "06-durable-child-replay-folding-and-real-leaf");
    await page.getByRole("button", { name: "返回子智能体列表", exact: true }).click();
    await openChild(page, workerA);
    const restoredAProcess = page.locator(".subagents-detail").getByRole("region", { name: "Agent 处理进度", exact: true });
    await restoredAProcess.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    await expandOperationEvidence(restoredAProcess);
    await expect(restoredAProcess.locator('.exec-cell[data-activity-kind=fileRead] .exec-cell-header-button')).toContainText("fact-child-a.txt");
    await expect(restoredAProcess.locator('.exec-cell[data-status=failed] .exec-cell-header-button')).not.toContainText("失败");
    await expect(restoredAProcess.locator('.exec-cell[data-status=failed] .exec-cell-output-pre')).toContainText("PROJECTION_CHILD_COMMAND_STDERR");
    await expectNoTransport(restoredAProcess);
    await capture(app, page, "07-durable-child-read-action-and-real-failure-details");
    expect(incoming.filter(event => ["diff.git_working_tree", "diff.git_staged"].includes(event.command) && event.level === "error")).toEqual([]);
    expect(provider.failures).toEqual([]);
    expect(provider.requests.filter((request) => ["parent", "child-a", "child-b"].includes(request.route)).every((request) => !request.tools.includes("read_file") && !request.tools.includes("task"))).toBe(true);
    verdicts.push({ scenario: "production desktop restart restores genuine child transcript with completed process folded", passed: true, conversationId });
  } finally {
    provider.releaseAll();
    if (app) await app.close();
    await provider.close();
    await writeFile(path.join(evidenceRoot, "code-projection-evidence.json"), JSON.stringify({ environment: "CONTROLLED model service; production built Electron/frontend, backend, JavaScript VM, nested tools, children, WebSocket and durable transcript; no store/event fixtures", testRoot, workspace, verdicts, providerRequests: provider.requests, fixtureFailures: provider.failures, rawArtifacts, incomingEvents: incoming }, null, 2));
  }
});
