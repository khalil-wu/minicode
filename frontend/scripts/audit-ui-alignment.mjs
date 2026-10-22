// Isolated CI only: production desktop, backend, provider retries and UI projection.
import { _electron } from "playwright";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { auditProjectConfig } from "./audit-project-config.mjs";
import { auditImportFlow } from "./audit-import-flow.mjs";

assert.equal(process.env.CI, "true", "Run this audit in isolated CI, never against a user's desktop profile");
setTimeout(() => { throw new Error("Current UI audit exceeded its 180-second execution window"); }, 180000).unref();
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const evidence = path.join(root, "output/playwright/ui-alignment-current");
const profile = await mkdtemp(path.join(tmpdir(), "minicode-ui-profile-"));
const workspace = await mkdtemp(path.join(tmpdir(), "minicode-ui-work-"));
const sourceHome = path.join(profile, "source-home");
await mkdir(path.join(profile, "data"));
await mkdir(path.join(profile, "managed"));
await mkdir(evidence, { recursive: true });
await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true, stream_max_attempts: 5 } }));
await writeFile(path.join(profile, "managed/managed-settings.json"), JSON.stringify({ web_search: "disabled" }));
const git = spawnSync("git", ["init", "-q", workspace], { encoding: "utf8" });
assert.equal(git.status, 0, git.stderr);
await mkdir(path.join(workspace, ".minicode"));
await writeFile(path.join(workspace, ".minicode/config.toml"), "# Actual project settings: preserve this comment\n[sandbox_workspace_write]\nnetwork_access = true # Preserve the inline network comment\n");
await mkdir(path.join(sourceHome, ".claude", "commands"), { recursive: true });
await mkdir(path.join(sourceHome, ".claude", "skills", "ui-import-skill"), { recursive: true });
await writeFile(path.join(sourceHome, ".claude", "CLAUDE.md"), "Imported UI audit conventions.\n");
await writeFile(path.join(sourceHome, ".claude", "skills", "ui-import-skill", "SKILL.md"), "---\nname: ui-import-skill\ndescription: Read the UI audit fixture\n---\nRead the actual fixture files.\n");
for (let index = 0; index < 30; index++) await writeFile(path.join(sourceHome, ".claude", "commands", `audit-${String(index).padStart(2, "0")}.md`), `---\ndescription: Actual audit command ${index}\n---\nRead fixture ${index}.\n`);

