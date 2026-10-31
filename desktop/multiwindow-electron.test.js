"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (process.env.MINICODE_MULTIWINDOW_TEST_CHILD === "1") {
  const { app, Menu, screen, webContents, dialog, shell } = require("electron");
  const http = require("node:http"), vm = require("node:vm");
  const manager = require("./window-manager"), browser = require("./embedded-browser-manager");
  const bridge = require("./embedded-browser-bridge"), terminal = require("./pty-manager"), ipc = require("./ipc-handlers");
  const desktopPreferences = require("./preferences");
  const profile = process.env.MINICODE_MULTIWINDOW_TEST_PROFILE;
  app.setPath("userData", profile);
  desktopPreferences.init(path.join(profile, "preferences.json"), { localeOverride: "zh-CN" });
  // This fixture owns the asynchronous PTY/bridge/server drain, just as
  // main.js owns it during production shutdown. Do not let Electron's
  // default last-window behavior quit before that drain completes.
  app.on("window-all-closed", () => {});
  app.whenReady().then(async () => {
    const closes = new Map();
    app.once("will-quit", () => {
      process.stdout.write("[multiwindow] graceful Electron will-quit reached\n");
    });
    let resolveSecond;
    const secondReady = new Promise((resolve) => { resolveSecond = resolve; });
    const server = http.createServer((_request, response) => {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end("<!doctype html><title>MiniCode owner oracle</title><p>Multiwindow ownership verification</p>");
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const runtime = { apiBaseUrl: origin, wsBaseUrl: origin.replace("http:", "ws:"), runtimeToken: "oracle-runtime-token", revision: 1 };
    manager.init({ frontendDevUrl: origin, preloadFile: path.join(__dirname, "preload.js"),
      onMainWindowCreated(window, record) {
        const area = screen.getPrimaryDisplay().workArea;
        window.setPosition(area.x + area.width - 24, area.y);
        if (record.windowOwnerId === "secondary-1") window.webContents.once("did-finish-load", () => resolveSecond(window));
      },
      onMainWindowClosed(_window, record) {
        bridge.releaseWindow(record.windowInstanceId);
        browser.releaseWindow(record.windowInstanceId);
        closes.set(record.windowInstanceId, terminal.releaseWindow(record.windowInstanceId));
      },
    });
    browser.init({ getWindowByInstanceId: manager.getWindowByInstanceId });
    const nativePty = process.platform === "win32"
      ? require("./windows-pty").createWindowsPty(path.join(__dirname, "windows-sandbox-runtime", "codex.exe"))
      : require("node-pty");
    terminal.init({ pty: nativePty, getWindowByInstanceId: manager.getWindowByInstanceId,
      sanitizedPtyEnv: require("./utils").sanitizedPtyEnv, assertTrustedPath: (value) => value });
    const dictationHotkeys = require("./dictation-hotkeys").createDictationHotkeys({ app,
      getMainWindows: manager.getMainWindows, getFocusedMainWindow: manager.getFocusedMainWindow,
      getFocusedWebContents: () => webContents.getFocusedWebContents() });
    try {
      const mainSource = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
      const sendMenuSource = mainSource.match(/function sendWorkbenchMenuEvent\([\s\S]*?\n\}/)[0];
      const menuSource = mainSource.slice(mainSource.indexOf("const applicationMenuStates = new WeakMap();"), mainSource.indexOf("// Startup failure window"));
      const menuModule = { exports: {} };
      vm.runInNewContext(`${sendMenuSource}\n${menuSource}\nmodule.exports = { buildApplicationMenu, setApplicationMenuState, executeApplicationMenuAction };`, {
        module: menuModule, app, desktopPreferences, Menu, windowManager: manager, process, dialog, shell, webContents,
        embeddedBrowserManager: browser, updater: require("./updater"), dictationHotkeys,
        appendDesktopLog: message => process.stdout.write(message + "\n"),
      });
      const menu = menuModule.exports;
      ipc.init({ getWindowForSender: manager.getWindowForSender, getWindowRecord: manager.getWindowRecord,
        getRuntimeConfig: (window) => ({ ...runtime, windowOwnerId: manager.getWindowRecord(window).windowOwnerId,
          windowInstanceId: manager.getWindowRecord(window).windowInstanceId, windowBindingToken: manager.getWindowRecord(window).windowBindingToken }), embeddedBrowserManager: browser, ptyManager: terminal,
        setApplicationMenuState: menu.setApplicationMenuState, executeApplicationMenuAction: menu.executeApplicationMenuAction,
      });
      ipc.registerIpcHandlers();
      bridge.init({ manager: browser, getWindowRecordByInstanceId: manager.getWindowRecordByInstanceId, token: "oracle-backend-only" });
      const endpoint = await bridge.start();
      const capabilities = new Map();
      const command = (instance, payload) => fetch(`${endpoint}/v1/command`, { method: "POST", headers: { authorization: "Bearer oracle-backend-only" },
        body: JSON.stringify({ ...payload, conversation_id: "same-conversation", window_instance_id: instance, window_binding_token: capabilities.get(instance) }),
      }).then((response) => response.json());
      process.stdout.write("[multiwindow] creating production menu windows\n");
      const primary = await manager.createMainWindow();
      menu.buildApplicationMenu(primary);
      const newWindow = Menu.getApplicationMenu().getMenuItemById("minicode-shortcut-newWindow");
      const locale = desktopPreferences.getPreferences().localeOverride ?? app.getLocale();
      assert.equal(newWindow.label, /^zh(?:[-_](?:cn|sg|hans.*))?$/i.test(locale) ? "新建窗口" : "New Window");
      newWindow.click(newWindow, primary);
      const secondary = await secondReady;
      primary.hide(); secondary.hide();
      assert.equal(manager.getMainWindows().length, 2);
      const a = manager.getWindowRecord(primary), b = manager.getWindowRecord(secondary);
      capabilities.set(a.windowInstanceId, a.windowBindingToken); capabilities.set(b.windowInstanceId, b.windowBindingToken);
      assert.equal(a.windowOwnerId, "primary"); assert.equal(b.windowOwnerId, "secondary-1");
      assert.notEqual(a.windowInstanceId, b.windowInstanceId);
      const readRuntime = (window) => window.webContents.executeJavaScript("({ owner: window.__MINICODE_RUNTIME__.windowOwnerId, instance: window.__MINICODE_RUNTIME__.windowInstanceId })");
      assert.deepEqual(await readRuntime(primary), { owner: "primary", instance: a.windowInstanceId });
      assert.deepEqual(await readRuntime(secondary), { owner: "secondary-1", instance: b.windowInstanceId });
      assert.equal(await secondary.webContents.executeJavaScript("window.__MINICODE_RUNTIME__.windowBindingToken"), b.windowBindingToken);
      const bindingHeaders = { authorization: "Bearer oracle-backend-only" };
      const forged = await fetch(`${endpoint}/v1/window-binding`, { method: "POST", headers: bindingHeaders,
        body: JSON.stringify({ window_instance_id: b.windowInstanceId, window_owner_id: b.windowOwnerId, window_binding_token: a.windowBindingToken }) });
      assert.equal(forged.status, 403);
      const valid = await fetch(`${endpoint}/v1/window-binding`, { method: "POST", headers: bindingHeaders,
        body: JSON.stringify({ window_instance_id: b.windowInstanceId, window_owner_id: b.windowOwnerId, window_binding_token: b.windowBindingToken }) });
      assert.equal(valid.status, 200);
      process.stdout.write("[multiwindow] two renderer instances and binding proofs verified\n");
      for (const window of [primary, secondary]) {
        await window.webContents.executeJavaScript("window.menuEvents = []; window.addEventListener('open-command-menu', () => window.menuEvents.push('open-command-menu'))");
      }
      secondary.focus();
      assert.equal(await primary.webContents.executeJavaScript("window.__MINICODE_RUNTIME__.desktop.menu.execute('commandPalette')"), true);
      assert.deepEqual(await primary.webContents.executeJavaScript("window.menuEvents"), ["open-command-menu"]);
      assert.deepEqual(await secondary.webContents.executeJavaScript("window.menuEvents"), []);
      process.stdout.write("[multiwindow] native menu IPC and renderer event retained their originating window\n");
      const openTab = (window) => window.webContents.executeJavaScript("window.__MINICODE_RUNTIME__.desktop.embeddedBrowser.create({ id: 'same-tab', conversation_id: 'same-conversation', url: 'about:blank' })");
      await openTab(primary); await openTab(secondary);
      const guest = (window) => window.contentView.children.find((view) => view.webContents && view.webContents.id !== window.webContents.id);
      await guest(primary).webContents.executeJavaScript("document.title = 'Primary owned tab'");
      await guest(secondary).webContents.executeJavaScript("document.title = 'Secondary owned tab'");
      secondary.focus();
      const [listA, listB] = await Promise.all([command(a.windowInstanceId, { action: "list_targets" }), command(b.windowInstanceId, { action: "list_targets" })]);
      assert.equal(listA.targets.length, 1); assert.equal(listA.targets[0].title, "Primary owned tab");
      assert.equal(listB.targets.length, 1); assert.equal(listB.targets[0].title, "Secondary owned tab");
      process.stdout.write("[multiwindow] native browser targets stayed with their owners\n");
      const spawnTerminal = (window) => window.webContents.executeJavaScript(`window.__MINICODE_RUNTIME__.desktop.pty.spawn(${JSON.stringify(profile)}, 'same-conversation')`);
      const termA = await spawnTerminal(primary), termB = await spawnTerminal(secondary);
      process.stdout.write("[multiwindow] both native terminal sessions spawned\n");
      assert.equal(await secondary.webContents.executeJavaScript(`window.__MINICODE_RUNTIME__.desktop.pty.write(${JSON.stringify(termA.session_id)}, ${JSON.stringify("foreign-window\r")}, 'same-conversation')`), false);
      assert.equal(await secondary.webContents.executeJavaScript(`window.__MINICODE_RUNTIME__.desktop.pty.snapshot(${JSON.stringify(termA.session_id)}, 100, 'same-conversation')`), null);
      assert.equal(terminal.listActiveSessions().length, 2);
      await new Promise((resolve) => { primary.once("closed", resolve); primary.close(); });
      await closes.get(a.windowInstanceId);
      process.stdout.write("[multiwindow] first native window closed and its PTY acknowledged exit\n");
      assert.equal(manager.getWindowForSender(a.webContents), null);
      assert.equal(manager.getMainWindows()[0], secondary);
      assert.equal(terminal.listActiveSessions().length, 1);
      assert.equal(terminal.listActiveSessions()[0].session_id, termB.session_id);
      assert.equal(browser.listTargets("same-conversation", b.windowInstanceId)[0].title, "Secondary owned tab");
      const rejected = await command(a.windowInstanceId, { action: "list_targets" });
      assert.equal(rejected.ok, false); assert.match(rejected.error, /window binding/);
      const reopened = await manager.createNewWindow(); reopened.hide();
      const c = manager.getWindowRecord(reopened);
      assert.equal(c.windowOwnerId, "primary"); assert.notEqual(c.windowInstanceId, a.windowInstanceId);
      assert.equal(manager.getWindowByInstanceId(a.windowInstanceId), null);
      process.stdout.write("Real Electron new-window menu, runtime bootstrap, native browser bridge, PTY ownership and independent closure passed.\n");
      await Promise.all(manager.getMainWindows().map((window) => new Promise((resolve) => { window.once("closed", resolve); window.close(); })));
      process.stdout.write("[multiwindow] all native windows closed\n");
      await Promise.all(Array.from(closes.values()));
      process.stdout.write("[multiwindow] all owned PTY exits acknowledged\n");
      await dictationHotkeys.dispose();
      await bridge.stop();
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
      process.stdout.write("[multiwindow] bridge and HTTP server closed; requesting normal app.quit\n");
      // Follow normal Electron shutdown after the actual native owners close.
      app.quit();
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      await Promise.all(manager.getMainWindows().map((window) => new Promise((resolve) => { window.once("closed", resolve); window.close(); })));
      await Promise.allSettled(Array.from(closes.values()));
      await dictationHotkeys.dispose();
      await bridge.stop(); server.close(); app.exit(1);
    }
  });
} else {
  const { test } = require("node:test"), { spawn } = require("node:child_process");
  test("real Electron windows keep renderer, browser and terminal ownership through close and reopen", { timeout: 60000 }, async () => {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-multiwindow-e2e-"));
    const env = { ...process.env, MINICODE_MULTIWINDOW_TEST_CHILD: "1", MINICODE_MULTIWINDOW_TEST_PROFILE: profile };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], { env, cwd: __dirname, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (value) => { output += value; }); child.stderr.on("data", (value) => { output += value; });
    const timer = setTimeout(() => child.kill(), 50000);
    try {
      const result = await new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (code, signal) => resolve({ code, signal }));
      });
      assert.deepEqual(result, { code: 0, signal: null }, output);
      assert.match(output, /\[multiwindow\] all owned PTY exits acknowledged/);
      assert.match(output, /\[multiwindow\] bridge and HTTP server closed; requesting normal app.quit/);
      assert.match(output, /\[multiwindow\] graceful Electron will-quit reached/);
      process.stdout.write(output);
    } finally {
      clearTimeout(timer);
      assert.equal(path.dirname(path.resolve(profile)), path.resolve(os.tmpdir()));
      fs.rmSync(profile, { recursive: true, force: true });
    }
  });
}
