"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (process.env.MINICODE_BROWSER_CAPTURE_TEST_CHILD === "1") {
  const { app, BrowserWindow, nativeImage, screen } = require("electron");
  const http = require("node:http");
  const manager = require("./embedded-browser-manager");
  app.setPath("userData", process.env.MINICODE_BROWSER_CAPTURE_TEST_PROFILE);
  app.whenReady().then(async () => {
    const area = screen.getPrimaryDisplay().workArea;
    const window = new BrowserWindow({ width: 1000, height: 750, show: false,
      x: area.x + area.width - 24, y: area.y, skipTaskbar: true });
    let receiveNativePost;
    const nativePost = new Promise(resolve => { receiveNativePost = resolve; });
    const server = http.createServer((request, response) => {
      if (request.url === "/agent-download") {
        response.writeHead(200, { "content-type": "text/plain", "content-disposition": 'attachment; filename="original-download.txt"' });
        response.end("original native download bytes");
        return;
      }
      if (request.url === "/native-post") {
        let body = "";
        request.on("data", chunk => { body += chunk; });
        request.on("end", () => {
          response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
          response.end("<!doctype html><h1>Native POST destination</h1>");
          receiveNativePost({ method: request.method, body, contentType: request.headers["content-type"] });
        });
        return;
      }
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end('<!doctype html><style>html,body{margin:0;background:rgb(17,133,71);color:white;font:32px sans-serif}h1{margin:80px}</style><h1>Actual browser capture</h1>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    manager.init({ getWindowByInstanceId: () => window });
    try {
      await window.loadURL("data:text/html,<h1>MiniCode capture host</h1>");
      const url = `http://127.0.0.1:${server.address().port}/`;
      await manager.executeControlCommand({ action: "navigate", conversation_id: "capture-owner", url, window_instance_id: "fixture-window" }, {
        requestPresentation: true,
        navigationAuthorization: { kind: "owned_preview", url, conversation_id: "capture-owner" },
      });
      process.stdout.write("[capture] owned page navigation complete\n");
      const target = manager.listTargets("capture-owner", "fixture-window")[0];
      const view = window.contentView.children.find((child) => child.webContents?.id !== window.webContents.id);
      const metrics = await view.webContents.executeJavaScript("({width:innerWidth,height:innerHeight})");
      const host = window.getContentBounds();
      assert.deepEqual(view.getBounds(), { x: 0, y: 0, width: host.width, height: host.height });
      // Native DIP-to-pixel rounding may move CSS dimensions by one pixel.
      assert.ok(Math.abs(metrics.width - host.width) <= 1);
      assert.ok(Math.abs(metrics.height - host.height) <= 1);
      assert.equal(view.getVisible(), false);
      assert.equal(window.isVisible(), false);
      const interrupted = new AbortController();
      const hiddenCapture = manager.executeControlCommand({ action: "screenshot", target_id: target.id,
        conversation_id: "capture-owner", window_instance_id: "fixture-window" }, { signal: interrupted.signal });
      const cancelled = assert.rejects(hiddenCapture, /test cancellation/);
      interrupted.abort(new Error("test cancellation"));
      await cancelled;
      process.stdout.write("[capture] hidden screenshot waiter cancelled\n");

      // This is the native presentation performed by BrowserPanel. The host
      // stays unfocused and only a narrow edge lies inside the desktop.
      window.showInactive();
      manager.activate(target.id, "capture-owner", "fixture-window");
      assert.equal(view.getVisible(), false);
      const resultPromise = manager.executeControlCommand({ action: "screenshot", target_id: target.id,
        conversation_id: "capture-owner", window_instance_id: "fixture-window" });
      manager.setBounds({ id: target.id, conversation_id: "capture-owner", x: 0, y: 0, width: 900, height: 600, window_instance_id: "fixture-window" });
      process.stdout.write("[capture] owner activated and native viewport presented\n");
      const result = await resultPromise;
      const buffer = Buffer.from(result.data, "base64");
      const image = nativeImage.createFromBuffer(buffer);
      assert.equal(image.isEmpty(), false);
      assert.ok(buffer.length > 1000);
      assert.ok(result.width >= 900 && result.height >= 600);
      const pixel = image.toBitmap().subarray(0, 3);
      assert.deepEqual(Array.from(pixel), [71, 133, 17]);
      assert.equal(view.webContents.getURL(), url);
      assert.equal(manager.listTargets("capture-owner", "fixture-window").length, 1);
      process.stdout.write(`Real same-page compositor capture ${result.width}x${result.height}, ${buffer.length} bytes; hidden wait cancellation and ownership passed.\n`);
      process.stdout.write("[capture] submitting original native POST form\n");
      await view.webContents.executeJavaScript(`{
        const form = document.createElement('form'); form.action = '/native-post'; form.method = 'POST'; form.target = '_blank'; form.rel = 'opener';
        const input = document.createElement('input'); input.name = 'original'; input.value = 'native value'; form.append(input);
        document.body.append(form); form.submit();
      }`, true);
      assert.deepEqual(await nativePost, { method: "POST", body: "original=native+value", contentType: "application/x-www-form-urlencoded" });
      const child = window.contentView.children.find(candidate => candidate !== view && candidate.webContents?.id !== window.webContents.id);
      if (child.webContents.isLoading()) await new Promise(resolve => child.webContents.once("did-finish-load", resolve));
      assert.equal(await child.webContents.executeJavaScript("window.opener !== null"), true);
      assert.equal(child.webContents.getURL(), new URL("/native-post", url).href);
      assert.equal(manager.listTargets("capture-owner", "fixture-window").length, 2);
      assert.equal(manager.listTargets("another-owner", "fixture-window").length, 0);
      process.stdout.write("[capture] native child POST body, original opener and conversation owner preserved\n");
      const downloadDirectory = path.join(app.getPath("userData"), "owned-downloads");
      fs.mkdirSync(downloadDirectory);
      manager.setBrowserSettings({ downloadDirectory, downloadPolicy: "ask" });
      const agentControl = { id: "capture-native-agent-owner", turnId: "capture-native-turn" };
      const downloadUrl = new URL("/agent-download", url).href;
      await manager.executeControlCommand({ action: "allow_download", target_id: target.id, conversation_id: "capture-owner",
        window_instance_id: "fixture-window", url: downloadUrl }, { agentControl });
      const downloadDone = new Promise((resolve, reject) => view.webContents.session.once("will-download", (event, item, contents) => {
        if (event.defaultPrevented) { reject(new Error("The exact native download URL was not admitted")); return; }
        assert.equal(contents, view.webContents);
        item.once("done", (_done, state) => resolve({ item, state }));
      }));
      process.stdout.write("[capture] exact native HTTP download authorized; starting original anchor\n");
      const [, downloaded] = await Promise.all([manager.executeControlCommand({ action: "evaluate", target_id: target.id, conversation_id: "capture-owner", window_instance_id: "fixture-window",
        expression: `(() => { const link=document.createElement('a'); link.href=${JSON.stringify(downloadUrl)}; link.download='original-download.txt'; document.body.append(link); link.click(); return true; })()` }, { agentControl }), downloadDone]);
      assert.equal(downloaded.state, "completed");
      assert.equal(fs.readFileSync(downloaded.item.getSavePath(), "utf8"), "original native download bytes");
      const ownedDownloads = await manager.executeControlCommand({ action: "list_downloads", conversation_id: "capture-owner", window_instance_id: "fixture-window" });
      assert.equal(ownedDownloads.value.length, 1); assert.equal(ownedDownloads.value[0].isUserInitiated, false);
      assert.equal(ownedDownloads.value[0].path, downloaded.item.getSavePath());
      process.stdout.write("[capture] original native DownloadItem completed with exact URL permission, bytes, save path and owner\n");
      await manager.executeControlCommand({ action: "set_blob_download_origin", target_id: target.id, conversation_id: "capture-owner",
        window_instance_id: "fixture-window", origin: new URL(url).origin }, { agentControl });
      const blobDone = new Promise((resolve, reject) => view.webContents.session.once("will-download", (event, item) => {
        if (event.defaultPrevented) { reject(new Error("The original main-frame blob event did not admit the native download")); return; }
        item.once("done", (_done, state) => resolve({ item, state }));
      }));
      process.stdout.write("[capture] original main-frame blob origin authorized; starting dynamic blob anchor\n");
      const [, blob] = await Promise.all([manager.executeControlCommand({ action: "evaluate", target_id: target.id, conversation_id: "capture-owner", window_instance_id: "fixture-window",
        expression: "(() => { const link=document.createElement('a'); link.href=URL.createObjectURL(new Blob(['original main-frame blob bytes'],{type:'text/plain'})); link.download='original-blob.txt'; document.body.append(link); link.click(); return true; })()" }, { agentControl }), blobDone]);
      assert.equal(blob.state, "completed"); assert.equal(fs.readFileSync(blob.item.getSavePath(), "utf8"), "original main-frame blob bytes");
      assert.equal(manager.releaseAgentControl({ conversation_id: "capture-owner", window_instance_id: "fixture-window", control: agentControl }), 1);
      assert.equal(view.webContents.debugger.isAttached(), false);
      process.stdout.write("[capture] original main-frame Page.downloadWillBegin produced and consumed the dynamic blob permission\n");
      manager.disposeAll(); window.destroy(); server.close(); app.exit(0);
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      manager.disposeAll(); window.destroy(); server.close(); app.exit(1);
    }
  });
} else {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  const { once } = require("node:events");
  async function waitForClose(closed, milliseconds) {
    let timer;
    try {
      return await Promise.race([
        closed,
        new Promise(resolve => { timer = setTimeout(() => resolve({ timedOut: true }), milliseconds); }),
      ]);
    } finally { clearTimeout(timer); }
  }
  test("agent browser captures the same presented page's real compositor frame", { timeout: 25000 }, async () => {
    const tempRoot = path.resolve(os.tmpdir());
    const prefix = "minicode-browser-capture-e2e-";
    const profile = fs.mkdtempSync(path.join(tempRoot, prefix));
    const env = { ...process.env, MINICODE_BROWSER_CAPTURE_TEST_CHILD: "1", MINICODE_BROWSER_CAPTURE_TEST_PROFILE: profile };
    delete env.ELECTRON_RUN_AS_NODE;
    const args = process.platform === "win32" ? [] : ["--no-sandbox", "--disable-gpu"];
    const child = spawn(require("electron"), [...args, __filename], { cwd: __dirname, env, windowsHide: true });
    const closed = once(child, "close").then(([code, signal]) => ({ code, signal, timedOut: false }));
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; process.stdout.write(chunk); });
    child.stderr.on("data", (chunk) => { output += chunk; process.stderr.write(chunk); });
    let timeout;
    let exit;
    let passed = false;
    try {
      exit = await Promise.race([closed, new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("Electron browser capture timed out after 20000ms")), 20000);
      })]);
      assert.equal(exit.code, 0, output);
      assert.equal(exit.signal, null, output);
      passed = true;
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      throw error;
    } finally {
      clearTimeout(timeout);
      if (!exit) {
        child.kill("SIGTERM");
        exit = await waitForClose(closed, 1000);
        if (exit.timedOut) {
          child.kill("SIGKILL");
          exit = await waitForClose(closed, 1000);
        }
      }
      if (!passed) process.stderr.write(`[capture] failed profile retained: ${profile}\n`);
      assert.equal(exit.timedOut, false,
        `Owned Electron process or its stdio are still open; profile retained: ${profile}`);
      if (passed) {
        assert.equal(path.dirname(path.resolve(profile)), tempRoot);
        assert.ok(path.basename(profile).startsWith(prefix));
        fs.rmSync(profile, { recursive: true, force: true });
      }
    }
  });
}
