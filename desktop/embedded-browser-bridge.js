"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const { TextDecoder } = require("node:util");

let manager = null;
let token = "";
let appendDesktopLog = () => {};
let server = null;
let starting = null;
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
    const chunks = [];
    let bytes = 0;
    request.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > maxBytes) {
        reject(new Error("Request body is too large."));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        const body = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(Buffer.concat(chunks, bytes));
        resolve(body ? JSON.parse(body) : {});
      } catch { reject(new Error("Request body must be valid UTF-8 JSON.")); }
    });
    request.on("error", reject);
  });
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(body);
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
  if (starting) return starting;
  if (server) {
    if (!accepting) throw new Error("Embedded browser bridge is stopping");
    return endpoint;
  }
  starting = (async () => {
    accepting = true;
    const active = http.createServer((request, response) => {
      const operation = handleRequest(request, response);
      inFlight.add(operation);
      void operation.finally(() => inFlight.delete(operation));
    });
    server = active;
    try {
      await new Promise((resolve, reject) => {
        active.once("error", reject);
        active.listen(0, "127.0.0.1", () => {
          active.off("error", reject);
          resolve();
        });
      });
      if (!accepting) throw new Error("Embedded browser bridge is stopping");
      endpoint = `http://127.0.0.1:${active.address().port}`;
      appendDesktopLog(`[desktop] embedded browser bridge listening on ${endpoint}`);
      return endpoint;
    } catch (error) {
      const closed = new Promise((resolve) => active.close(() => resolve()));
      active.closeAllConnections();
      await closed;
      if (server === active) server = null;
      endpoint = "";
      accepting = false;
      throw error;
    } finally {
      starting = null;
    }
  })();
  return starting;
}

async function stop() {
  accepting = false;
  if (starting) await starting.catch(() => {});
  const active = server;
  if (!active) return;
  const closed = new Promise((resolve) => active.close(() => resolve()));
  // Stop owns the live HTTP connections, including requests which have not
  // finished sending their body. server.close alone waits for those bodies
  // indefinitely and prevents the desktop quit lifecycle from completing.
  active.closeAllConnections();
  await closed;
  if (inFlight.size) await Promise.allSettled(Array.from(inFlight));
  if (server === active) server = null;
  endpoint = "";
}

module.exports = { init, start, stop };
