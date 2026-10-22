// Isolated CI only: production desktop, backend, provider retries and UI projection.
import { _electron } from "playwright";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

assert.equal(process.env.CI, "true", "Run this audit in isolated CI, never against a user's desktop profile");
setTimeout(() => { throw new Error("Current UI audit exceeded its 180-second execution window"); }, 180000).unref();
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const evidence = path.join(root, "output/playwright/ui-alignment-current");
const profile = await mkdtemp(path.join(tmpdir(), "minicode-ui-profile-"));
const workspace = await mkdtemp(path.join(tmpdir(), "minicode-ui-work-"));
await mkdir(path.join(profile, "data"));
await mkdir(evidence, { recursive: true });
await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true, stream_max_attempts: 5 }, memory: { enabled: false } }));

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
  app = await _electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
    args: [path.join(root, "desktop/main.js")], cwd: path.join(root, "desktop"),
    env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(backendPort), MINICODE_FRONTEND_URL: "",
      MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_DISABLE_CHROMIUM_SANDBOX: "1",
      MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"),
      MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key",
      CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: model, CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
      OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
  app.process().stdout.on("data", chunk => logs.push(String(chunk)));
  app.process().stderr.on("data", chunk => logs.push(String(chunk)));
  page = await app.firstWindow();
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("websocket", socket => {
    socket.on("framereceived", event => wire.push({ direction: "server", message: JSON.parse(String(event.payload)) }));
    socket.on("framesent", event => wire.push({ direction: "client", message: JSON.parse(String(event.payload)) }));
  });
  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
  assert(page.url().includes("/dist/index.html"));
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
  assert(terminalAttempts > 1);
  await capture("06-actual-terminal-model-failure");
  checks.push("Actual exhausted provider retries retain their original Wi-Fi evidence and one standalone terminal notice");

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
    checks.push({ scenario: `Theme trigger current rendered geometry at ${width}px`, layout });
    await capture(`06-theme-row-${width}`);
  }
  await trigger.click(); await capture("07-theme-root-menu");
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
    await capture(`settings-${String(index + 1).padStart(2, "0")}`, { settingsPage: label });
  }
  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors));
  success = true;
} catch (error) {
  if (page) await capture("failure-current-state");
  throw error;
} finally {
  await writeFile(path.join(evidence, "receipt.json"), JSON.stringify({ success, attempts, terminalAttempts, checks, captures, pageErrors,
    scope: "Production backend and renderer; model replies controlled; settings-page captures are current baselines, not full visual parity acceptance" }, null, 2));
  await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
  await writeFile(path.join(evidence, "provider.json"), JSON.stringify(requests, null, 2));
  await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
  await app?.close();
  provider.closeAllConnections();
  await new Promise(resolve => provider.close(resolve));
}
console.log(JSON.stringify({ success, attempts, checks: checks.length, captures: captures.length }));
