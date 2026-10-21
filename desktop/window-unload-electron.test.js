"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

if (process.env.MINICODE_UNLOAD_TEST_CHILD === "1") {
  const { app, screen } = require("electron"), http = require("node:http"), vm = require("node:vm");
  const windows = require("./window-manager"), ipc = require("./ipc-handlers");
  const profile = process.env.MINICODE_UNLOAD_TEST_PROFILE;
  const reading = process.env.MINICODE_UNLOAD_TEST_MODE === "read";
  app.setPath("userData", profile);
  app.whenReady().then(async () => {
    const commits = [], captures = [], resumes = [];
    let closedCount = 0, verified = false, backendStopped = false;
    const runtime = { revision: 0, apiBaseUrl: "", wsBaseUrl: "", runtimeToken: "unload-oracle-token" };
    const source = fs.readFileSync(path.join(__dirname, "../frontend/src.v2/stores/index.ts"), "utf8");
    const unload = source.match(/window.addEventListener\("beforeunload", event => \{[\s\S]*?\n  \}\);/)[0];
    const html = `<!doctype html><title>MiniCode draft unload oracle</title><script>
      let pending = false, text = '', attachment;
      const database = new Promise((resolve, reject) => {
        const request = indexedDB.open('native-unload-drafts', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('drafts');
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const composerRecovery = database;
      const hasPendingEditorDrafts = () => false;
      const hasPendingComposerDrafts = () => pending;
      const useAppStore = { getState: () => ({ composerDraftsReady: true }) };
      // This oracle has editor/composer drafts and no active recording.
      const dictationController = { hasPendingAudio: () => false, settle: async () => {} };
      const flushEditorDrafts = async () => {};
      const flushComposerDrafts = async () => {
        if (!pending) return;
        const db = await database;
        await new Promise((resolve, reject) => {
          const tx = db.transaction('drafts', 'readwrite');
          tx.objectStore('drafts').put({ text, attachment }, 'primary');
          tx.oncomplete = () => { pending = false; resolve(); }; tx.onabort = () => reject(tx.error);
        });
        await fetch('/committed', { method:'POST', body:JSON.stringify({ text, size:attachment.size }) });
      };
      ${unload}
      window.oracle = {
        ready: database,
        prepare(value) { text = value; attachment = new Blob(['x'.repeat(2*1024*1024)], {type:'application/octet-stream'}); pending = true; },
        async read() {
          const db = await database;
          const draft = await new Promise((resolve, reject) => {
            const request = db.transaction('drafts').objectStore('drafts').get('primary');
            request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
          });
          return draft ? { text:draft.text, size:draft.attachment.size } : null;
        }
      };
    </script>`;
    const server = http.createServer((request, response) => {
      if (request.url === "/committed") {
        let body = ""; request.on("data", (chunk) => { body += chunk; });
        request.on("end", () => { commits.push(JSON.parse(body)); response.end("committed"); });
      } else { response.writeHead(200, { "content-type": "text/html; charset=utf-8" }); response.end(html); }
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    // Keep the origin stable for the second process's cold IndexedDB read.
    const origin = reading ? fs.readFileSync(path.join(profile, "oracle-origin"), "utf8") : `http://127.0.0.1:${server.address().port}`;
    if (reading) {
      const port = Number(new URL(origin).port); await new Promise((resolve) => server.close(resolve));
      await new Promise((resolve, reject) => { server.once("error", reject); server.listen(port, "127.0.0.1", resolve); });
    } else fs.writeFileSync(path.join(profile, "oracle-origin"), origin, "utf8");
    runtime.apiBaseUrl = origin; runtime.wsBaseUrl = origin.replace("http:", "ws:");
    windows.init({ frontendDevUrl: origin, preloadFile: path.join(__dirname, "preload.js"),
      onMainWindowCreated(window) { const area = screen.getPrimaryDisplay().workArea; window.setPosition(area.x + area.width - 24, area.y); },
      onMainWindowClosed() { closedCount++; },
    });
    ipc.init({ getWindowForSender: windows.getWindowForSender, getWindowRecord: windows.getWindowRecord,
      getRuntimeConfig: (window) => ({ ...runtime, windowOwnerId: windows.getWindowRecord(window).windowOwnerId,
        windowInstanceId: windows.getWindowRecord(window).windowInstanceId, windowBindingToken: windows.getWindowRecord(window).windowBindingToken }),
      captureUnloadIntent(window) { const intent = windows.captureUnloadIntent(window); captures.push(intent); return intent; },
      resumeUnload(window, id) { resumes.push(id); return windows.resumeUnload(window, id); },
    });
    ipc.registerIpcHandlers();
    const mainSource = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
    const quit = mainSource.match(/app.on\("before-quit", \(event\) => \{[\s\S]*?\n\}\);/)[0];
    const allClosed = mainSource.match(/app.on\("window-all-closed", \(\) => \{[\s\S]*?\n\}\);/)[0];
    const drain = mainSource.slice(mainSource.indexOf("async function drainDesktopResources()"), mainSource.indexOf("async function prepareAutomaticRollback()"));
    vm.runInNewContext(`${drain}\n${quit}\n${allClosed}`, {
      app, windowManager: windows, process, quitCleanupComplete: false, quitCleanupStarted: false, appQuitRequested: false,
      rollbackPreparationInProgress: false, desktopCleanupFailed: false,
      dictationHotkeys: { async dispose() {} },
      generalMaintenance: null, petManager: null, petInitialization: null,
      windowCleanupTasks: new Map(), ptyManager: { async killAllSessions() {} },
      remoteDesktop: { async close() { assert.equal(backendStopped, false); } },
      backendSidecar: { requestBackendStop() {}, async stopBackendSidecar() {
        if (!reading) assert.deepEqual(commits.at(-1), { text: "native-close-content", size: 2 * 1024 * 1024 });
        backendStopped = true; server.close();
      }, getBackendProcess: () => null }, embeddedBrowserBridge: { async stop() {} }, embeddedBrowserManager: { disposeAll() {} },
      appendDesktopLog(message) { throw new Error(message); },
      async createStartupFailureWindow(error) { throw error; }, serializeError: error => error,
    });
    app.on("will-quit", () => {
      fs.writeFileSync(path.join(profile, `unload-${reading ? "read" : "write"}-receipt.json`), JSON.stringify({
        verified, backendStopped, closedCount, actions: captures.map((intent) => intent.action),
        resumedOwnIntents: resumes.length === captures.length && resumes.every((id, index) => id === captures[index].id),
        committed: commits.at(-1),
      }), "utf8");
      assert.equal(verified, true); assert.equal(backendStopped, true); assert.equal(closedCount, 1);
      if (!reading) {
        assert.deepEqual(captures.map((intent) => intent.action), ["reload", "reload", "close"]);
        assert.deepEqual(resumes, captures.map((intent) => intent.id));
      }
      process.stdout.write(reading ? "Cold process read preserved the committed 2 MiB attachment and authored close content.\n"
        : "Real runtime-change reload and Ctrl+R each committed a pending 2 MiB draft without closing; native Quit committed before backend stop and exited normally.\n");
    });
    try {
      const window = await windows.createMainWindow(); window.hide();
      await window.webContents.executeJavaScript("window.oracle.ready.then(() => true)");
      if (reading) {
        assert.deepEqual(await window.webContents.executeJavaScript("window.oracle.read()"), { text: "native-close-content", size: 2 * 1024 * 1024 });
      } else {
        for (const trigger of ["runtime", "keyboard"]) {
          await window.webContents.executeJavaScript(`window.oracle.prepare(${JSON.stringify(trigger + "-reload-content")})`, true);
          const loaded = new Promise((resolve) => window.webContents.once("did-finish-load", resolve));
          if (trigger === "runtime") window.webContents.send("minicode:runtime:changed", { revision: ++runtime.revision });
          else {
            window.show(); window.focus();
            if (!window.isFocused()) await new Promise((resolve) => window.once("focus", resolve));
            window.webContents.sendInputEvent({ type: "keyDown", keyCode: "R", modifiers: ["control"] });
          }
          await loaded;
          assert.equal(closedCount, 0); assert.equal(windows.getMainWindows()[0], window);
          assert.deepEqual(await window.webContents.executeJavaScript("window.oracle.read()"), { text: trigger + "-reload-content", size: 2 * 1024 * 1024 });
          assert.equal(backendStopped, false);
          process.stdout.write(`[unload] ${trigger} reload preserved the native window and committed attachment\n`);
        }
        await window.webContents.executeJavaScript("window.oracle.prepare('native-close-content')", true);
      }
      verified = true;
      app.quit();
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      server.close(); app.exit(1);
    }
  });
} else {
  const { test } = require("node:test"), { spawn } = require("node:child_process");
  test("pending native drafts replay reload or close and survive a cold process", { timeout: 60000 }, async () => {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-native-unload-"));
    let output = "";
    try {
      for (const mode of ["write", "read"]) {
        const env = { ...process.env, MINICODE_UNLOAD_TEST_CHILD: "1", MINICODE_UNLOAD_TEST_PROFILE: profile, MINICODE_UNLOAD_TEST_MODE: mode };
        delete env.ELECTRON_RUN_AS_NODE;
        const child = spawn(require("electron"), [__filename], { cwd: __dirname, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
        child.stdout.on("data", (chunk) => { output += chunk; }); child.stderr.on("data", (chunk) => { output += chunk; });
        const timer = setTimeout(() => child.kill(), 25000);
        try {
          const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", resolve); });
          assert.equal(code, 0, output);
          const receipt = JSON.parse(fs.readFileSync(path.join(profile, `unload-${mode}-receipt.json`), "utf8"));
          assert.equal(receipt.verified, true); assert.equal(receipt.backendStopped, true); assert.equal(receipt.closedCount, 1);
          if (mode === "write") {
            assert.deepEqual(receipt.actions, ["reload", "reload", "close"]);
            assert.equal(receipt.resumedOwnIntents, true);
            assert.deepEqual(receipt.committed, { text: "native-close-content", size: 2 * 1024 * 1024 });
          }
        } finally { clearTimeout(timer); }
      }
      process.stdout.write(output);
    } finally {
      assert.equal(path.dirname(path.resolve(profile)), path.resolve(os.tmpdir()));
      fs.rmSync(profile, { recursive: true, force: true });
    }
  });
}
