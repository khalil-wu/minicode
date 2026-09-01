import { spawn } from "node:child_process";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const host = "127.0.0.1";
const configuredPort = process.env.MINICODE_E2E_PORT?.trim();

function allocateLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

const port = configuredPort ? Number(configuredPort) : await allocateLoopbackPort();
if (!Number.isInteger(port) || port <= 0 || port > 65_535) {
  throw new Error(`Invalid MINICODE_E2E_PORT: ${configuredPort}`);
}
const baseUrl = `http://${host}:${port}/`;
const testFile = process.argv[2] ?? "tests/e2e/electron-multi-agent.spec.ts";
const playwrightArgs = process.argv.slice(3);
const viteBin = path.join(frontendRoot, "node_modules", "vite", "bin", "vite.js");
const playwrightCli = path.join(frontendRoot, "node_modules", "@playwright", "test", "cli.js");

const noProxy = new Set(
  `${process.env.NO_PROXY ?? ""},${process.env.no_proxy ?? ""}`
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
for (const value of [host, "localhost", "::1"]) noProxy.add(value);

function waitForServer(timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    let lastError = null;
    const probe = () => {
      const request = http.get(baseUrl, { headers: { connection: "close" } }, (response) => {
        response.resume();
        if ((response.statusCode ?? 500) < 500) {
          resolve();
          return;
        }
        lastError = new Error(`Vite readiness returned HTTP ${response.statusCode}`);
        retry();
      });
      request.once("error", (error) => {
        lastError = error;
        retry();
      });
      request.setTimeout(1_000, () => {
        request.destroy();
        lastError = new Error("Vite readiness probe timed out");
        retry();
      });
    };
    const retry = () => {
      if (Date.now() >= deadline) {
        reject(lastError ?? new Error("Timed out waiting for Vite"));
        return;
      }
      setTimeout(probe, 100);
    };
    probe();
  });
}

const env = {
  ...process.env,
  NO_PROXY: Array.from(noProxy).join(","),
  no_proxy: Array.from(noProxy).join(","),
};
const vite = spawn(process.execPath, [viteBin, "--host", host, "--port", String(port), "--strictPort"], {
  cwd: frontendRoot,
  env,
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});
let viteStdout = "";
const viteReady = new Promise((resolve) => {
  vite.stdout.on("data", (chunk) => {
    const text = chunk.toString();
    process.stdout.write(`[vite] ${chunk}`);
    viteStdout = `${viteStdout}${text}`.slice(-8_192);
    if (/Local[\s\S]{0,200}http:\/\//.test(viteStdout)) resolve();
  });
});
vite.stderr.on("data", (chunk) => process.stderr.write(`[vite] ${chunk}`));
const viteExit = new Promise((_, reject) => {
  vite.once("exit", (code, signal) => {
    reject(new Error(`Vite exited before readiness (code=${code ?? "null"}, signal=${signal ?? "null"})`));
  });
});

let runner;
try {
  // A stale service can answer the HTTP probe with 200 while the Vite child
  // fails with EADDRINUSE. Require this exact child to announce readiness
  // before probing HTTP, so an old process can never impersonate the server
  // owned by this test invocation.
  await Promise.race([viteReady, viteExit]);
  await waitForServer();
  runner = spawn(process.execPath, [
    playwrightCli,
    "test",
    testFile,
    "--config=playwright.config.ts",
    "--workers=1",
    ...playwrightArgs,
  ], {
    cwd: frontendRoot,
    env: {
      ...env,
      MINICODE_E2E_EXTERNAL_SERVER: "1",
      MINICODE_E2E_PORT: String(port),
    },
    stdio: "inherit",
    windowsHide: true,
  });
  const code = await new Promise((resolve, reject) => {
    runner.once("error", reject);
    runner.once("exit", (exitCode, signal) => resolve(exitCode ?? (signal ? 1 : 0)));
  });
  process.exitCode = Number(code);
} finally {
  if (runner && !runner.killed) runner.kill();
  if (!vite.killed) vite.kill();
}
