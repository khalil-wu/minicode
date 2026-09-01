"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const electronPath = require("electron");

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function waitForExit(child, timeoutMs = 5000) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode, timedOut: false });
  }
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off("exit", onExit);
      resolve({ code: child.exitCode, signal: child.signalCode, timedOut: true });
    }, timeoutMs);
    const onExit = (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, timedOut: false });
    };
    child.once("exit", onExit);
  });
}

function createSmokePage(port) {
  return `<!doctype html>
<html>
  <head><meta charset="utf-8"><title>MiniCode Electron Smoke</title></head>
  <body>
    <main id="status">booting</main>
    <script>
      (async () => {
        const runtime = window.__MINICODE_RUNTIME__;
        const payload = {
          hasRuntime: Boolean(runtime),
          isDesktop: runtime?.desktop?.platformInfo?.isDesktop === true,
          apiBaseUrl: runtime?.apiBaseUrl || "",
          wsBaseUrl: runtime?.wsBaseUrl || "",
          hasRuntimeToken: typeof runtime?.runtimeToken === "string" && runtime.runtimeToken.length >= 16,
          nodeIntegrationBlocked: typeof window.require === "undefined",
          diagnosticsOk: false,
          diagnosticsHasElectron: false,
          embeddedBrowserOk: false,
          selfTrustRejected: false,
          error: "",
        };
        try {
          const browserConversationId = "e2e-browser-conversation";
          console.log("[minicode-smoke] diagnostics.export");
          const diagnostics = await runtime.desktop.diagnostics.export();
          payload.diagnosticsOk = Boolean(diagnostics && diagnostics.path);
          payload.diagnosticsHasElectron = Boolean(diagnostics?.payload?.runtime?.electron);
          console.log("[minicode-smoke] embeddedBrowser.create");
          const browserState = await runtime.desktop.embeddedBrowser.create({
            id: "e2e-browser-tab",
            // This smoke test verifies the trusted IPC surface.  It must not
            // drive a private-network navigation because production correctly
            // requires an interactive user approval for loopback targets.
            // Dedicated manager tests cover that approval boundary.
            url: "about:blank",
            conversationId: browserConversationId,
          });
          console.log("[minicode-smoke] embeddedBrowser.list");
          const browserTabs = await runtime.desktop.embeddedBrowser.list({ conversationId: browserConversationId });
          console.log("[minicode-smoke] embeddedBrowser.setBounds");
          const boundsOk = await runtime.desktop.embeddedBrowser.setBounds({
            id: "e2e-browser-tab",
            x: 20,
            y: 80,
            width: 320,
            height: 220,
            conversationId: browserConversationId,
          });
          console.log("[minicode-smoke] embeddedBrowser.reload");
          const reloadOk = await runtime.desktop.embeddedBrowser.runAction({ id: "e2e-browser-tab", conversationId: browserConversationId, action: "reload" });
          console.log("[minicode-smoke] embeddedBrowser.close");
          const closeOk = await runtime.desktop.embeddedBrowser.close({ id: "e2e-browser-tab", conversationId: browserConversationId });
          payload.embeddedBrowserOk = browserState?.id === "e2e-browser-tab"
            && browserTabs.some((tab) => tab.id === "e2e-browser-tab")
            && boundsOk && reloadOk && closeOk;
          console.log("[minicode-smoke] workspace.trust");
          const trustResult = await runtime.desktop.trustWorkspace("${os.homedir().replace(/\\/g, "\\\\")}");
          payload.workspaceTrustResult = trustResult;
          payload.selfTrustRejected = trustResult === "";
        } catch (error) {
          payload.error = String(error && error.message ? error.message : error);
        }
        console.log("[minicode-smoke] report");
        document.getElementById("status").textContent = "done";
        await fetch("http://127.0.0.1:${port}/e2e-done", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        setTimeout(() => {
          runtime.desktop.windowControls.close().catch(() => undefined);
        }, 0);
      })();
    </script>
  </body>
</html>`;
}

