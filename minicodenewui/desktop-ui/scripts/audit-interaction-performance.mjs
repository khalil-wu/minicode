import { _electron } from "playwright";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const label = process.argv[2] || "current";
const dist = path.resolve(root, process.argv[3] || "frontend/dist");
const evidence = path.join(root, "output/playwright/interaction-performance-20261024", label);
const profile = process.argv[4] || await mkdtemp(path.join(tmpdir(), "minicode-interaction-profile-"));
const workspace = path.join(profile, "workspace");
await mkdir(evidence, { recursive: true });
await mkdir(workspace, { recursive: true });
await mkdir(path.join(profile, "data"), { recursive: true });
await mkdir(path.join(profile, "managed"), { recursive: true });
await writeFile(path.join(workspace, "example.ts"), "export function sum(a: number, b: number) { return a + b; }\n");
await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
const trustedRoots = process.argv[4] ? JSON.parse(await readFile(path.join(profile, "data/trusted_workspaces.json"), "utf8")).roots : [];
await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [...trustedRoots, workspace] }));
await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false } }));

const markdown = Array.from({ length: 16 }, (_, section) => `## Section ${section + 1}\n\nThis is a controlled long conversation for measuring buttons and layout.\n\n| Item | Result |\n| --- | --- |\n${Array.from({ length: 6 }, (_, row) => `| Entry ${row} | Verified renderer fixture |`).join("\n")}\n\n\`\`\`typescript\n${Array.from({ length: 16 }, (_, line) => `const item${line} = ${line} + ${section};`).join("\n")}\n\`\`\`\n`).join("\n");
const provider = createServer(async (request, response) => {
  if (request.method === "GET") { response.setHeader("content-type", "application/json"); response.end(JSON.stringify({ data: [{ id: "interaction-fixture" }] })); return; }
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString());
  const isTask = Boolean(body.tools?.length);
  response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
  const content = isTask ? markdown : "Interaction performance fixture";
  response.write(`data: ${JSON.stringify({ id: "interaction-reply", choices: [{ index: 0, delta: { content }, finish_reason: null }] })}\n\n`);
  response.write(`data: ${JSON.stringify({ id: "interaction-reply", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\n`);
  response.end("data: [DONE]\n\n");
});
await new Promise(resolve => provider.listen(0, "127.0.0.1", resolve));
const providerUrl = `http://127.0.0.1:${provider.address().port}/v1`;
const portServer = createPortServer();
await new Promise(resolve => portServer.listen(0, "127.0.0.1", resolve));
const backendPort = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const { ELECTRON_RUN_AS_NODE: ignoredNodeMode, ...environment } = process.env;
const errors = [], wire = [], checks = [], measurements = [], logs = [];
let app;
try {
  let entry = path.join(root, "desktop/main.js");
  if (process.env.MINICODE_PERF_PROFILE_BACKEND === "1") {
    const worker = path.join(evidence, "profile-worker.py");
    await writeFile(worker, `import cProfile, functools, json, pathlib, runpy, time
from backend.ws.handler import WebSocketSession
destination = pathlib.Path(${JSON.stringify(evidence)})
original = WebSocketSession.runtime_capabilities_payload
def measured(self, *args, **kwargs):
    profile = cProfile.Profile()
    started = time.perf_counter()
    result = profile.runcall(original, self, *args, **kwargs)
    elapsed = time.perf_counter() - started
    stamp = time.time_ns()
    profile.dump_stats(str(destination / f"capability-{stamp}.prof"))
    with (destination / "timings.jsonl").open("a") as output:
        output.write(json.dumps({"elapsed_ms": elapsed * 1000, "source": kwargs.get("source"), "stamp": stamp}) + "\\n")
    return result
WebSocketSession.runtime_capabilities_payload = measured
runpy.run_module("backend", run_name="__main__")
`);
    entry = path.join(evidence, "profile-entry.cjs");
    await writeFile(entry, `const cp = require("node:child_process"); const spawn = cp.spawn;
cp.spawn = (command, args, options) => spawn(command, args[0] === "-m" && args[1] === "backend"
  ? [${JSON.stringify(worker)}] : args, options);
require(${JSON.stringify(path.join(root, "desktop/main.js"))});\n`);
  }
  app = await _electron.launch({
    executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
    args: [entry], cwd: path.join(root, "desktop"),
    env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(backendPort),
      MINICODE_FRONTEND_URL: pathToFileURL(path.join(dist, "index.html")).href,
      MINICODE_DISABLE_HARDWARE_ACCELERATION: "0", NO_PROXY: "127.0.0.1,localhost,::1", no_proxy: "127.0.0.1,localhost,::1",
      MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"),
      MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_API_KEY: "loopback-fixture",
      CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: "interaction-fixture", CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
      OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" },
  });
  app.process().stdout.on("data", chunk => logs.push(String(chunk)));
  app.process().stderr.on("data", chunk => logs.push(String(chunk)));
  const page = await app.firstWindow();
  page.on("pageerror", error => errors.push(error.message));
  // Electron's unload coordinator owns draft persistence and native dialogs.
  // Let it finish instead of Playwright automatically dismissing its dialog.
  page.on("dialog", dialog => logs.push(`[native-dialog] ${dialog.type()}\n`));
  page.on("websocket", socket => {
    socket.on("framesent", event => wire.push({ at: Date.now(), direction: "client", message: JSON.parse(String(event.payload)) }));
    socket.on("framereceived", event => wire.push({ at: Date.now(), direction: "server", message: JSON.parse(String(event.payload)) }));
  });
  await app.evaluate(({ BrowserWindow }) => { const window = BrowserWindow.getAllWindows()[0]; window.setContentSize(1500, 960); window.show(); });
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
  if (label !== "close-check") {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  await cdp.send("Profiler.enable");
  await page.evaluate(() => {
    window.__interactionTasks = [];
    new PerformanceObserver(list => window.__interactionTasks.push(...list.getEntries().map(entry => ({ start: entry.startTime, duration: entry.duration })))).observe({ type: "longtask", buffered: true });
    document.addEventListener("pointerdown", () => {
      if (!window.__interactionCapture) return;
      const capture = window.__interactionCapture;
      capture.start = performance.now();
      capture.frames = [];
      let previous = capture.start;
      const frame = now => {
        capture.frames.push(now - previous); previous = now;
        const state = window.__zustandStore.getState();
        const ready = !capture.condition || (capture.condition.selector ? document.querySelector(capture.condition.selector)
          : state.conversationId === capture.condition.conversationId && !state.pendingConversationSwitchId && !state.conversationHydration[state.conversationId]?.isHydrating);
        if (capture.click !== undefined && capture.ready === undefined && ready) capture.ready = performance.now();
        if (now - capture.start < 500 || capture.ready === undefined) requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    }, true);
    document.addEventListener("click", () => {
      const capture = window.__interactionCapture;
      if (capture?.start == null) return;
      capture.click = performance.now();
      requestAnimationFrame(() => { capture.firstFrame = performance.now(); });
    }, true);
  });
  async function measure(name, target, ready) {
    await target.waitFor({ state: "visible" });
    if (name === "left-collapse-0") await cdp.send("Profiler.start");
    const before = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map(entry => [entry.name, entry.value]));
    await page.evaluate(condition => { window.__interactionCapture = { start: null, condition }; }, ready);
    await target.click({ noWaitAfter: true });
    await page.waitForFunction(() => window.__interactionCapture.ready !== undefined, null, { timeout: 15000 });
    await page.waitForFunction(() => window.__interactionCapture.frames?.reduce((sum, gap) => sum + gap, 0) >= 500);
    const measurement = await page.evaluate(() => {
      const capture = window.__interactionCapture;
      const frames = capture.frames.slice(1);
      return { responseMs: capture.ready - capture.start, clickHandlerFrameMs: capture.firstFrame - capture.click, maxFrameGapMs: Math.max(...frames), framesOver25Ms: frames.filter(gap => gap > 25).length,
        frameCount: frames.length, longTasks: window.__interactionTasks.filter(task => task.start >= capture.start && task.start < capture.start + 500),
        buttons: document.querySelectorAll("button").length };
    });
    const after = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map(entry => [entry.name, entry.value]));
    for (const metric of ["LayoutCount", "RecalcStyleCount", "LayoutDuration", "ScriptDuration", "TaskDuration"]) measurement[metric] = after[metric] - before[metric];
    measurements.push({ name, ...measurement });
    if (name === "left-collapse-0") await writeFile(path.join(evidence, "sidebar.cpuprofile"), JSON.stringify((await cdp.send("Profiler.stop")).profile));
    console.log(JSON.stringify(measurements.at(-1)));
  }
  console.log(JSON.stringify({ phase: "backend-connected", dist }));
  for (const name of process.argv[4] ? [] : ["PERF_A", "PERF_B"]) {
    await page.evaluate(async ({ workspace }) => {
      await window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: workspace, appMode: "cowork" });
    }, { workspace });
    await page.locator(".composer-input-region [data-composer-input]").fill(name);
    await page.locator(".composer-input-region [data-composer-input]").press("Enter");
    await page.waitForFunction(() => window.__zustandStore.getState().messages.some(message => message.role === "assistant" && message.content.includes("Section 16")) && !window.__zustandStore.getState().isStreaming, null, { timeout: 60000 });
  }
  if (process.argv[4]) {
    await page.waitForFunction(() => window.__zustandStore.getState().conversations.length === 2);
    await page.evaluate(() => {
      const state = window.__zustandStore.getState();
      state.requestConversationSwitch(state.conversations[0].id);
    });
    await page.waitForFunction(() => {
      const state = window.__zustandStore.getState();
      return state.conversationId && !state.pendingConversationSwitchId && !state.conversationHydration[state.conversationId]?.isHydrating
        && state.messages.some(message => message.role === "assistant" && message.content.includes("Section 16"));
    });
  }
  await page.locator(".chat-pane .md-table-wrap").last().waitFor();
  assert.equal(await page.locator(".chat-pane .md-table-wrap").count(), 16, "Every timing run must display the same actual long reply");
  await page.evaluate(() => window.__zustandStore.getState().setDraft("KEEP_PERFORMANCE_DRAFT"));
  await page.screenshot({ path: path.join(evidence, "latest-build.png") });
  checks.push("Two actual conversations persisted through the backend and loopback provider, hardware acceleration enabled, 16 tables and code blocks per reply");
  for (let index = 0; index < 3; index++) {
    await measure(`left-collapse-${index}`, page.getByRole("button", { name: "收起左侧栏", exact: true }));
    await measure(`left-expand-${index}`, page.getByRole("button", { name: "打开左侧栏", exact: true }));
  }
  await page.evaluate(() => window.__zustandStore.getState().setRightStackTab("tasks"));
  for (let index = 0; index < 3; index++) {
    await measure(`right-collapse-${index}`, page.locator(".mc-sidebar-right-actions").getByRole("button", { name: /关闭|收起/ }).last());
    await measure(`right-expand-${index}`, page.getByRole("button", { name: "新建标签页", exact: true }).first());
  }
  await measure("chat-actions", page.getByRole("button", { name: "聊天操作", exact: true }));
  await page.keyboard.press("Escape");
  await measure("model-menu", page.locator(".composer-model-select"));
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "打开个人资料菜单", exact: true }).click();
  await measure("settings-first-open", page.getByRole("menuitem", { name: /设置/ }), { selector: ".settings-center-main" });
  const settingsTabs = await page.locator(".settings-center-tabs .settings-center-tab").evaluateAll(buttons => buttons.map(button => button.getAttribute("aria-label")));
  for (const tab of settingsTabs) {
    await measure(`settings-${tab}`, page.locator(".settings-center-tabs").getByRole("button", { name: tab, exact: true }));
  }
  await page.screenshot({ path: path.join(evidence, "settings.png") });
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().draft), "KEEP_PERFORMANCE_DRAFT");
  checks.push("Composer draft survives every settings page and panel toggle");
  const conversationIds = await page.evaluate(() => window.__zustandStore.getState().conversations.map(item => item.id));
  for (let index = 0; index < 6; index++) {
    const id = conversationIds[index % 2];
    if (await page.evaluate(() => window.__zustandStore.getState().conversationId) === id) continue;
    await measure(`conversation-switch-${index}`, page.locator(`.session-row-hover[data-session-presentation$="${id}"] > button`).first(), { conversationId: id });
  }
  // Prepare the registered views once through their real store action, then
  // measure their actual tab buttons. This distinguishes view preparation
  // from switching a tab the user has already opened.
  await page.evaluate(() => window.__zustandStore.setState({ goalMode: true }));
  await page.locator(".composer-input-region [data-composer-input]").fill("Measure an actual saved goal editor without starting a model turn");
  await page.locator(".composer-input-region [data-composer-input]").press("Enter");
  await page.waitForFunction(() => window.__zustandStore.getState().activeGoal?.text.includes("actual saved goal editor"));
  const panels = ["tasks", "diff", "preview", "browser", "terminal", "files", "goal", "sidechat", "artifacts", "subagents", "inspector", "diagnostics"];
  for (const tab of panels) {
    await page.evaluate(tab => window.__zustandStore.getState().setRightStackTab(tab), tab);
    await page.locator(`#right-panel-${tab}`).waitFor({ state: "visible" });
  }
  for (const tab of panels.filter(tab => tab !== "browser")) {
    await measure(`panel-tab-${tab}`, page.locator(`#right-tab-${tab}`));
  }
  await measure("panel-overflow", page.getByRole("button", { name: "已打开的面板", exact: true }));
  await page.keyboard.press("Escape");
  await measure("scheduled-tasks-open", page.locator(".mc-navigation-rail").getByRole("button", { name: "定时任务", exact: true }), { selector: ".scheduled-tasks-main" });
  await measure("scheduled-task-create-dialog", page.locator(".scheduled-tasks-main").getByRole("button", { name: "新建任务", exact: true }));
  await page.keyboard.press("Escape");
  await measure("home-return", page.getByRole("button", { name: "首页", exact: true }));
  await measure("plugins-open", page.locator(".mc-navigation-rail").getByRole("button", { name: "插件", exact: true }), { selector: ".skills-workspace" });
  await measure("plugins-home-return", page.getByRole("button", { name: "首页", exact: true }));
  await measure("search-open", page.locator(".mc-sidebar-heading-actions").getByRole("button", { name: "搜索", exact: true }));
  await page.keyboard.press("Escape");
  checks.push("Every registered built-in right-panel tab, overflow menu, scheduled tasks and creation dialog, plugin marketplace, home and search");
  await writeFile(path.join(evidence, "resources.json"), JSON.stringify(await page.evaluate(() => performance.getEntriesByType("resource").map(entry => ({ name: entry.name, startTime: entry.startTime, duration: entry.duration, type: entry.initiatorType }))), null, 2));
  assert.deepEqual(errors, []);
  checks.push("No renderer page errors");
  }
} catch (error) {
  errors.push(error.stack || String(error));
  console.error(error);
  process.exitCode = 1;
} finally {
  await writeFile(path.join(evidence, "results.json"), JSON.stringify({ label, dist, profile, workspace, hardwareAcceleration: true, checks, measurements, errors }, null, 2));
  await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
  await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
  if (app) await app.close();
  await writeFile(path.join(evidence, "close.json"), JSON.stringify({ closed: true }));
  await new Promise(resolve => provider.close(resolve));
}
