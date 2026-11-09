/** Model HTTP is controlled; Git, Desktop, WS, harness, history and review UI are real. */
import { _electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
const root = path.resolve(import.meta.dirname, "../../..");
const evidence = path.resolve(process.env.MINICODE_REVIEW_E2E_EVIDENCE_ROOT || path.join(root, "output/playwright/codex-control-audit-20261013/production-review-run01"));
const messageText = (message: Fact): string => typeof message.content === "string" ? message.content : (message.content || []).map((part: Fact) => part.text || "").join("\n");
async function freePort() {
  const server = createPortServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return port;
}
async function capture(app: ElectronApplication, page: Page, name: string) {
  await page.evaluate(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    await Promise.allSettled(document.getAnimations().filter(animation => animation.playState === "running"
      && animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished));
  });
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
  await writeFile(path.join(evidence, name), Buffer.from(png, "base64"));
}

test("Source Markdown review targets, actual file read, model comments and cold history", async () => {
  test.setTimeout(240_000);
  await mkdir(evidence, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-review-qa-"));
  const workBase = path.join(path.parse(process.env.SystemRoot!).root, "Temp");
  await mkdir(workBase, { recursive: true });
  const workspace = await mkdtemp(path.join(workBase, "minicode-review-work-"));
  const git = (...args: string[]) => execFileSync("git", ["-C", workspace, ...args], { encoding: "utf8", windowsHide: true }).trim();
  git("init", "-b", "main"); git("config", "user.name", "MiniCode QA"); git("config", "user.email", "qa@example.test");
  await writeFile(path.join(workspace, "calculate.py"), "def add(a, b):\n    return a + b\n");
  git("add", "calculate.py"); git("commit", "-m", "Isolated review baseline");
  const baseline = git("rev-parse", "HEAD"); git("checkout", "-b", "review-fixture");
  const workingContent = "def add(a, b):\n    return a - b\n";
  await writeFile(path.join(workspace, "calculate.py"), workingContent);
  await writeFile(path.join(workspace, "untracked-note.txt"), "UNTRACKED_REVIEW_FACT\n");
  await mkdir(path.join(profile, "data"), { recursive: true });
  await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", showFullAccess: true,
    questionNotifications: false, turnCompletionNotifications: "never" }));
  const requests: Fact[] = [], counts = new Map<string, number>(), wire: Fact[] = [], errors: string[] = [], warnings: string[] = [], desktopOutput: string[] = [], checks: Fact[] = [];
  const file = path.join(workspace, "calculate.py").replaceAll("\\", "/");
  const directive = `::code-comment{title="[P2] Keep addition semantics" body="The new subtraction breaks callers expecting addition; return a + b." file="${file}" start=2 end=2 priority=2}`;
  const provider = createServer(async (request, response) => {
    if (request.method === "GET") { response.setHeader("content-type", "application/json"); response.end(JSON.stringify({ data: [{ id: "audit-model-a" }] })); return; }
    const parts: Buffer[] = []; for await (const part of request) parts.push(Buffer.from(part));
    const body = JSON.parse(Buffer.concat(parts).toString("utf8"));
    const user = body.messages.filter((item: Fact) => item.role === "user").map(messageText).findLast((content: string) => content.includes("## Code review guidelines:")) || "";
    const route = user ? user.includes("against the base branch") ? "base" : "uncommitted" : "auxiliary";
    const index = counts.get(route) || 0; counts.set(route, index + 1);
    requests.push({ route, index, body });
    const calls = route !== "auxiliary" && index === 0 ? [{ id: `review-read-${route}`, type: "function", function: { name: "tool_exec",
      arguments: JSON.stringify({ code: 'text(await tools.read_file({file_path:"calculate.py"}));', yield_time_ms: 10000 }) } }] : [];
    const content = route === "auxiliary" ? "Code review audit" : `Found one issue in the changed calculation.\n\n${directive}`;
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    response.write(`data: ${JSON.stringify({ id: `review-${route}-${index}`, choices: [{ index: 0, delta: calls.length
      ? { tool_calls: calls.map((call, index) => ({ index, ...call })) } : { content }, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ id: `review-${route}-${index}`, choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }] })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => provider.listen(0, "127.0.0.1", resolve));
  const providerUrl = `http://127.0.0.1:${(provider.address() as { port: number }).port}/v1`;
  const config = JSON.parse(await readFile(path.join(root, "output/playwright/codex-control-audit-20261013/model-control-provider-config.json"), "utf8"));
  config.llm.custom.base_url = providerUrl; config.llm.custom.wire_api = "chat";
  config.agent = { code_mode_only: true }; config.memory = { enabled: false };
  await writeFile(path.join(profile, "settings.json"), JSON.stringify(config));
  const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
  let app: ElectronApplication | undefined;
  const launch = async () => {
    app = await _electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
      args: [path.join(root, "output/playwright/codex-control-audit-20261013/offscreen-desktop-entry.cjs")], cwd: path.join(root, "desktop"),
      env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await freePort()), MINICODE_FRONTEND_URL: "",
        MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"),
        MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
        LLM_PROVIDER: "custom", CUSTOM_API_KEY: "fixture-model-control", CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: "audit-model-a", CUSTOM_WIRE_API: "chat",
        CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "", OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "",
        ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
    app.process().stdout?.on("data", chunk => desktopOutput.push(String(chunk))); app.process().stderr?.on("data", chunk => desktopOutput.push(String(chunk)));
    const page = await app.firstWindow(); page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (["warning", "error"].includes(message.type())) warnings.push(message.text()); });
    const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameReceived", ({ response }) => { if (response.opcode === 1) wire.push(JSON.parse(response.payloadData)); });
    cdp.on("Network.webSocketFrameSent", ({ response }) => { if (response.opcode === 1) wire.push({ outbound: true, ...JSON.parse(response.payloadData) }); });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(page.url()).toContain("/dist/index.html"); return page;
  };
  try {
    let page = await launch();
    await page.evaluate(root => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" }), workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBeTruthy();
    const owner = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setAgentMode("plan"))).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().slashCommands.some((item: Fact) => item.name === "review" && item.source === "builtin"))).toBe(true);
    const input = page.locator(".composer-input-region textarea").first(); await input.fill("/review"); await input.press("Enter");
    await expect(page.locator('[data-review-target="uncommitted"]')).toBeVisible();
    await capture(app!, page, "01-source-review-targets.png");
    await page.locator('[data-review-target="uncommitted"]').click();
    await expect.poll(() => requests.some(item => item.route === "uncommitted" && item.index === 1), { timeout: 30_000 }).toBe(true);
    await expect(page.locator('[data-code-review-comment][data-code-review-line="2"]').first()).toContainText("Keep addition semantics");
    await expect(page.locator(".message-list-content")).toContainText("请检查我未提交的更改");
    await expect(page.locator(".message-list-content")).not.toContainText("Code review guidelines:");
    await expect(page.locator(".message-list-content")).not.toContainText("::code-comment");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().agentMode)).toBe("plan");
    const prepared = wire.find(event => event.type === "command.result" && event.command === "review.prepare" && event.data?.scope?.mode === "uncommitted");
    expect(prepared).toBeTruthy(); expect(prepared.data.diff).toContain("untracked-note.txt");
    const submitted = wire.find(event => event.outbound && event.type === "user_message" && event.code_review?.mode === "uncommitted");
    expect(submitted.content).toContain("## Code review guidelines:"); expect(submitted.display_content).toBe("请检查我未提交的更改");
    expect(wire.some(event => event.type === "tool_call" && event.conversation_id === owner && event.name === "read_file")).toBe(true);
    expect(requests.find(item => item.route === "uncommitted").body.messages.filter((item: Fact) => ["system", "developer"].includes(item.role)).map(messageText).join("\n")).toContain("Default");
    checks.push({ scenario: "actual uncommitted Git scope, Source full model prompt/request-only bubble, default turn with persistent Plan unchanged, real file read and line comment", passed: true });
    await capture(app!, page, "02-source-review-real-comment.png");
    if (process.env.MINICODE_REVIEW_E2E_VISUAL_ONLY === "1") {
      const fonts = await page.locator('[data-code-review-comment]').first().evaluate(element => ({
        card: getComputedStyle(element).fontFamily, ui: getComputedStyle(document.body).fontFamily,
        code: getComputedStyle(document.querySelector('.mc-diff-code-body')!).fontFamily,
      }));
      expect(fonts.card).toBe(fonts.ui);
      const native = await app!.evaluate(({ BrowserWindow }) => ({ visible: BrowserWindow.getAllWindows().map(window => window.isVisible()), qa: (globalThis as any).__offscreenQa }));
      expect(native.visible).toEqual([false]); expect(errors).toEqual([]);
      await writeFile(path.join(evidence, "verdict.json"), JSON.stringify({ passed: true, scope: "new build visual check of review comment font",
        controlledModelOnly: true, profile, workspace, baseline, checks, fonts, native, errors }, null, 2));
      return;
    }
    await input.fill("/review"); await input.press("Enter");
    await expect(page.locator('[data-review-branch="main"]')).toBeVisible(); await page.locator('[data-review-branch="main"]').click();
    await expect.poll(() => requests.some(item => item.route === "base" && item.index === 1), { timeout: 30_000 }).toBe(true);
    const branchPrepared = wire.find(event => event.type === "command.result" && event.command === "review.prepare" && event.data?.scope?.mode === "base-branch");
    expect(branchPrepared.data.scope.mergeBaseSha).toBe(baseline); expect(branchPrepared.data.prompt).toContain(`git diff ${baseline}`);
    expect(await page.locator('[data-code-review-comment]').count()).toBe(1);
    checks.push({ scenario: "real main merge-base in submitted Source target prompt, one accumulated deduplicated finding, unchanged disk contents", passed: true });
    await capture(app!, page, "03-source-review-base-branch.png");
    await input.fill("UNSENT_REVIEW_DRAFT"); await app!.close(); app = undefined;
    page = await launch();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId), { timeout: 30_000 }).toBe(owner);
    await expect(page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_REVIEW_DRAFT");
    await page.getByRole("button", { name: "打开右侧栏", exact: true }).click();
    await page.getByRole("button", { name: "添加面板", exact: true }).click();
    await page.getByRole("menuitem", { name: "变更", exact: true }).click();
    await expect(page.locator('[data-code-review-comment][data-code-review-line="2"]').first()).toContainText("Keep addition semantics");
    await expect(page.locator(".message-list-content")).not.toContainText("Code review guidelines:");
    expect(await readFile(path.join(workspace, "calculate.py"), "utf8")).toBe(workingContent);
    checks.push({ scenario: "real profile/backend cold restore retains source owner, review scope, model finding, request projection and unsent draft", passed: true });
    await capture(app!, page, "04-source-review-cold-restore.png");
    const native = await app!.evaluate(({ BrowserWindow }) => ({ visible: BrowserWindow.getAllWindows().map(window => window.isVisible()), qa: (globalThis as any).__offscreenQa }));
    expect(native.visible).toEqual([false]); expect(errors).toEqual([]);
    await writeFile(path.join(evidence, "verdict.json"), JSON.stringify({ passed: true, controlledModelOnly: true, profile, workspace, baseline, checks, native, errors }, null, 2));
  } finally {
    await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2)); await writeFile(path.join(evidence, "provider-requests.json"), JSON.stringify(requests, null, 2));
    await writeFile(path.join(evidence, "partial-checks.json"), JSON.stringify({ checks, errors }, null, 2));
    await writeFile(path.join(evidence, "renderer-warnings.json"), JSON.stringify(warnings, null, 2)); await writeFile(path.join(evidence, "desktop-output.log"), desktopOutput.join(""));
    if (app) await app.close(); await new Promise<void>((resolve, reject) => provider.close(error => error ? reject(error) : resolve()));
  }
});
