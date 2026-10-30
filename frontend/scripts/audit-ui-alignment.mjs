// Isolated CI only: production desktop, backend, provider retries and UI projection.
import { _electron } from "playwright";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { auditProjectConfig } from "./audit-project-config.mjs";
import { auditImportFlow } from "./audit-import-flow.mjs";
import { auditShellMenus } from "./audit-shell-menus.mjs";
import { auditThreadControls } from "./audit-thread-controls.mjs";
import { auditCustomThemeColors } from "./audit-custom-theme-colors.mjs";

assert.equal(process.env.CI, "true", "Run this audit in isolated CI, never against a user's desktop profile");
const auditStartedAt = Date.now();
let phase = "setup";
function trace(next, details = {}) {
  phase = next;
  console.log(JSON.stringify({ phase, elapsedMs: Date.now() - auditStartedAt, ...details }));
}
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
let attempts = 0, terminalAttempts = 0, heldResponse, acceptThirdRequest, releaseSecondFailure;
const thirdRequest = new Promise(resolve => { acceptThirdRequest = resolve; });
const secondFailure = new Promise(resolve => { releaseSecondFailure = resolve; });
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
    response.writeHead(429, { "content-type": "application/json", "retry-after": "0" });
    response.end(JSON.stringify({ error: { message: "Controlled provider remained rate-limited", type: "rate_limit_error", code: "controlled_terminal" } })); return;
  }
  if (!main) { reply(response); return; }
  attempts++;
  if (attempts < 3) {
    if (attempts === 2) await secondFailure;
    response.writeHead(attempts === 1 ? 429 : 503, { "content-type": "application/json", "retry-after": "1" });
    response.end(JSON.stringify({ error: { message: "Controlled provider stream interrupted", type: attempts === 1 ? "rate_limit_error" : "server_error", code: "controlled_retry" } }));
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
const deadline = setTimeout(async () => {
  const error = "Current UI audit exceeded its 180-second execution window";
  console.error(JSON.stringify({ phase, elapsedMs: Date.now() - auditStartedAt, error }));
  await persistEvidence({ success: false, deadlineExceeded: true, error });
  throw new Error(error);
}, 180000).unref();
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
  page.on("console", message => logs.push(`[renderer:${message.type()}] ${message.text()}\n`));
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("websocket", socket => {
    socket.on("framereceived", event => wire.push({ direction: "server", message: JSON.parse(String(event.payload)) }));
    socket.on("framesent", event => wire.push({ direction: "client", message: JSON.parse(String(event.payload)) }));
  });
  if (scale === 1) await page.setViewportSize({ width: 1800, height: 1000 });
  else await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1280, 900));
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
}
async function waitForCommittedComposerDrafts() {
  // Reload and DPI relaunch verify persisted recovery. Pending native unload
  // is exercised by window-unload-electron.test.js without Playwright's dialog handler.
  await page.waitForFunction(() => {
    const state = window.__zustandStore.getState();
    if (state.composerDraftStorageError) throw new Error(state.composerDraftStorageError);
    return state.composerDraftsReady && !state.composerDraftStoragePending;
  });
}
async function auditRealtimeWorklet() {
  trace("realtime:worklet:begin");
  const entries = await page.locator('script[type="module"][src]').evaluateAll(scripts =>
    scripts.map(script => script.src).filter(url => /\/assets\/index-[^/]+\.js$/.test(url)));
  assert.equal(entries.length, 1, "The current production page must identify its built entry");
  const queue = [entries[0]], visited = new Set();
  let audioUrl;
  while (queue.length && !audioUrl) {
    const url = queue.shift();
    if (visited.has(url)) continue;
    visited.add(url);
    const source = await readFile(fileURLToPath(url), "utf8");
    const imports = /\b(?:import\s*(?:\(\s*)?|from\s*)(["'`])(\.{1,2}\/[^"'`\s$]+\.js)\1/g;
    for (const match of source.matchAll(imports)) {
      const dependency = new URL(match[2], url).href;
      if (/\/realtime-audio-[A-Za-z0-9_-]+\.js$/.test(dependency)) {
        audioUrl = dependency;
        break;
      }
      if (!visited.has(dependency)) queue.push(dependency);
    }
  }
  assert(audioUrl, "The current entry's module graph must reach its production realtime audio chunk");
  const audio = await readFile(fileURLToPath(audioUrl), "utf8");
  const workletAssets = [...new Set(audio.match(/realtime-capture\.worklet-[A-Za-z0-9_-]+\.js/g) ?? [])];
  assert.equal(workletAssets.length, 1, "Realtime capture must use an independent production worklet asset");
  const moduleUrl = new URL(workletAssets[0], audioUrl).href;
  assert.equal(new URL(moduleUrl).protocol, "file:");
  const csp = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
  assert.match(csp, /(?:^|;)\s*script-src 'self';/);
  const loaded = await page.evaluate(async url => {
    const context = new AudioContext({ sampleRate: 24000 });
    try {
      await context.audioWorklet.addModule(url);
      const capture = new AudioWorkletNode(context, "minicode-realtime-capture");
      capture.port.close();
      capture.disconnect();
    } finally {
      await context.close();
    }
    return { moduleUrl: url, sampleRate: context.sampleRate, contextState: context.state };
  }, moduleUrl);
  assert.equal(loaded.contextState, "closed");
  checks.push({ scenario: "The current production realtime capture worklet loads and registers in an actual AudioContext under script-src self", entryUrl: entries[0], audioUrl, ...loaded, csp,
    boundary: "Module loading and processor registration only; no microphone permission, audio capture, transcription or realtime model request" });
  trace("realtime:worklet:complete", loaded);
}
async function capture(name, context = {}) {
  trace(`capture:${name}:begin`);
  const renderState = await page.evaluate(() => ({ theme: document.documentElement.dataset.theme, fonts: document.fonts.status,
    animations: document.getAnimations().filter(animation => animation.playState === "running").map(animation => {
      const timing = animation.effect?.getComputedTiming();
      return { name: animation.animationName ?? animation.constructor.name, duration: String(timing?.duration),
        iterations: String(timing?.iterations), endTime: String(timing?.endTime), currentTime: String(animation.currentTime) };
    }) }));
  trace(`capture:${name}:render-state`, renderState);
  await writeFile(path.join(evidence, `${name}.aria.txt`), await page.locator("body").ariaSnapshot());
  const metrics = await page.evaluate(() => {
    const rulers = {
      chrome: ".header-bar", workspace: ".mc-desktop-workspace", mainToolbar: ".mc-workbench-toolbar", rightToolbar: ".mc-sidebar-right-header",
      sidebarHeader: ".mc-sidebar-heading", mode: ".mc-sidebar-mode-toggle", sidebarRows: ".mc-sidebar-action, .session-row-hover",
      rail: ".mc-navigation-rail", railButtons: ".mc-navigation-rail button", railIcons: ".mc-navigation-rail button > svg",
      tabs: ".mc-sidebar-right-tab-frame", splitters: ".mc-sidebar-left-resize-handle, .mc-sidebar-right-resize-handle",
      transcript: ".message-list-content", transcriptScroll: ".message-list-scroll", composer: ".composer-container", composerFrame: ".composer-textarea-frame",
      composerInput: "[data-composer-input]", composerFooter: ".composer-footer", composerControls: ".composer-send-btn, .composer-attach-btn",
      composerTriggers: ".composer-permission-btn, .composer-model-select", composerGroups: ".composer-footer-primary, .composer-permission-picker, .composer-model-controls, .composer-model-picker",
      composerVoice: ".composer-dictation, .composer-realtime-controls, .composer-voice-btn", titlebarNavigation: ".mc-header-start, .mc-titlebar-navigation, .mc-header-start .mc-header-icon-button",
      titlebarMenu: ".mc-titlebar-menus, .mc-titlebar-menus > button",
      userBubble: ".user-cell-bubble", prose: ".assistant-cell-content, .md-prose", process: ".agent-loop-process-summary, .chat-turn-process-summary",
      homeBrand: ".workbench-empty-brand", homeMark: ".workbench-empty-mark > svg", homeTitle: ".workbench-empty-title",
      settingsCard: ".settings-card", settingsRow: ".settings-row", settingsNav: ".settings-center-tabs", settingsSidebar: ".settings-workspace-sidebar",
      settingsNavItem: ".settings-center-tab", settingsToggle: '.settings-toggle, .settings-center input[type="checkbox"]',
      settingsButton: ".settings-action-button", menus: '.context-menu-surface, [role="menu"], .mc-dropdown-menu', menuRows: '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]',
    };
    const geometry = element => {
      const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      return { tag: element.tagName, className: element.getAttribute("class"), ariaLabel: element.getAttribute("aria-label"),
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right, bottom: rect.bottom },
        boxSizing: style.boxSizing, display: style.display, font: style.fontFamily, fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight,
        padding: style.padding, paddingInline: style.paddingInline, margin: style.margin, gap: style.gap, rowGap: style.rowGap, columnGap: style.columnGap,
        border: style.border, borderWidth: style.borderWidth, borderRadius: style.borderRadius, minHeight: style.minHeight, maxHeight: style.maxHeight,
        minWidth: style.minWidth, maxWidth: style.maxWidth, flexBasis: style.flexBasis, scrollbarGutter: style.scrollbarGutter };
    };
    const domRulers = Object.fromEntries(Object.entries(rulers).map(([key, selector]) => [key, { selector,
      elements: [...document.querySelectorAll(selector)].filter(element => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== "hidden" && !element.closest("[hidden]");
      }).slice(0, 12).map(geometry) }]));
    return { width: innerWidth, height: innerHeight, dpr: devicePixelRatio, url: location.href,
      theme: document.documentElement.dataset.theme, font: getComputedStyle(document.body).fontFamily,
      cssZoom: getComputedStyle(document.documentElement).zoom, shellHeaderHeight: getComputedStyle(document.querySelector(".mc-desktop-shell")).getPropertyValue("--app-shell-header-height"),
      domRulers, draftTrace: window.__uiAlignmentDraftTrace ?? null };
  });
  const browserWindow = await app.browserWindow(page);
  metrics.nativeWindow = await browserWindow.evaluate(window => ({ id: window.id, zoomFactor: window.webContents.getZoomFactor(),
    zoomLevel: window.webContents.getZoomLevel(), bounds: window.getBounds(), contentBounds: window.getContentBounds() }));
  await browserWindow.dispose();
  trace(`capture:${name}:screenshot`);
  await page.screenshot({ path: path.join(evidence, `${name}.png`), animations: "disabled" });
  captures.push({ name, ...context, metrics,
    boundary: "Static Electron renderer viewport; finite animations fast-forwarded and continuous animations temporarily reset; excludes natural-motion completion and native OS window decoration" });
  trace(`capture:${name}:complete`);
}
async function observeDraftLifecycle() {
  await page.evaluate(() => {
    const store = window.__zustandStore;
    const rows = [];
    window.__uiAlignmentDraftTrace = rows;
    const record = cause => {
      const state = store.getState();
      rows.push({ cause, at: performance.now(), conversationId: state.conversationId, pendingConversationSwitchId: state.pendingConversationSwitchId,
        pendingConversationCreateId: state.pendingConversationCreateId, workingDirectory: state.workingDirectory, draft: state.draft,
        richDraft: state.richDraft, ready: state.composerDraftsReady, revision: state.composerDraftRevision,
        cached: Object.fromEntries(Object.entries(state.conversationWorkbenchStates).map(([id, snapshot]) => [id, { draft: snapshot.draft, richDraft: snapshot.richDraft }])),
        editors: [...document.querySelectorAll('.composer-input-region [data-composer-input]')].map(element => ({
          text: element.innerText, textContent: element.textContent, editable: element.getAttribute("contenteditable"), active: document.activeElement === element,
          visible: element.getBoundingClientRect().height > 0 && getComputedStyle(element).visibility !== "hidden" && !element.closest('[hidden], [aria-hidden="true"]') })) });
    };
    store.subscribe((next, previous) => {
      if (next.conversationId !== previous.conversationId || next.pendingConversationSwitchId !== previous.pendingConversationSwitchId
        || next.draft !== previous.draft || next.richDraft !== previous.richDraft || next.conversationWorkbenchStates !== previous.conversationWorkbenchStates) record("store");
    });
    document.addEventListener("input", event => { if (event.target.closest('[data-composer-input]')) record("native-input"); }, true);
    window.__uiAlignmentDraftTraceMark = record;
    record("attached");
  });
}
async function auditSettingsRefresh({ selector, paths, name, size, spinner }) {
  const button = page.locator(selector);
  await button.waitFor({ state: "visible" });
  await page.waitForFunction(selector => !document.querySelector(selector).disabled, selector);
  const idle = await button.evaluate(element => ({ width: element.getBoundingClientRect().width,
    height: element.getBoundingClientRect().height, border: getComputedStyle(element).borderTopColor,
    glyph: element.querySelector("svg").getAttribute("viewBox") }));
  assert.equal(idle.width, size);
  assert.equal(idle.height, size);
  assert.equal(idle.border, "rgba(0, 0, 0, 0)");
  assert.equal(idle.glyph, "0 0 16 16");
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const ready = Promise.withResolvers();
  const actualReads = [];
  const inFlight = [];
  const matcher = url => paths.includes(url.pathname);
  const handler = route => {
    const operation = (async () => {
      assert.equal(route.request().method(), "GET");
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      actualReads.push(new URL(route.request().url()).pathname);
      if (actualReads.length === paths.length) ready.resolve();
      await held;
      await route.fulfill({ response });
    })();
    inFlight.push(operation);
    // The scenario awaits the original operations below, including failures
    // after readiness. Route callbacks only notify its readiness wait here.
    return operation.catch(ready.reject);
  };
  await page.route(matcher, handler);
  try {
    await button.click();
    await ready.promise;
    assert.deepEqual([...actualReads].sort(), [...paths].sort());
    await page.waitForFunction(selector => {
      const element = document.querySelector(selector);
      return element.disabled && element.getAttribute("aria-busy") === "true";
    }, selector);
    assert.equal(await button.isDisabled(), true);
    assert.equal(await button.getAttribute("aria-busy"), "true");
    const pending = await button.evaluate(element => ({ spinner: element.querySelector(".settings-refresh-spinner") !== null,
      glyph: element.querySelector("svg").getAttribute("viewBox"),
      // Rotation changes the painted bounds, not the Source glyph's layout size.
      width: Number.parseFloat(getComputedStyle(element.querySelector("svg")).width) }));
    assert.equal(pending.spinner, spinner);
    assert.equal(pending.glyph, spinner ? "0 0 24 24" : "0 0 16 16");
    assert.equal(pending.width, spinner ? 12 : 16);
    await capture(name, { idle, pending, actualReads });
  } finally {
    release();
    await Promise.allSettled(inFlight);
    await page.unroute(matcher, handler);
    await Promise.all(inFlight);
  }
  await page.waitForFunction(selector => !document.querySelector(selector).disabled, selector);
  assert.equal(await button.getAttribute("aria-busy"), "false");
  checks.push({ scenario: `${name}: actual backend GETs complete, pending delivery disables refresh with the Source glyph; original responses restore the idle button`, actualReads });
}
async function assertSettingsInfoColor(expected) {
  const actual = await page.locator(".settings-learn-more").evaluate(element => {
    const context = document.createElement("canvas").getContext("2d");
    context.fillStyle = getComputedStyle(element).color;
    context.fillRect(0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
  });
  assert.deepEqual(actual, expected);
  checks.push({ scenario: "Source default desktop text-info is independent of the neutral control accent", actual });
  return actual;
}
async function auditPluginCatalog() {
  trace("plugins:catalog:begin");
  const fixedCatalog = JSON.parse(await readFile(path.join(root, "backend/plugins/public-catalog.json"), "utf8"));
  const owner = await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    return { workspace: state.workingDirectory, conversation: state.conversationId, draft: state.draft,
      richDraft: state.richDraft, attachments: state.attachments, hiddenDrafts: state.workspaceComposerDrafts };
  });
  const rail = page.getByRole("navigation", { name: "应用导航", exact: true });
  const received = Promise.withResolvers();
  const matcher = url => url.pathname === "/api/plugins/marketplaces" && url.searchParams.get("workspace_root") === owner.workspace;
  const handler = route => (async () => {
    assert.equal(route.request().method(), "GET");
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    const payload = await response.json();
    await route.fulfill({ response });
    received.resolve(payload);
  })().catch(received.reject);
  // Read the exact forwarded response; Electron's CDP response body may be unavailable.
  await page.route(matcher, handler, { times: 1 });
  let payload;
  try {
    [payload] = await Promise.all([received.promise, rail.getByRole("button", { name: "插件", exact: true }).click()]);
  } finally { await page.unroute(matcher, handler); }
  const { marketplaces } = payload;
  const publicSources = marketplaces.filter(source => source.directory === "public");
  const official = publicSources.find(source => source.name === fixedCatalog.name);
  assert(official, "The actual backend must expose the bundled official public catalog");
  assert.equal(official.status, "ready");
  assert.equal(official.revision, fixedCatalog.revision);
  assert.deepEqual(official.plugins.map(({ id, name, interface: metadata, iconAsset }) => ({ id, name, title: metadata.displayName, iconAsset })),
    fixedCatalog.plugins.map(({ id, name, interface: metadata, iconAsset }) => ({ id, name, title: metadata.displayName, iconAsset })));
  const entries = publicSources.flatMap(source => source.plugins);
  const catalog = page.locator(".plugins-catalog");
  await page.getByRole("navigation", { name: "自定义导航", exact: true }).getByRole("button", { name: "插件", exact: true }).click();
  await catalog.getByRole("tab", { name: "公开", exact: true }).click();
  await page.waitForFunction(count => document.querySelectorAll(".plugins-catalog #plugin-directory-results .plugin-discover-row").length === count, entries.length);
  const rows = catalog.locator("#plugin-directory-results .plugin-discover-row");
  assert.equal(await rows.locator(".plugin-local-title > span:visible").count(), entries.length,
    "Every real public catalog title must be rendered visibly, including titles hidden by inherited CSS regressions");
  const categories = [...new Set(publicSources.flatMap(source => source.plugins.map(plugin =>
    plugin.interface?.category || plugin.category || source.interface?.displayName || source.name)))];
  assert.deepEqual(await catalog.locator(".plugin-catalog-heading h3").allTextContents(), categories);
  const rendered = await rows.evaluateAll(elements => elements.map(element => ({
    id: element.querySelector(".plugin-catalog-open").getAttribute("aria-label").slice("查看插件详情 ".length),
    title: element.querySelector(".plugin-local-title > span").innerText,
  })));
  const byId = (left, right) => left.id.localeCompare(right.id);
  assert.deepEqual(rendered.sort(byId), entries.map(plugin => ({ id: plugin.id, title: plugin.interface?.displayName || plugin.name })).sort(byId));
  const dark = await page.evaluate(() => window.__zustandStore.getState().resolvedTheme === "dark");
  const declaredImages = entries.filter(plugin => plugin.iconAsset || plugin.iconUrl || (plugin.path && plugin.iconVariant))
    .map(plugin => ({ id: plugin.id, asset: dark ? plugin.iconDarkAsset || plugin.iconAsset : plugin.iconAsset }));
  await page.waitForFunction(entries => entries.every(plugin => {
    const button = [...document.querySelectorAll(".plugins-catalog .plugin-catalog-open")]
      .find(element => element.getAttribute("aria-label") === `查看插件详情 ${plugin.id}`);
    const image = button.querySelector("img");
    return image && image.complete && image.naturalWidth > 0 && !image.hidden && image.getClientRects().length > 0
      && (!plugin.asset || image.currentSrc === new URL(plugin.asset, document.baseURI).href);
  }), declaredImages);
  assert.equal(await rows.locator("img:visible").count(), declaredImages.length);
  const nativeCua = entries.find(plugin => plugin.id === "computer-use@minicode-bundled");
  assert(nativeCua?.path && nativeCua.iconVariant, "The real native Cua catalog descriptor must declare its local icon");
  const nativeCuaIcon = await catalog.getByRole("button", { name: `查看插件详情 ${nativeCua.id}`, exact: true }).locator("img").evaluate(image => image.currentSrc);
  for (const name of ["Slack", "Figma"]) assert.equal(await rows.getByText(name, { exact: true }).isVisible(), true);
  const counts = { official: official.plugins.length, public: entries.length, declaredImages: declaredImages.length,
    categories: await catalog.locator(".plugin-source-catalog").count(), revision: official.revision };
  await catalog.locator(".plugin-catalog-page-header").scrollIntoViewIfNeeded();
  await capture("plugins-catalog", counts);

  trace("plugins:figma-detail:begin");
  const figma = official.plugins.find(plugin => plugin.name === "figma");
  await catalog.getByRole("button", { name: `查看插件详情 ${figma.id}`, exact: true }).click();
  const detail = catalog.getByRole("region", { name: "插件详情", exact: true });
  await detail.getByRole("heading", { name: "Figma", exact: true }).waitFor();
  assert.equal(await catalog.locator(".plugin-list-page").isVisible(), false);
  assert.equal(await detail.getByRole("button", { name: "安装插件", exact: true }).isVisible(), true);
  assert.equal(await detail.locator(".plugin-starter-prompts button").count(), figma.defaultPrompt.length);
  for (const prompt of figma.defaultPrompt) assert.equal(await detail.getByRole("button", { name: prompt, exact: true }).isVisible(), true);
  assert.equal(await detail.getByRole("heading", { name: `MCP 服务器 · ${figma.mcp_server_names.length}`, exact: true }).isVisible(), true);
  assert.equal(await detail.getByRole("heading", { name: `技能 · ${figma.skill_summaries.length}`, exact: true }).isVisible(), true);
  assert.deepEqual(await detail.locator(".plugin-detail-skill strong").allTextContents(), figma.skill_summaries.map(skill => skill.displayName || skill.name));
  await page.waitForFunction(count => {
    const images = [...document.querySelectorAll(".plugins-catalog .plugin-detail-page img")];
    return images.length === count && images.every(image => image.complete && image.naturalWidth > 0 && !image.hidden && image.getClientRects().length > 0);
  }, 1 + figma.defaultPrompt.length);
  assert.equal(await detail.locator("img:visible").count(), 1 + figma.defaultPrompt.length);
  await detail.locator(".plugin-detail-breadcrumb").scrollIntoViewIfNeeded();
  await capture("plugins-figma-detail", { pluginId: figma.id, starterPrompts: figma.defaultPrompt.length,
    skills: figma.skill_summaries.length, mcpServers: figma.mcp_server_names.length });
  await detail.getByRole("button", { name: "返回插件列表", exact: true }).click();
  await catalog.getByRole("tab", { name: "公开", exact: true }).waitFor();
  trace("composer:skill-registry:begin");
  const skillsReceived = Promise.withResolvers();
  const skillsMatcher = url => url.pathname === "/api/skills/settings"
    && url.searchParams.get("workspace_root") === owner.workspace && url.searchParams.get("include_projects") === "true";
  const skillsHandler = route => (async () => {
    assert.equal(route.request().method(), "GET");
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    const payload = await response.json();
    await route.fulfill({ response });
    skillsReceived.resolve(payload);
  })().catch(skillsReceived.reject);
  // Capture the real Skills page's normal inventory read; a conversation's
  // deferred availableSkills projection is not the standalone registry.
  await page.route(skillsMatcher, skillsHandler, { times: 1 });
  let skillSettings;
  try {
    [skillSettings] = await Promise.all([skillsReceived.promise,
      page.getByRole("navigation", { name: "自定义导航", exact: true }).getByRole("button", { name: "技能", exact: true }).click()]);
  } finally { await page.unroute(skillsMatcher, skillsHandler); }
  assert.equal(skillSettings.workspace_root, owner.workspace);
  const referenceSkills = skillSettings.skills.filter(skill => skill.path && skill.enabled === true && skill.user_invocable === true
    && skill.source_level !== "plugin" && (!skill.catalog_scope || skill.catalog_scope.workspace_root === owner.workspace));
  const skill = referenceSkills.find(item => item.name === "ui-import-skill") ?? referenceSkills[0];
  assert(skill, "The real standalone registry must contain an enabled user-invocable Skill");
  trace("composer:skill-registry:complete", { name: skill.name, path: skill.path, workspace: skillSettings.workspace_root });
  await rail.getByRole("button", { name: "首页", exact: true }).click();
  await page.locator(".skills-workspace").waitFor({ state: "hidden" });
  assert.deepEqual(await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    return { workspace: state.workingDirectory, conversation: state.conversationId, draft: state.draft,
      richDraft: state.richDraft, attachments: state.attachments, hiddenDrafts: state.workspaceComposerDrafts };
  }), owner);
  checks.push({ scenario: "Real public plugin GET matches the fixed catalog; every title is visible and every declared image decodes; Figma details render actual prompts, MCP and Skills without installing or logging in; returning preserves owner and drafts", ...counts, rendered });
  checks.push({ scenario: "The actual Skills page GET supplies the enabled standalone reference fixture without a side fetch or modifying the conversation skill projection",
    workspace: skillSettings.workspace_root, skill: { name: skill.name, path: skill.path, enabled: skill.enabled, userInvocable: skill.user_invocable } });
  trace("plugins:catalog:complete", counts);
  return { nativeCua, nativeCuaIcon, skill };
}
async function auditComposerInlineReferences({ nativeCua, nativeCuaIcon, skill }) {
  trace("composer:inline-references:begin");
  const file = path.join(workspace, ".minicode", "config.toml"), folder = path.join(workspace, ".minicode");
  assert((await stat(file)).isFile() && (await stat(folder)).isDirectory(), "Composer file references must use the existing audit workspace");
  const editor = page.locator('.composer-input-region [data-rich-composer]').first();
  await editor.waitFor({ state: "visible" });
  const requestCount = requests.length, wireStart = wire.length;
  try {
    const fixture = await page.evaluate(({ nativeCua, skill, workspace, file, folder }) => {
      const store = window.__zustandStore, state = store.getState();
      if (state.draft !== "KEEP_SETTINGS_DRAFT" || state.workingDirectory !== workspace || !state.conversationId) throw new Error("Expected the original settings draft and actual conversation owner");
      const conversation = state.conversations.find(item => item.id === state.conversationId);
      if (!conversation?.title || !skill?.path) throw new Error("Real conversation and Skill identities are required");
      const input = document.querySelector('.composer-input-region [data-rich-composer]');
      const endpoint = (node, offset) => {
        if (!node || !input.contains(node)) return { node, offset };
        const indices = [];
        while (node !== input) { indices.unshift([...node.parentNode.childNodes].indexOf(node)); node = node.parentNode; }
        return { indices, offset };
      };
      const selection = window.getSelection();
      const fields = ["draft", "richDraft", "attachments", "quotedMessage", "selectedMentions", "selectedSkills", "allowedRemoteImageDomains", "queuedEdit", "retainedInputEdit", "goalMode"];
      const original = Object.fromEntries(fields.map(key => [key, state[key]]));
      window.__uiAlignmentComposerAudit = {
        original, conversationId: state.conversationId, workspaceRoot: state.workingDirectory,
        workbenches: state.conversationWorkbenchStates, workspaceDrafts: state.workspaceComposerDrafts,
        focused: document.activeElement, input,
        selection: { anchor: endpoint(selection.anchorNode, selection.anchorOffset), focus: endpoint(selection.focusNode, selection.focusOffset) },
        references: [
          { kind: "plugin", name: nativeCua.interface.displayName, configName: nativeCua.id, path: `plugin://${nativeCua.id}`,
            pluginPath: nativeCua.path, iconVariant: nativeCua.iconVariant, iconVariants: nativeCua.iconVariants, description: nativeCua.description },
          { kind: "skill", name: skill.name, displayName: skill.display_name, path: skill.path, description: skill.description,
            sourceLevel: skill.source_level, iconVariant: skill.icon ? "small" : undefined },
          { kind: "url", name: conversation.title, path: `codex://threads/${encodeURIComponent(conversation.id)}` },
          { kind: "file", name: "config.toml", path: file, workspaceRoot: workspace },
          { kind: "folder", name: ".minicode", path: folder, workspaceRoot: workspace },
        ],
      };
      store.setState({ draft: "", richDraft: undefined, selectedMentions: [], selectedSkills: [] });
      return { conversationId: conversation.id, title: conversation.title, skill: { name: skill.name, path: skill.path, declaredIcon: Boolean(skill.icon) },
        labels: window.__uiAlignmentComposerAudit.references.map(reference => reference.displayName || reference.name) };
    }, { nativeCua, skill, workspace, file, folder });
    assert((await stat(fixture.skill.path)).isFile(), "The selected Skill must have a real local file");
    await page.waitForFunction(() => document.querySelector('.composer-input-region [data-rich-composer]').textContent === "");
    await editor.focus();
    for (let index = 0; index < 5; index++) {
      await page.evaluate(index => {
        const state = window.__zustandStore.getState(), reference = window.__uiAlignmentComposerAudit.references[index];
        if (reference.kind === "skill") state.addSelectedSkill(reference); else state.addSelectedMention(reference);
      }, index);
      await page.waitForFunction(count => {
        const document = window.__zustandStore.getState().richDraft?.document;
        return document?.content?.length === 1 && document.content[0].type === "paragraph"
          && document.content[0].content.filter(node => node.type === "context_mention").length === count
          && window.document.querySelectorAll('.composer-input-region [data-composer-reference]').length === count;
      }, index + 1);
    }
    await editor.press("End");
    await page.keyboard.type("123");
    await page.waitForFunction(() => window.__zustandStore.getState().draft.endsWith("123"));
    await page.waitForFunction(() => {
      const images = [...document.querySelectorAll('.composer-input-region [data-rich-composer] .composer-inline-reference img')];
      return images.length >= 1 && images.every(image => image.complete && image.naturalWidth > 0 && !image.hidden);
    });
    const geometry = await editor.evaluate(async input => {
      await document.fonts.ready;
      const images = [...input.querySelectorAll('.composer-inline-reference img')];
      await Promise.all(images.map(image => image.decode()));
      const rect = element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom }; };
      const paragraph = input.querySelector(":scope > p");
      const refs = [...input.querySelectorAll("[data-composer-reference]")];
      const text = [...paragraph.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.endsWith("123"));
      const range = document.createRange();
      range.setStart(text, text.textContent.length - 3); range.setEnd(text, text.textContent.length);
      const ordinary = range.getBoundingClientRect();
      return {
        paragraphs: input.querySelectorAll(":scope > p").length, text: paragraph.textContent, ordinaryColor: getComputedStyle(paragraph).color,
        ordinary: { x: ordinary.x, y: ordinary.y, width: ordinary.width, height: ordinary.height, bottom: ordinary.bottom },
        separateChipRows: document.querySelectorAll(".composer-context-prefix-row, .composer-skill-tokens").length,
        references: refs.map(element => {
          const icon = element.querySelector(".composer-inline-reference-icon"), label = element.querySelector(".composer-inline-reference-label");
          const glyph = icon.querySelector("img:not([hidden]), svg"), i = rect(icon), l = rect(label);
          const labelRange = document.createRange(); labelRange.selectNodeContents(label);
          const labelText = labelRange.getBoundingClientRect();
          return { reference: JSON.parse(element.dataset.composerReference), sameParagraph: element.parentElement === paragraph,
            display: getComputedStyle(element).display, color: getComputedStyle(label).color, label: label.textContent,
            gap: l.x - i.right, centerOffset: Math.abs(i.y + i.height / 2 - l.y - l.height / 2), icon: i, labelRect: l,
            labelText: { y: labelText.y, bottom: labelText.bottom },
            glyph: glyph?.tagName.toLowerCase(), glyphClass: glyph?.getAttribute("class"), glyphSize: glyph ? rect(glyph) : null };
        }),
        images: images.map(image => ({ src: image.currentSrc, decoded: image.complete && image.naturalWidth > 0,
          visible: !image.hidden && getComputedStyle(image).visibility !== "hidden" && image.getClientRects().length > 0 })),
      };
    });
    assert.equal(geometry.paragraphs, 1);
    assert.equal(geometry.separateChipRows, 0);
    assert.deepEqual(geometry.references.map(item => item.reference.kind), ["plugin", "skill", "url", "file", "folder"]);
    assert.deepEqual(geometry.references.map(item => item.label), fixture.labels);
    assert.equal(geometry.text, `${fixture.labels.join(" ")} 123`);
    for (const reference of geometry.references) {
      assert(reference.sameParagraph && reference.display === "inline-flex", "Every reference must stay in the actual paragraph's inline flow");
      assert(reference.glyphSize?.width > 0 && reference.glyphSize.height > 0, `Missing rendered ${reference.reference.kind} icon`);
      assert(reference.labelRect.width > 0 && reference.labelRect.height > 0, `Reference label is not visibly laid out: ${reference.label}`);
      assert(reference.gap >= 3 && reference.gap <= 5 && reference.centerOffset <= 1, `Icon/label alignment drift: ${reference.label}`);
      assert.notEqual(reference.color, geometry.ordinaryColor, "Reference labels and ordinary text retain their distinct Source colors");
    }
    assert(geometry.ordinary.width > 0 && geometry.ordinary.height > 0);
    const lastReference = geometry.references.at(-1);
    // If 123 wraps onto the next line it is still valid inline flow. When it
    // follows the final atom on the same line, their text baselines must agree.
    if (geometry.ordinary.x >= lastReference.labelRect.right - 1) {
      assert(Math.abs(lastReference.labelText.bottom - geometry.ordinary.bottom) <= 2, "Reference and ordinary text baselines diverged");
    }
    assert(geometry.images.length >= 1 && geometry.images.every(image => image.decoded && image.visible));
    const assetIdentity = value => { const url = new URL(value); url.searchParams.delete("asset_token"); return url.href; };
    assert.equal(assetIdentity(geometry.images[0].src), assetIdentity(nativeCuaIcon), "The composer must reuse the real decoded catalog icon and workspace scope");
    if (!fixture.skill.declaredIcon) assert.match(geometry.references[1].glyphClass, /box/);
    assert.match(geometry.references[2].glyphClass, /message-circle/);
    assert.match(geometry.references[3].glyphClass, /mc-file-icon/);
    assert.match(geometry.references[4].glyphClass, /folder/);
    await capture("composer-inline-references", { fixture, geometry,
      referenceBoundary: "Actual catalog descriptor quoted for visual reference only; not installed, enabled or executed. Real current Skill/conversation/workspace identities; wrapping is allowed within the same paragraph." });
    checks.push({ scenario: "Production context_mention renders five real inline reference kinds with decoded images and ordinary keyboard text", fixture, geometry });
  } finally {
    const restored = await page.evaluate(async () => {
      const saved = window.__uiAlignmentComposerAudit;
      if (!saved) return null;
      const store = window.__zustandStore;
      store.setState(saved.original);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      saved.focused.focus({ preventScroll: true });
      const nodeFor = endpoint => endpoint.indices ? endpoint.indices.reduce((node, index) => node.childNodes[index], saved.input) : endpoint.node;
      const selection = window.getSelection(), anchor = nodeFor(saved.selection.anchor), focus = nodeFor(saved.selection.focus);
      selection.removeAllRanges();
      if (anchor && focus) selection.setBaseAndExtent(anchor, saved.selection.anchor.offset, focus, saved.selection.focus.offset);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const state = store.getState();
      const result = { draft: state.draft, owner: state.conversationId === saved.conversationId && state.workingDirectory === saved.workspaceRoot,
        originalObjects: Object.entries(saved.original).every(([key, value]) => state[key] === value),
        hiddenDrafts: state.workspaceComposerDrafts === saved.workspaceDrafts && state.conversationWorkbenchStates === saved.workbenches,
        selection: selection.anchorNode === anchor && selection.anchorOffset === saved.selection.anchor.offset
          && selection.focusNode === focus && selection.focusOffset === saved.selection.focus.offset };
      delete window.__uiAlignmentComposerAudit;
      return result;
    });
    if (restored) {
      assert.equal(restored.draft, "KEEP_SETTINGS_DRAFT");
      assert(restored.owner && restored.originalObjects && restored.hiddenDrafts && restored.selection, "Composer audit must restore original object identities, owner and selection");
      checks.push({ scenario: "Composer visual audit restores original authoring objects and selection inside the page, including File identities", ...restored });
    }
  }
  assert.equal(requests.length, requestCount, "Reference rendering must not invoke a model");
  assert.equal(wire.slice(wireStart).filter(event => event.direction === "client" && /^(?:user_message|plugin\.(?:install|enable)|mcp\.(?:call|invoke))$/.test(event.message.type)).length, 0);
  trace("composer:inline-references:complete");
}
async function persistEvidence(extra = {}) {
  const draftDiagnostics = page && !page.isClosed() ? await page.evaluate(() => window.__uiAlignmentDraftTrace ?? null) : null;
  await writeFile(path.join(evidence, "receipt.json"), JSON.stringify({ success, attempts, terminalAttempts, checks, captures, pageErrors, phase,
    scope: "Production backend and renderer; controlled model replies and delivery of one actual completed import response. Static screenshots use native Playwright animation finalization. Real config/read/write/import I/O, real import failure, production AudioWorklet module loading under packaged CSP, Chromium scales 1/1.25/1.5. Settings captures are baselines, excluding natural-motion completion and native OS states.", draftDiagnostics, ...extra }, null, 2));
  await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
  await writeFile(path.join(evidence, "provider.json"), JSON.stringify(requests, null, 2));
  await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
}
try {
  await connectDesktop();
  assert(page.url().includes("/dist/index.html"));
  assert.equal(await page.getByRole("alert").count(), 0, "Initial ready state must not produce a disconnected-session error");
  await auditRealtimeWorklet();
  await capture("01-current-build-idle");
  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
  await page.waitForFunction(() => Boolean(window.__zustandStore.getState().conversationId));
  const owner = await page.evaluate(() => window.__zustandStore.getState().conversationId);
  const composer = page.locator(".composer-input-region [data-composer-input]").first();
  await composer.fill("HARNESS_RECONNECT: Return the completed response after the two real provider failures.");
  await composer.press("Enter");
  await page.locator('.stream-error-cell[data-retry-attempt="1"]').waitFor({ timeout: 45000 });
  assert.match(await page.locator(".stream-error-summary").innerText(), /服务器繁忙，正在重连\s*1\s*\/5/u);
  const busyRetryReceipt = wire.findLast(row => row.direction === "server" && row.message.type === "agent.progress"
    && row.message.retry_attempt === 1 && row.message.visibility === "timeline").message;
  assert.equal(busyRetryReceipt.status_code, 429);
  assert.equal(busyRetryReceipt.provider_error_code, "controlled_retry");
  await page.waitForFunction(id => window.__zustandStore.getState().inspectorEntries.some(entry =>
    entry.targetKind === "provider" && entry.payload?.event === "rate_limit" && entry.payload.conversation_id === id), owner);
  const ownedRateLimit = wire.findLast(row => row.direction === "server" && row.message.type === "rate_limit"
    && row.message.conversation_id === owner).message;
  assert.equal(ownedRateLimit.message_id, busyRetryReceipt.message_id);
  assert.equal(ownedRateLimit.turn_id, busyRetryReceipt.turn_id);
  assert.equal(await page.locator('.toast-container [role="alert"]').count(), 0, "The owned retry has one transcript row without a duplicate provider toast");
  await capture("02-actual-provider-busy-reconnect-one");
  await waitForCommittedComposerDrafts();
  await page.reload();
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
  await page.locator('.stream-error-cell[data-retry-attempt="1"]').waitFor();
  assert.match(await page.locator(".stream-error-summary").innerText(), /服务器繁忙，正在重连\s*1\s*\/5/u);
  assert.equal(await page.locator(".stream-error-cell").count(), 1);
  const restoredBusyRetry = await page.evaluate(id => window.__zustandStore.getState().getVisibleMessages(id)
    .flatMap(message => message.blocks || []).find(block => block.type === "progress" && block.retryAttempt === 1 && block.visibility === "timeline"), owner);
  assert.equal(restoredBusyRetry.statusCode, 429);
  assert.equal(restoredBusyRetry.providerErrorCode, "controlled_retry");
  await capture("02-reload-restores-busy-retry");
  releaseSecondFailure();
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor({ timeout: 45000 });
  await thirdRequest;
  assert.equal(await page.locator(".stream-error-cell").count(), 1);
  assert.doesNotMatch(await page.locator(".stream-error-summary").innerText(), /繁忙/u);
  assert.equal(attempts, 3);
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().isStreaming), true);
  await capture("02-actual-provider-reconnect-two");
  await page.locator(".stream-error-summary").click();
  const retryDetails = page.locator('.stream-error-cell[data-retry-attempt="2"] .stream-error-details');
  await retryDetails.first().waitFor();
  const retryReceipt = wire.findLast(row => row.direction === "server" && row.message.type === "agent.progress"
    && row.message.retry_attempt === 2 && row.message.visibility === "timeline").message;
  assert.equal(retryReceipt.status_code, 503);
  const retainedRetryDetails = await retryDetails.allTextContents();
  assert.equal(retainedRetryDetails.length, 2);
  assert(retainedRetryDetails[0].includes(busyRetryReceipt.error_message));
  assert(retainedRetryDetails[1].includes(retryReceipt.error_message));
  assert.equal(await page.locator(".stream-error-details a, .stream-error-details code").count(), 0);
  const retryGeometry = await page.locator(".stream-error-summary").evaluate(element => ({
    outerGap: getComputedStyle(element).columnGap,
    contentGap: getComputedStyle(element.querySelector(".stream-error-summary-content")).columnGap,
    wifi: element.querySelector('[data-source-icon="wifi-light-16"]').getBoundingClientRect().width,
    // The disclosure rotates on expansion; its painted bounding box grows
    // mid-transition even though the Source icon keeps its 12px layout size.
    chevron: Number.parseFloat(getComputedStyle(element.querySelector('[data-source-icon="stream-error-chevron"]')).width),
  }));
  assert.deepEqual(retryGeometry, { outerGap: "4px", contentGap: "6px", wifi: 16, chevron: 12 });
  await capture("03-actual-reconnect-expanded", { retryGeometry });
  checks.push({ scenario: "Source retry aggregate uses 6px icon/summary gap, 4px disclosure gap, 16px Wi-Fi and 12px filled chevron; original details remain plain text", retryGeometry });
  await observeDraftLifecycle();
  await composer.fill("KEEP_A_DRAFT");
  await page.evaluate(workspaceRoot => {
    window.__uiAlignmentDraftTraceMark("before-create-B");
    return window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" });
  }, workspace);
  await page.waitForFunction(previous => window.__zustandStore.getState().conversationId !== previous, owner);
  const other = await page.evaluate(() => window.__zustandStore.getState().conversationId);
  await composer.fill("KEEP_B_DRAFT");
  assert.equal(await page.locator(".stream-error-cell").count(), 0);
  const returnToOwnerAt = wire.length;
  await page.evaluate(id => {
    window.__uiAlignmentDraftTraceMark("before-return-A");
    window.__zustandStore.getState().requestConversationSwitch(id);
  }, owner);
  await page.waitForFunction(id => {
    const state = window.__zustandStore.getState();
    return state.conversationId === id && state.pendingConversationSwitchId === null;
  }, owner);
  assert(wire.slice(returnToOwnerAt).some(row => row.direction === "server" && row.message.type === "conversation.switched"
    && row.message.conversation_id === owner && row.message.client_command_type === "conversation.switch"));
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor();
  // The backend owner is acknowledged before ChatPane exposes its restored
  // editor. A retry cell can already be present while that composer is hidden.
  await composer.waitFor({ state: "visible" });
  assert.equal(await composer.innerText(), "KEEP_A_DRAFT");
  await capture("04-return-to-running-owner");
  await waitForCommittedComposerDrafts();
  await page.reload();
  await page.waitForFunction(() => window.__zustandStore?.getState().isConnected, null, { timeout: 60000 });
  await page.locator('.stream-error-cell[data-retry-attempt="2"]').waitFor();
  assert.equal(await page.locator(".stream-error-cell").count(), 1);
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().isStreaming), true);
  await composer.waitFor({ state: "visible" });
  assert.equal(await composer.innerText(), "KEEP_A_DRAFT");
  await capture("05-reload-restores-live-retry");
  reply(heldResponse);
  await page.getByText("HARNESS_RECONNECT_COMPLETE", { exact: true }).waitFor({ timeout: 30000 });
  await page.waitForFunction(() => !window.__zustandStore.getState().isStreaming);
  const returnToOtherAt = wire.length;
  await page.evaluate(id => window.__zustandStore.getState().requestConversationSwitch(id), other);
  await page.waitForFunction(id => {
    const state = window.__zustandStore.getState();
    return state.conversationId === id && state.pendingConversationSwitchId === null;
  }, other);
  assert(wire.slice(returnToOtherAt).some(row => row.direction === "server" && row.message.type === "conversation.switched"
    && row.message.conversation_id === other && row.message.client_command_type === "conversation.switch"));
  await composer.waitFor({ state: "visible" });
  assert.equal(await composer.innerText(), "KEEP_B_DRAFT");
  checks.push("Actual provider 429 → busy retry 1/5 → session restore → 503 → ordinary retry 2/5 → live owner switch → session restore → final response, both drafts retained");

  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
  await page.waitForFunction(previous => window.__zustandStore.getState().conversationId !== previous, other);
  await composer.fill("HARNESS_TERMINAL: Preserve the actual failure after exhausting the provider retry budget.");
  await composer.press("Enter");
  await page.locator(".error-cell-notice").waitFor({ timeout: 60000 });
  await page.waitForFunction(() => !window.__zustandStore.getState().isStreaming);
  assert.equal(await page.locator(".error-cell-notice").count(), 1);
  const terminalError = wire.findLast(row => row.direction === "server" && row.message.type === "error" && row.message.provider_error_type).message;
  const terminalRateLimit = wire.findLast(row => row.direction === "server" && row.message.type === "rate_limit"
    && row.message.conversation_id === terminalError.conversation_id && row.message.message === terminalError.message).message;
  assert.equal(terminalRateLimit.message_id, terminalError.message_id);
  assert.equal(terminalRateLimit.turn_id, terminalError.turn_id);
  assert.equal(await page.getByRole("alert").filter({ hasText: terminalError.message }).count(), 1, "The model failure belongs to its original turn without a duplicate global toast");
  assert.equal(await page.getByText("处理失败", { exact: true }).count(), 0);
  assert(terminalAttempts > 1);
  await capture("06-actual-terminal-model-failure");
  checks.push("Actual exhausted 429 retries and their terminal rate-limit notification retain the original owner, Wi-Fi evidence and one standalone notice without a duplicate provider toast");

  await composer.fill("KEEP_SETTINGS_DRAFT");
  const archivedTitle = await page.evaluate(id => window.__zustandStore.getState().conversations.find(chat => chat.id === id).title, owner);
  await page.locator(`[data-session-row][data-session-presentation$=":${owner}"]`).first().click({ button: "right" });
  await page.getByRole("menuitem", { name: "归档", exact: true }).click();
  await page.waitForFunction(id => window.__zustandStore.getState().conversations.find(chat => chat.id === id)?.archived, owner);
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
  const themeMenus = await page.locator(".appearance-code-theme-menu").evaluateAll(elements => elements.map(element => {
    const row = element.querySelector('[role="menuitemradio"]');
    const preview = row.querySelector(".appearance-code-theme-preview");
    const slot = row.querySelector(".mc-menu-leading-icon");
    return { width: element.getBoundingClientRect().width, padding: getComputedStyle(element).padding,
      fontSize: getComputedStyle(row).fontSize, fontWeight: getComputedStyle(row).fontWeight, previewWidth: preview.getBoundingClientRect().width,
      slotWidth: slot.getBoundingClientRect().width, contentGap: getComputedStyle(row.querySelector(".mc-menu-content")).gap,
      previewOpacity: getComputedStyle(slot).opacity, trailingGap: getComputedStyle(row).gap,
      checks: Array.from(element.querySelectorAll('[data-sidebar-source-icon="sort-check"]'), check => check.getBoundingClientRect().width) };
  }));
  assert.equal(themeMenus.length, 2);
  for (const { checks: indicatorWidths, ...geometry } of themeMenus) {
    assert(indicatorWidths.every(width => width === 18));
    assert.deepEqual(geometry, {
    width: 240, padding: "4px", fontSize: "13px", fontWeight: "500", previewWidth: 24, slotWidth: 24, contentGap: "8px", previewOpacity: "1", trailingGap: "8px",
    });
  }
  checks.push({ scenario: "Both actual theme menu levels retain the Source 240px surface and full 24px preview slots", themeMenus });
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
  assert.equal(await importDialog.getByRole("button", { name: "导入主题", exact: true }).isDisabled(), false);
  await page.keyboard.press("Escape");
  await importDialog.waitFor({ state: "hidden" });
  assert.equal(await page.evaluate(() => window.__zustandStore.getState().appearanceColors.light), null);
  checks.push("Theme nested menu and invalid/valid import admission; dismissal retains the default palette before the later actual custom imports");

  // Current-build page baselines, separate from Source visual acceptance.
  const settingsPages = await page.locator(".settings-center-tab").evaluateAll(elements => elements.map(element => element.getAttribute("aria-label")));
  for (const [index, label] of settingsPages.entries()) {
    await page.locator(".settings-center-tab").filter({ hasText: label }).click();
    if (label === "已归档的聊天") {
      const title = page.locator(".settings-archive-title").filter({ hasText: archivedTitle });
      await title.waitFor();
      const archiveGeometry = await title.evaluate(element => {
        const row = element.closest(".settings-row"), date = row.querySelector("time"), copy = element.parentElement;
        const titleStyle = getComputedStyle(element), dateStyle = getComputedStyle(date), rowStyle = getComputedStyle(row);
        const deleteStyle = getComputedStyle(row.querySelector(".settings-archive-icon-action"));
        return { tag: element.tagName, titleSize: titleStyle.fontSize, titleWeight: titleStyle.fontWeight,
          dateSize: dateStyle.fontSize, dateHeight: dateStyle.lineHeight, gap: getComputedStyle(copy).gap,
          padding: [rowStyle.paddingTop, rowStyle.paddingRight, rowStyle.paddingBottom, rowStyle.paddingLeft],
          leftDifference: element.getBoundingClientRect().left - date.getBoundingClientRect().left,
          deleteButton: { width: deleteStyle.width, height: deleteStyle.height, border: deleteStyle.borderTopWidth, background: deleteStyle.backgroundColor } };
      });
      assert.deepEqual(archiveGeometry, { tag: "SPAN", titleSize: "13px", titleWeight: "500", dateSize: "12px", dateHeight: "16px", gap: "2px",
        padding: ["12px", "16px", "12px", "16px"], leftDifference: 0,
        deleteButton: { width: "28px", height: "28px", border: "0px", background: "rgba(0, 0, 0, 0)" } });
      await title.click();
      assert.equal(await page.evaluate(() => window.__zustandStore.getState().conversationId), terminalError.conversation_id);
      assert.equal(await page.evaluate(() => window.__zustandStore.getState().draft), "KEEP_SETTINGS_DRAFT");
      await capture("settings-archive-populated", { archiveGeometry });
      await page.getByRole("button", { name: `恢复 ${archivedTitle}`, exact: true }).click();
      await page.getByRole("button", { name: "查看", exact: true }).waitFor();
      assert.equal(await page.evaluate(() => window.__zustandStore.getState().conversationId), terminalError.conversation_id);
      await page.getByRole("button", { name: "查看", exact: true }).click();
      await page.waitForFunction(id => window.__zustandStore.getState().conversationId === id && !window.__zustandStore.getState().pendingConversationSwitchId, owner);
      await page.evaluate(id => window.__zustandStore.getState().requestConversationSwitch(id), terminalError.conversation_id);
      await page.waitForFunction(id => window.__zustandStore.getState().conversationId === id && !window.__zustandStore.getState().pendingConversationSwitchId, terminalError.conversation_id);
      await composer.waitFor({ state: "visible" });
      assert.equal(await composer.innerText(), "KEEP_SETTINGS_DRAFT");
      await page.evaluate(() => window.__zustandStore.getState().toggleSettings());
      checks.push({ scenario: "Actual archived row reuses Source settings typography and spacing; title leaves owner/draft intact; unarchive navigates only through View and preserves the hidden draft", archiveGeometry });
    }
    if (label === "存储") {
      const chart = page.locator('.settings-storage-chart[role="img"]');
      await chart.waitFor();
      assert.equal((await chart.boundingBox()).height, 24);
      assert.equal(await page.locator(".settings-storage-legend > li").count(), 5);
      assert((await page.locator(".settings-storage-path").innerText()).startsWith(profile));
      checks.push("Real storage measurement uses the Source 24px category bar, five-category legend and actual data path");
    }
    if (label === "配置") {
      const defaultsHeading = page.getByRole("heading", { name: "智能体默认设置", exact: true });
      await defaultsHeading.waitFor();
      assert.equal(await page.getByRole("link", { name: "了解更多", exact: true }).getAttribute("href"), "https://developers.openai.com/codex/config-basic");
      const scope = page.getByRole("button", { name: /^配置作用域，当前：/ });
      const verbosity = page.getByRole("button", { name: /^输出详细程度，当前：/ });
      const configGeometry = await page.evaluate(() => {
        const description = document.querySelector(".settings-page-heading-copy p");
        const heading = document.querySelector(".settings-tab-pane:not([hidden]) .settings-group-title");
        const toolbar = document.querySelector(".config-scope-toolbar");
        const opener = document.querySelector(".config-open-button");
        const headingText = document.createRange();
        headingText.selectNodeContents(heading);
        return { descriptionBottom: description.getBoundingClientRect().bottom, headingTop: heading.getBoundingClientRect().top,
          headingBottom: heading.getBoundingClientRect().bottom, headingTextBottom: headingText.getBoundingClientRect().bottom,
          toolbarTop: toolbar.getBoundingClientRect().top,
          openerBorder: getComputedStyle(opener).borderTopWidth, openerIconWidth: opener.querySelector("svg").getBoundingClientRect().width };
      });
      assert(configGeometry.descriptionBottom < configGeometry.headingTop && configGeometry.headingBottom <= configGeometry.toolbarTop
        && configGeometry.headingTextBottom < configGeometry.toolbarTop);
      assert.equal(configGeometry.openerBorder, "0px");
      assert.equal(configGeometry.openerIconWidth, 14);
      checks.push({ scenario: "Actual Configuration description → Agent defaults → scope toolbar; Source ghost opener and external arrow", configGeometry });
      await scope.click();
      assert.equal(await page.getByRole("option", { name: "管理员", exact: true }).count(), 1);
      const scopeMenuGeometry = await page.getByRole("listbox").evaluate(element => {
        const row = element.querySelector('[role="option"]');
        const style = getComputedStyle(row);
        return { width: element.getBoundingClientRect().width, padding: getComputedStyle(element).padding,
          fontSize: style.fontSize, rowPadding: style.padding, rowMinHeight: style.minHeight };
      });
      assert.deepEqual(scopeMenuGeometry, { width: 240, padding: "4px", fontSize: "13px", rowPadding: "5px 8px", rowMinHeight: "0px" });
      checks.push({ scenario: "Actual configuration scope uses Source menuWide and default menu rows", scopeMenuGeometry });
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
      await page.getByRole("button", { name: "输出详细程度，当前：高", exact: true }).waitFor();
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
      await page.getByRole("button", { name: "输出详细程度，当前：模型默认值", exact: true }).waitFor();
      checks.push("Real User/Admin scope menu, managed field lock, actual User file write and explicit-null override removal");
      await auditProjectConfig({ page, app, workspace, profile, capture, checks, wire, trace });
    }
    if (label === "钩子") {
      await assertSettingsInfoColor([51, 156, 255]);
      const emptyTitle = page.getByText("未找到钩子", { exact: true });
      await emptyTitle.waitFor();
      const copyWidth = await emptyTitle.evaluate(element => element.parentElement.getBoundingClientRect().width);
      assert(copyWidth > 400, "The empty Hooks message must use the content column instead of the icon column");
      checks.push({ scenario: "Actual empty Hooks row keeps its text horizontal", copyWidth });
      await auditSettingsRefresh({ selector: ".settings-hooks-refresh", paths: ["/api/settings/hooks"],
        name: "settings-12-refresh-pending", size: 26, spinner: false });
    }
    if (label === "Worktrees") {
      await auditSettingsRefresh({ selector: ".settings-worktrees-refresh",
        paths: ["/api/settings/worktrees", "/api/settings/worktrees/managed"],
        name: "settings-15-refresh-pending", size: 28, spinner: true });
    }
    await capture(`settings-${String(index + 1).padStart(2, "0")}`, { settingsPage: label });
  }
  await auditImportFlow({ page, profile, sourceHome, capture, checks, trace });
  trace("appearance:dark:navigate");
  await page.locator(".settings-center-tab").filter({ hasText: "外观" }).click();
  trace("appearance:dark:select-mode");
  await page.getByRole("radio", { name: "深色", exact: true }).click();
  await page.locator(".settings-center-tab").filter({ hasText: "钩子" }).click();
  const darkInfoColor = await assertSettingsInfoColor([153, 206, 255]);
  await capture("settings-12-dark-info-link", { darkInfoColor });
  await page.locator(".settings-center-tab").filter({ hasText: "外观" }).click();
  trace("appearance:dark:open-import");
  await page.getByRole("button", { name: "导入深色主题", exact: true }).click();
  const darkThemeDialog = page.getByRole("dialog", { name: "导入主题", exact: true });
  await darkThemeDialog.waitFor();
  const darkDialogOverlay = await page.locator(".appearance-theme-import-backdrop").evaluate(element => getComputedStyle(element).backgroundColor);
  assert.equal(darkDialogOverlay, "rgba(0, 0, 0, 0.133)");
  await capture("dialog-dark-theme-import", { darkDialogOverlay });
  await page.keyboard.press("Escape");
  await darkThemeDialog.waitFor({ state: "hidden" });
  await page.getByRole("radio", { name: "浅色", exact: true }).click();
  checks.push("Actual default Dialog overlay uses the Source #0002 value in light import progress and dark theme import");
  await page.keyboard.press("Escape");
  await page.locator(".settings-center").waitFor({ state: "hidden" });
  assert.equal(await composer.innerText(), "KEEP_SETTINGS_DRAFT");
  const pluginCatalog = await auditPluginCatalog();
  await auditComposerInlineReferences(pluginCatalog);
  assert.equal(await composer.innerText(), "KEEP_SETTINGS_DRAFT");
  trace("shell:menus");
  await auditShellMenus({ page, capture, checks });
  await auditThreadControls({ page, app, wire, capture, checks });
  assert.equal(await composer.innerText(), "KEEP_SETTINGS_DRAFT");

  // Relaunch only the isolated CI desktop with actual Chromium device scales.
  // These captures exclude OS decorations; Windows-native scaling is separate.
  for (const scale of [1.25, 1.5]) {
    await waitForCommittedComposerDrafts();
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
    await page.keyboard.type("VS ");
    assert.equal(await page.getByRole("menu", { name: "Codex 主题", exact: true }).isVisible(), true);
    await page.keyboard.type("Code");
    assert.equal(await page.getByRole("menuitemradio", { name: "VS Code Plus", exact: true }).evaluate(element => document.activeElement === element), true);
    await capture(`dpi-${scale}-theme-submenu-focus`);
    const lastTheme = page.getByRole("menu", { name: "Codex 主题", exact: true }).getByRole("menuitemradio").last();
    const lastThemeText = await lastTheme.textContent();
    await page.keyboard.press("End");
    await page.waitForFunction(text => document.activeElement?.textContent === text, lastThemeText);
    const lastThemeGeometry = await lastTheme.evaluate(element => {
      const item = element.getBoundingClientRect(), menu = element.closest('[role="menu"]').getBoundingClientRect();
      return { focused: document.activeElement === element, itemTop: item.top, itemBottom: item.bottom, menuTop: menu.top, menuBottom: menu.bottom };
    });
    assert(lastThemeGeometry.focused && lastThemeGeometry.itemTop >= lastThemeGeometry.menuTop
      && lastThemeGeometry.itemBottom <= lastThemeGeometry.menuBottom);
    await capture(`dpi-${scale}-theme-submenu-last-item`, { lastThemeText, lastThemeGeometry });
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
  trace("appearance:custom-colors");
  await auditCustomThemeColors({ page, capture, checks });
  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors));
  success = true;
} catch (error) {
  trace("failure", { error: String(error) });
  if (page) await capture("failure-current-state");
  throw error;
} finally {
  releaseSecondFailure();
  await persistEvidence();
  trace("cleanup");
  await app?.close();
  provider.closeAllConnections();
  await new Promise(resolve => provider.close(resolve));
  clearTimeout(deadline);
}
console.log(JSON.stringify({ success, attempts, checks: checks.length, captures: captures.length }));
