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
    const server = http.createServer((_request, response) => {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end('<!doctype html><style>html,body{margin:0;background:rgb(17,133,71);color:white;font:32px sans-serif}h1{margin:80px}</style><h1>Actual browser capture</h1>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    manager.init({ getMainWindow: () => window });
    try {
      await window.loadURL("data:text/html,<h1>MiniCode capture host</h1>");
      const url = `http://127.0.0.1:${server.address().port}/`;
      await manager.executeControlCommand({ action: "navigate", conversation_id: "capture-owner", url }, {
        requestPresentation: true,
        navigationAuthorization: { kind: "owned_preview", url, conversation_id: "capture-owner" },
      });
      const target = manager.listTargets("capture-owner")[0];
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
        conversation_id: "capture-owner" }, { signal: interrupted.signal });
      const cancelled = assert.rejects(hiddenCapture, /test cancellation/);
      interrupted.abort(new Error("test cancellation"));
      await cancelled;

      // This is the native presentation performed by BrowserPanel. The host
      // stays unfocused and only a narrow edge lies inside the desktop.
      window.showInactive();
      manager.activate(target.id, "capture-owner");
      assert.equal(view.getVisible(), false);
      const resultPromise = manager.executeControlCommand({ action: "screenshot", target_id: target.id,
        conversation_id: "capture-owner" });
      manager.setBounds({ id: target.id, conversation_id: "capture-owner", x: 0, y: 0, width: 900, height: 600 });
      const result = await resultPromise;
      const buffer = Buffer.from(result.data, "base64");
      const image = nativeImage.createFromBuffer(buffer);
      assert.equal(image.isEmpty(), false);
      assert.ok(buffer.length > 1000);
      assert.ok(result.width >= 900 && result.height >= 600);
      const pixel = image.toBitmap().subarray(0, 3);
      assert.deepEqual(Array.from(pixel), [71, 133, 17]);
      assert.equal(view.webContents.getURL(), url);
      assert.equal(manager.listTargets("capture-owner").length, 1);
      process.stdout.write(`Real same-page compositor capture ${result.width}x${result.height}, ${buffer.length} bytes; hidden wait cancellation and ownership passed.\n`);
      manager.disposeAll(); window.destroy(); server.close(); app.exit(0);
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      manager.disposeAll(); window.destroy(); server.close(); app.exit(1);
    }
  });
} else {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  test("agent browser captures the same presented page's real compositor frame", { timeout: 25000 }, async () => {
    const tempRoot = path.resolve(os.tmpdir());
    const prefix = "minicode-browser-capture-e2e-";
    const profile = fs.mkdtempSync(path.join(tempRoot, prefix));
    const env = { ...process.env, MINICODE_BROWSER_CAPTURE_TEST_CHILD: "1", MINICODE_BROWSER_CAPTURE_TEST_PROFILE: profile };
    delete env.ELECTRON_RUN_AS_NODE;
    const args = process.platform === "win32" ? [] : ["--no-sandbox", "--disable-gpu"];
    const child = spawn(require("electron"), [...args, __filename], { cwd: __dirname, env, windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    const timeout = setTimeout(() => child.kill(), 20000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, output);
      process.stdout.write(output);
    } finally {
      clearTimeout(timeout);
      assert.equal(path.dirname(path.resolve(profile)), tempRoot);
      assert.ok(path.basename(profile).startsWith(prefix));
      fs.rmSync(profile, { recursive: true, force: true });
    }
  });
}
