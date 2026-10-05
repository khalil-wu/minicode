"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");

if (process.env.MINICODE_VIEWPORT_TEST_CHILD === "1") {
  const { app, BrowserWindow, webContents } = require("electron");
  const browser = require("./embedded-browser-manager");
  app.setPath("userData", path.resolve(__dirname, "../output/playwright/minicode-three-batches-20261005/viewport-runtime"));
  app.whenReady().then(async () => {
    const window = new BrowserWindow({ width: 1200, height: 950, show: false });
    browser.init({ getMainWindow: () => window });
    try {
      await browser.create({ id: "viewport-test", conversationId: "viewport-owner", url: "about:blank" });
      const guests = webContents.getAllWebContents().filter((item) => item.id !== window.webContents.id);
      assert.equal(guests.length, 1);
      const guest = guests[0];
      guest.setBackgroundThrottling(false);
      await guest.executeJavaScript('document.head.innerHTML = \'<meta name="viewport" content="width=device-width, initial-scale=1">\';');
      const metrics = (width) => guest.executeJavaScript('new Promise(resolve => { const started = Date.now(); const read = () => { const value = { width: innerWidth, height: innerHeight, mobile: matchMedia("(max-width: 500px)").matches }; if (value.width === ' + width + ' || Date.now() - started > 1500) resolve(value); else setTimeout(read, 10); }; read(); })');
      const bounds = { id: "viewport-test", conversationId: "viewport-owner", x: 0, y: 0, width: 900, height: 850 };
      browser.setBounds({ ...bounds, viewport: { width: 390, height: 844, mobile: true } });
      assert.deepEqual(await metrics(390), { width: 390, height: 844, mobile: true });
      browser.setBounds({ ...bounds, viewport: { width: 844, height: 390, mobile: true } });
      assert.deepEqual(await metrics(844), { width: 844, height: 390, mobile: false });
      browser.setBounds({ ...bounds, viewport: null });
      assert.deepEqual(await metrics(900), { width: 900, height: 850, mobile: false });
      process.stdout.write("Real Electron viewport, rotation and reset passed.\n");
      browser.disposeAll(); window.destroy(); app.exit(0);
    } catch (error) {
      process.stderr.write(String(error.stack || error) + "\n");
      browser.disposeAll(); window.destroy(); app.exit(1);
    }
  });
} else {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  test("native embedded preview uses the requested CSS viewport", async () => {
    const env = { ...process.env, MINICODE_VIEWPORT_TEST_CHILD: "1" };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], { cwd: __dirname, env, windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    const timer = setTimeout(() => child.kill(), 20000);
    const code = await new Promise((resolve) => child.once("exit", resolve));
    clearTimeout(timer);
    assert.equal(code, 0, output);
  });
}
