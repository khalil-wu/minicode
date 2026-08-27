"use strict";

const crypto = require("node:crypto");
const http = require("node:http");

let manager = null;
let token = "";
let appendDesktopLog = () => {};
let server = null;
let endpoint = "";
let accepting = false;
const inFlight = new Set();

function init(deps = {}) {
  manager = deps.manager || null;
  token = typeof deps.token === "string" ? deps.token : "";
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
}

function tokenMatches(value) {
  const supplied = Buffer.from(String(value || ""));
  const expected = Buffer.from(token);
  return supplied.length === expected.length && expected.length > 0 && crypto.timingSafeEqual(supplied, expected);
}

function readJsonBody(request, maxBytes = 1_000_000) {
  return new Promise((resolve, reject) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
      if (Buffer.byteLength(body, "utf8") > maxBytes) {
        reject(new Error("Request body is too large."));
        request.destroy();
      }
    });
    request.on("end", () => {
      try { resolve(body ? JSON.parse(body) : {}); }
      catch { reject(new Error("Request body must be valid JSON.")); }
    });
    request.on("error", reject);
  });
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(JSON.stringify(payload));
}

async function handleRequest(request, response) {
  if (!accepting) return sendJson(response, 503, { ok: false, error: "Embedded browser bridge is stopping" });
  const auth = String(request.headers.authorization || "");
  const suppliedToken = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!tokenMatches(suppliedToken)) return sendJson(response, 401, { ok: false, error: "Unauthorized" });
  if (request.method === "GET" && request.url === "/v1/health") {
    return sendJson(response, 200, { ok: true, browser: "MiniCode Embedded Browser" });
  }
  if (request.method !== "POST" || request.url !== "/v1/command") {
    return sendJson(response, 404, { ok: false, error: "Not found" });
  }
  try {
    if (!manager?.executeControlCommand) throw new Error("Embedded browser manager is unavailable.");
    const payload = await readJsonBody(request);
    const result = await manager.executeControlCommand(payload);
    return sendJson(response, result?.ok === false ? 400 : 200, result || { ok: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendDesktopLog(`[desktop] embedded browser bridge command failed: ${message}`);
    return sendJson(response, 400, { ok: false, error: message });
  }
}

async function start() {
  if (server) return endpoint;
  accepting = true;
  server = http.createServer((request, response) => {
    const operation = handleRequest(request, response);
    inFlight.add(operation);
    void operation.finally(() => inFlight.delete(operation));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  endpoint = `http://127.0.0.1:${server.address().port}`;
  appendDesktopLog(`[desktop] embedded browser bridge listening on ${endpoint}`);
  return endpoint;
}

async function stop() {
  accepting = false;
  const active = server;
  server = null;
  endpoint = "";
  if (!active) return;
  await new Promise((resolve) => active.close(() => resolve()));
  if (inFlight.size) await Promise.allSettled(Array.from(inFlight));
}

module.exports = { init, start, stop };
