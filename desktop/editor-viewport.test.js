"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (process.env.MINICODE_VIEWPORT_TEST_CHILD === "1") {
  const { app, BrowserWindow, webContents } = require("electron");
  app.setPath("userData", path.resolve(process.env.MINICODE_VIEWPORT_TEST_USER_DATA_DIR));
  const browser = require("./embedded-browser-manager");
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
    const tempRoot = path.resolve(os.tmpdir());
    const profilePrefix = "minicode-editor-viewport-e2e-";
    const userDataDir = fs.mkdtempSync(path.join(tempRoot, profilePrefix));
    const env = { ...process.env, MINICODE_VIEWPORT_TEST_CHILD: "1", MINICODE_VIEWPORT_TEST_USER_DATA_DIR: userDataDir };
    delete env.ELECTRON_RUN_AS_NODE;
    // Match electron-smoke's isolated test-process launch on Linux CI, whose
    // npm-installed Electron has no root-owned SUID sandbox helper.
    const args = ["--disable-gpu"];
    if (process.platform !== "win32") args.push("--no-sandbox");
    args.push(__filename);
    const child = spawn(require("electron"), args, { cwd: __dirname, env, windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    const timer = setTimeout(() => child.kill(), 20000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, output);
    } finally {
      clearTimeout(timer);
      const profile = path.resolve(userDataDir);
      assert.equal(path.dirname(profile), tempRoot);
      assert.ok(path.basename(profile).startsWith(profilePrefix));
      fs.rmSync(profile, { recursive: true, force: true });
    }
  });
}
