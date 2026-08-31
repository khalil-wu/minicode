import { defineConfig } from "@playwright/test";

const host = "127.0.0.1";
const port = Number(process.env.MINICODE_E2E_PORT ?? "43173");
const externalServer = process.env.MINICODE_E2E_EXTERNAL_SERVER === "1";

// The desktop E2E stack is entirely loopback-owned. Keep Playwright's
// readiness probe, Electron, and the mock WebSocket transport off any ambient
// HTTP(S) proxy; otherwise a proxy can answer the local readiness URL (for
// example with 502) or retain local sockets before the worker starts.
const loopbackNoProxy = new Set(
  `${process.env.NO_PROXY ?? ""},${process.env.no_proxy ?? ""}`
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
for (const value of [host, "localhost", "::1"]) loopbackNoProxy.add(value);
process.env.NO_PROXY = Array.from(loopbackNoProxy).join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: `http://${host}:${port}`,
    headless: true,
    screenshot: "only-on-failure",
  },
  ...(externalServer ? {} : { webServer: {
    // Launch Vite directly. On Windows, nesting `npm run dev` under
    // Playwright's web-server process can leave the npm/cmd wrapper alive
    // after Vite is ready, preventing the Electron worker from starting.
    // Playwright should own the actual long-lived server process.
    command: `node ./node_modules/vite/bin/vite.js --host ${host} --port ${port} --strictPort`,
    // This host has an ambient proxy and unreliable dual-stack readiness
    // probes. Vite's own authoritative startup line avoids mistaking a proxy
    // response or a half-open local socket for readiness.
    wait: { stdout: /Local:\s+http:\/\// },
    stdout: "pipe",
    timeout: 30000,
  } }),
});