test("Electron app boots real BrowserWindow with preload runtime and guarded IPC", { timeout: 30000 }, async () => {
  let resolveDone;
  let rejectDone;
  const done = new Promise((resolve, reject) => {
    resolveDone = resolve;
    rejectDone = reject;
  });
  const requests = [];
  const requestPaths = [];

  const server = http.createServer((req, res) => {
    requestPaths.push(`${req.method || "GET"} ${req.url || ""}`);
    if (req.url === "/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ status: "ok", ready: true }));
      return;
    }
    if (req.url === "/" || req.url === "/index.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(createSmokePage(server.address().port));
      return;
    }
    if (req.url === "/embedded") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end("<!doctype html><title>Embedded Browser Smoke</title><p>embedded browser ready</p>");
      return;
    }
    if (req.url === "/e2e-done" && req.method === "POST") {
      let body = "";
      req.setEncoding("utf8");
      req.on("data", (chunk) => {
        body += chunk;
      });
      req.on("end", () => {
        try {
          const payload = JSON.parse(body);
          requests.push(payload);
          resolveDone(payload);
          res.writeHead(204);
          res.end();
        } catch (error) {
          rejectDone(error);
          res.writeHead(400);
          res.end(String(error));
        }
      });
      return;
    }
    res.writeHead(404);
    res.end("not found");
  });

  const port = await listen(server);
  const baseUrl = `http://127.0.0.1:${port}`;
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-electron-e2e-"));
  // Chromium refuses to start as root unless its sandbox is explicitly
  // disabled. CI smoke runs may use a root container; regular user sessions
  // keep the sandbox enabled.
  const electronArgs = ["--disable-gpu"];
  if (typeof process.getuid === "function" && process.getuid() === 0) {
    electronArgs.push("--no-sandbox");
  }
  electronArgs.push(__dirname);
  const child = spawn(electronPath, electronArgs, {
    cwd: __dirname,
    env: {
      ...process.env,
      ELECTRON_ENABLE_LOGGING: "1",
      MINICODE_SKIP_BACKEND: "1",
      MINICODE_BACKEND_STARTUP_TIMEOUT_MS: "5000",
      MINICODE_API_BASE_URL: baseUrl,
      MINICODE_WS_BASE_URL: `ws://127.0.0.1:${port}`,
      MINICODE_FRONTEND_URL: `${baseUrl}/`,
      MINICODE_RUNTIME_TOKEN: "e2e-runtime-token-1234567890",
      MINICODE_USER_DATA_DIR: userDataDir,
      MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  try {
    let timeoutId;
    const timeout = new Promise((_, reject) => {
      timeoutId = setTimeout(() => reject(new Error("Electron smoke timed out")), 25000);
    });
    const payload = await Promise.race([done, timeout]);
    clearTimeout(timeoutId);

    assert.equal(payload.hasRuntime, true);
    assert.equal(payload.isDesktop, true);
    assert.equal(payload.apiBaseUrl, baseUrl);
    assert.equal(payload.wsBaseUrl, `ws://127.0.0.1:${port}`);
    assert.equal(payload.hasRuntimeToken, true);
    assert.equal(payload.nodeIntegrationBlocked, true);
    assert.equal(payload.diagnosticsOk, true);
    assert.equal(payload.diagnosticsHasElectron, true);
    assert.equal(payload.embeddedBrowserOk, true);
    assert.equal(
      payload.selfTrustRejected,
      true,
      `renderer workspace trust result: ${JSON.stringify(payload)}`,
    );
    assert.equal(payload.error, "");
    assert.equal(requests.length, 1);
  } catch (error) {
    error.message += `\n--- requests ---\n${requestPaths.join("\n")}`;
    error.message += `\n--- child ---\nexitCode=${child.exitCode} signalCode=${child.signalCode}`;
    error.message += `\n--- electron stdout ---\n${stdout}\n--- electron stderr ---\n${stderr}`;
    throw error;
  } finally {
    const exit = await waitForExit(child, 3000);
    if (exit.timedOut) {
      child.kill("SIGKILL");
      await waitForExit(child, 3000);
    }
    await closeServer(server);
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
});