const model = "ui-audit-loopback";
const requests = [], wire = [], pageErrors = [], logs = [], captures = [], checks = [];
let attempts = 0, terminalAttempts = 0, heldResponse, acceptThirdRequest;
const thirdRequest = new Promise(resolve => { acceptThirdRequest = resolve; });
const reply = response => {
  response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
  response.write(`data: ${JSON.stringify({ id: "ui-audit-reply", object: "chat.completion.chunk", choices: [{ index: 0, delta: { content: "HARNESS_RECONNECT_COMPLETE" }, finish_reason: null }] })}\n\n`);
  response.write(`data: ${JSON.stringify({ id: "ui-audit-reply", object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\n`);
  response.end("data: [DONE]\n\n");
};
const provider = createServer(async (request, response) => {
  if (request.method === "GET") {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ data: [{ id: model, object: "model" }] })); return;
  }
  const chunks = []; for await (const chunk of request) chunks.push(Buffer.from(chunk));
  const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const terminal = body.tools?.length && body.messages.some(item => item.role === "user" && JSON.stringify(item.content).includes("HARNESS_TERMINAL"));
  const main = body.tools?.length && body.messages.some(item => item.role === "user" && JSON.stringify(item.content).includes("HARNESS_RECONNECT"));
  requests.push({ path: request.url, main: Boolean(main), terminal: Boolean(terminal), body });
  if (terminal) {
    terminalAttempts++;
    response.writeHead(503, { "content-type": "application/json", "retry-after": "0" });
    response.end(JSON.stringify({ error: { message: "Controlled provider remained unavailable", type: "server_error", code: "controlled_terminal" } })); return;
  }
  if (!main) { reply(response); return; }
  attempts++;
  if (attempts < 3) {
    response.writeHead(503, { "content-type": "application/json", "retry-after": "1" });
    response.end(JSON.stringify({ error: { message: "Controlled provider stream interrupted", type: "server_error", code: "controlled_retry" } }));
  } else { heldResponse = response; acceptThirdRequest(); }
});
await new Promise(resolve => provider.listen(0, "127.0.0.1", resolve));
const providerUrl = `http://127.0.0.1:${provider.address().port}`;
const portServer = createPortServer();
await new Promise(resolve => portServer.listen(0, "127.0.0.1", resolve));
const backendPort = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const { ELECTRON_RUN_AS_NODE: _nodeMode, ...environment } = process.env;
let app, page, success = false;
const launchOptions = {
  executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
  args: [path.join(root, "desktop/main.js")], cwd: path.join(root, "desktop"),
  env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(backendPort), MINICODE_FRONTEND_URL: "",
    HOME: sourceHome, USERPROFILE: sourceHome,
    MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_DISABLE_CHROMIUM_SANDBOX: "1",
    MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"),
    MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key",
    CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: model, CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
    OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" },
};
async function connectDesktop(scale = 1) {
  app = await _electron.launch({ ...launchOptions, args: [
    ...(scale === 1 ? [] : [`--force-device-scale-factor=${scale}`]), ...launchOptions.args,
  ] });
  app.process().stdout.on("data", chunk => logs.push(String(chunk)));
  app.process().stderr.on("data", chunk => logs.push(String(chunk)));
  page = await app.firstWindow();
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("websocket", socket => {
    socket.on("framereceived", event => wire.push({ direction: "server", message: JSON.parse(String(event.payload)) }));
    socket.on("framesent", event => wire.push({ direction: "client", message: JSON.parse(String(event.payload)) }));
  });
  if (scale === 1) await page.setViewportSize({ width: 1800, height: 1000 });
  else await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1280, 900));
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
}
async function capture(name, context = {}) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.allSettled(document.getAnimations().filter(animation => animation.playState === "running"
      && animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished));
  });
  await writeFile(path.join(evidence, `${name}.aria.txt`), await page.locator("body").ariaSnapshot());
  const metrics = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, dpr: devicePixelRatio, url: location.href,
    theme: document.documentElement.dataset.theme, font: getComputedStyle(document.body).fontFamily }));
  await page.screenshot({ path: path.join(evidence, `${name}.png`) });
  captures.push({ name, ...context, metrics, boundary: "Electron renderer viewport; excludes native OS window decoration" });
}
try {
  await connectDesktop();
  assert(page.url().includes("/dist/index.html"));
  assert.equal(await page.getByRole("alert").count(), 0, "Initial ready state must not produce a disconnected-session error");
  await capture("01-current-build-idle");
  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
  await page.waitForFunction(() => Boolean(window.__zustandStore.getState().conversationId));
  const owner = await page.evaluate(() => window.__zustandStore.getState().conversationId);
  const composer = page.locator(".composer-input-region [data-composer-input]").first();
  await composer.fill("HARNESS_RECONNECT: Return the completed response after the two real provider failures.");
  await composer.press("Enter");
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor({ timeout: 45000 });
  await thirdRequest;
  assert.equal(await page.locator(".stream-error-cell").count(), 1);
  assert.equal(attempts, 3);
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().isStreaming), true);
  await capture("02-actual-provider-reconnect-two");
  await page.locator(".stream-error-summary").click();
  await page.locator(".stream-error-details").waitFor();
  const retryReceipt = wire.findLast(row => row.direction === "server" && row.message.type === "agent.progress"
    && row.message.retry_attempt === 2 && row.message.visibility === "timeline").message;
  assert((await page.locator(".stream-error-details").innerText()).includes(retryReceipt.error_message));
  await capture("03-actual-reconnect-expanded");
  await composer.fill("KEEP_A_DRAFT");
  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
  await page.waitForFunction(previous => window.__zustandStore.getState().conversationId !== previous, owner);
  const other = await page.evaluate(() => window.__zustandStore.getState().conversationId);
  await composer.fill("KEEP_B_DRAFT");
  assert.equal(await page.locator(".stream-error-cell").count(), 0);
  await page.evaluate(id => window.__zustandStore.getState().switchConversation(id), owner);
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor();
  assert.equal(await composer.innerText(), "KEEP_A_DRAFT");
  await capture("04-return-to-running-owner");
  await page.reload();
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor();
  assert.equal(await page.locator(".stream-error-cell").count(), 1);
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().isStreaming), true);
  assert.equal(await composer.innerText(), "KEEP_A_DRAFT");
  await capture("05-reload-restores-live-retry");
  reply(heldResponse);
  await page.getByText("HARNESS_RECONNECT_COMPLETE", { exact: true }).waitFor({ timeout: 30000 });
  await page.waitForFunction(() => !window.__zustandStore.getState().isStreaming);
  await page.evaluate(id => window.__zustandStore.getState().switchConversation(id), other);
  await page.waitForFunction(id => window.__zustandStore.getState().conversationId === id, other);
  assert.equal(await composer.innerText(), "KEEP_B_DRAFT");
  checks.push("Actual provider 503 → retry 1/5 → retry 2/5 → live owner switch → session restore → final response, both drafts retained");

  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
  await page.waitForFunction(previous => window.__zustandStore.getState().conversationId !== previous, other);
  await composer.fill("HARNESS_TERMINAL: Preserve the actual failure after exhausting the provider retry budget.");
  await composer.press("Enter");
  await page.locator(".error-cell-notice").waitFor({ timeout: 60000 });
  await page.waitForFunction(() => !window.__zustandStore.getState().isStreaming);
  assert.equal(await page.locator(".error-cell-notice").count(), 1);
  const terminalError = wire.findLast(row => row.direction === "server" && row.message.type === "error" && row.message.provider_error_type).message;
  assert.equal(await page.getByRole("alert").filter({ hasText: terminalError.message }).count(), 1, "The model failure belongs to its original turn without a duplicate global toast");
  assert.equal(await page.getByText("处理失败", { exact: true }).count(), 0);
  assert(terminalAttempts > 1);
  await capture("06-actual-terminal-model-failure");
  checks.push("Actual exhausted provider retries retain their original Wi-Fi evidence and one standalone terminal notice");

  await composer.fill("KEEP_SETTINGS_DRAFT");
  await page.evaluate(() => { const state = window.__zustandStore.getState(); state.setSettingsTab("appearance"); state.toggleSettings(); });
  await page.getByRole("radio", { name: "浅色", exact: true }).click();
  const trigger = page.locator(".appearance-code-theme-picker .mc-select-trigger").first();
  await trigger.waitFor();
  assert((await trigger.innerText()).includes("ChatGPT"));
  for (const width of [1800, 1280]) {
    await page.setViewportSize({ width, height: 1000 });
    const layout = await trigger.evaluate(element => {
      const rect = node => { const box = node.getBoundingClientRect(); return { x: box.x, y: box.y, width: box.width, height: box.height, centerY: box.y + box.height / 2 }; };
      return { button: rect(element), value: rect(element.querySelector(".mc-select-trigger-value")),
        preview: rect(element.querySelector(".appearance-code-theme-preview")), label: rect(element.querySelector(".mc-select-trigger-label")), arrow: rect(element.lastElementChild) };
    });
    assert(Math.abs(layout.preview.centerY - layout.arrow.centerY) <= 1, "Theme preview and arrow must remain on the same line");
    assert.equal(layout.button.width, 176);
    const modePreviews = await page.locator(".settings-theme-card .settings-theme-preview").evaluateAll(elements => elements.map(element => {
      const { width, height } = element.getBoundingClientRect(); return { width, height, vectors: element.querySelectorAll("svg").length };
    }));
    assert.deepEqual(modePreviews, [{ width: 80, height: 60, vectors: 2 }, { width: 80, height: 60, vectors: 1 }, { width: 80, height: 60, vectors: 1 }]);
    checks.push({ scenario: `Theme trigger current rendered geometry at ${width}px`, layout });
    await capture(`06-theme-row-${width}`);
  }
  await trigger.click();
  assert.equal(await page.getByRole("menuitem", { name: "Codex 主题", exact: true }).locator(".appearance-code-theme-preview").count(), 1);
  await capture("07-theme-root-menu");
  await page.getByRole("menuitem", { name: "Codex 主题", exact: true }).hover();
  await page.getByRole("menuitemradio", { name: "VS Code Plus", exact: true }).waitFor();
  await capture("08-theme-nested-menu");
  await page.keyboard.press("Escape");
  await page.getByRole("menu", { name: "浅色代码主题", exact: true }).waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "导入浅色主题", exact: true }).click();
  const importDialog = page.getByRole("dialog", { name: "导入主题", exact: true });
  const themeInput = importDialog.getByRole("textbox");
  await themeInput.fill("invalid-theme");
  assert.equal(await importDialog.getByRole("button", { name: "导入主题", exact: true }).isDisabled(), true);
  await capture("09-theme-import-invalid");
  await themeInput.fill(await themeInput.getAttribute("placeholder"));
  await importDialog.getByRole("button", { name: "导入主题", exact: true }).click();
  await importDialog.waitFor({ state: "hidden" });
  checks.push("Theme nested menu and actual import callback completed");

  // Current-build page baselines, separate from Source visual acceptance.
  const settingsPages = await page.locator(".settings-center-tab").evaluateAll(elements => elements.map(element => element.getAttribute("aria-label")));
  for (const [index, label] of settingsPages.entries()) {
    await page.locator(".settings-center-tab").filter({ hasText: label }).click();
    if (label === "存储") {
      const chart = page.locator('.settings-storage-chart[role="img"]');
      await chart.waitFor();
      assert.equal((await chart.boundingBox()).height, 24);
      assert.equal(await page.locator(".settings-storage-legend > li").count(), 5);
      assert((await page.locator(".settings-storage-path").innerText()).startsWith(profile));
      checks.push("Real storage measurement uses the Source 24px category bar, five-category legend and actual data path");
    }
    if (label === "配置") {
      const scope = page.getByRole("button", { name: /^配置作用域，当前：/ });
      const verbosity = page.getByRole("button", { name: /^回复详细程度，当前：/ });
      await scope.click();
      assert.equal(await page.getByRole("option", { name: "管理员", exact: true }).count(), 1);
      await capture("settings-06-config-scope-menu");
      await page.getByRole("option", { name: "管理员", exact: true }).click();
      assert.equal(await verbosity.isDisabled(), true);
      await capture("settings-06-config-admin");
      await scope.click();
      await page.getByRole("option", { name: "用户", exact: true }).click();
      assert.equal(await page.getByRole("button", { name: /^网页搜索，当前：/ }).isDisabled(), true);
      await verbosity.click();
      await capture("settings-06-config-verbosity-menu");
      const [savedResponse] = await Promise.all([
        page.waitForResponse(response => response.url().includes("/api/settings/agent-config") && response.request().method() === "PUT"),
        page.getByRole("option", { name: "高", exact: true }).click(),
      ]);
      assert.equal(savedResponse.ok(), true);
      const saved = await savedResponse.json();
      assert.equal(saved.write.status, "applied");
      assert.equal(saved.write.path, path.join(profile, "settings.json"));
      assert.equal(JSON.parse(await readFile(saved.write.path, "utf8")).model_verbosity, "high");
      await page.getByRole("button", { name: "回复详细程度，当前：高", exact: true }).waitFor();
      await capture("settings-06-config-user-saved");
      await verbosity.click();
      const [unsetResponse] = await Promise.all([
        page.waitForResponse(response => response.url().includes("/api/settings/agent-config") && response.request().method() === "PUT"),
        page.getByRole("option", { name: "模型默认值", exact: true }).click(),
      ]);
      assert.equal(unsetResponse.ok(), true);
      const unset = await unsetResponse.json();
      assert.equal(unset.write.fields.model_verbosity.requested_value, null);
      assert.equal(Object.hasOwn(JSON.parse(await readFile(unset.write.path, "utf8")), "model_verbosity"), false);
      await page.getByRole("button", { name: "回复详细程度，当前：模型默认值", exact: true }).waitFor();
      checks.push("Real User/Admin scope menu, managed field lock, actual User file write and explicit-null override removal");
      await auditProjectConfig({ page, app, workspace, profile, capture, checks });
    }
    if (label === "钩子") {
      const emptyTitle = page.getByText("未找到钩子", { exact: true });
      await emptyTitle.waitFor();
      const copyWidth = await emptyTitle.evaluate(element => element.parentElement.getBoundingClientRect().width);
      assert(copyWidth > 400, "The empty Hooks message must use the content column instead of the icon column");
      checks.push({ scenario: "Actual empty Hooks row keeps its text horizontal", copyWidth });
    }
    await capture(`settings-${String(index + 1).padStart(2, "0")}`, { settingsPage: label });
  }
  await auditImportFlow({ page, profile, sourceHome, capture, checks });
  await page.keyboard.press("Escape");
  await page.locator(".settings-center").waitFor({ state: "hidden" });
  assert.equal(await composer.innerText(), "KEEP_SETTINGS_DRAFT");

  // Relaunch only the isolated CI desktop with actual Chromium device scales.
  // These captures exclude OS decorations; Windows-native scaling is separate.
  for (const scale of [1.25, 1.5]) {
    await app.close();
    await connectDesktop(scale);
    const dpr = await page.evaluate(() => devicePixelRatio);
    assert(Math.abs(dpr - scale) < 0.01, `Requested ${scale} scale, actual DPR ${dpr}`);
    await page.evaluate(() => { const state = window.__zustandStore.getState(); state.setSettingsTab("appearance"); state.toggleSettings(); });
    const theme = page.locator(".appearance-code-theme-picker .mc-select-trigger").first();
    await theme.waitFor();
    const geometry = await theme.evaluate(element => {
      const preview = element.querySelector(".appearance-code-theme-preview").getBoundingClientRect();
      const arrow = element.lastElementChild.getBoundingClientRect();
      return { width: element.getBoundingClientRect().width, offset: Math.abs(preview.y + preview.height / 2 - arrow.y - arrow.height / 2) };
    });
    assert(Math.abs(geometry.width - 176) < 0.1 && geometry.offset <= 1);
    await capture(`dpi-${scale}-appearance`);
    await theme.click();
    await page.getByRole("menuitem", { name: "Codex 主题", exact: true }).focus();
    await page.keyboard.press("ArrowRight");
    await page.getByRole("menuitemradio", { name: "VS Code Plus", exact: true }).waitFor();
    await capture(`dpi-${scale}-theme-submenu-focus`);
    await page.keyboard.press("Escape");
    for (const label of settingsPages) {
      await page.locator(".settings-center-tab").filter({ hasText: label }).click();
      if (label === "存储") await page.locator('.settings-storage-chart[role="img"]').waitFor();
      if (label === "配置") await page.getByRole("button", { name: /^配置作用域，当前：/ }).click();
      await capture(`dpi-${scale}-settings-${String(settingsPages.indexOf(label) + 1).padStart(2, "0")}`, { settingsPage: label });
      if (label === "配置") await page.keyboard.press("Escape");
    }
    checks.push({ scenario: `Actual renderer scale ${scale}: Theme geometry and keyboard submenu, all settings-page baselines`, dpr, geometry });
  }
  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors));
  success = true;
} catch (error) {
  if (page) await capture("failure-current-state");
  throw error;
} finally {
  await writeFile(path.join(evidence, "receipt.json"), JSON.stringify({ success, attempts, terminalAttempts, checks, captures, pageErrors,
    scope: "Production backend and renderer; model replies and delivery of one actual completed import response controlled. Real config/read/write/import I/O, real import failure, Chromium scales 1/1.25/1.5. Settings captures are baselines, not every OS/native state." }, null, 2));
  await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
  await writeFile(path.join(evidence, "provider.json"), JSON.stringify(requests, null, 2));
  await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
  await app?.close();
  provider.closeAllConnections();
  await new Promise(resolve => provider.close(resolve));
}
console.log(JSON.stringify({ success, attempts, checks: checks.length, captures: captures.length }));
