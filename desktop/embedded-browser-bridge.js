"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const { TextDecoder } = require("node:util");
const { bridgeBindTarget } = require("./backend-host-contract");

let manager = null;
let getWindowRecordByInstanceId = () => null;
let token = "";
let appendDesktopLog = () => {};
let server = null;
let starting = null;
let endpoint = "";
let accepting = false;
let configuredEndpoint = "";
const inFlight = new Set();
const operations = new Map();
const remoteHosts = new Map();

function init(deps = {}) {
  manager = deps.manager || null;
  if (typeof deps.getWindowRecordByInstanceId === "function") getWindowRecordByInstanceId = deps.getWindowRecordByInstanceId;
  token = typeof deps.token === "string" ? deps.token : "";
  configuredEndpoint = typeof deps.endpoint === "string" ? deps.endpoint.trim() : "";
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
}

function tokenMatches(value, expectedToken = token) {
  const supplied = Buffer.from(String(value || ""));
  const expected = Buffer.from(expectedToken);
  return supplied.length === expected.length && expected.length > 0 && crypto.timingSafeEqual(supplied, expected);
}

function assertWindowBinding(payload) {
  const record = getWindowRecordByInstanceId(payload.window_instance_id);
  const supplied = Buffer.from(typeof payload.window_binding_token === "string" ? payload.window_binding_token : "");
  const expected = Buffer.from(record?.windowBindingToken || "");
  if (!record || record.window.isDestroyed() || !expected.length || supplied.length !== expected.length
    || !crypto.timingSafeEqual(supplied, expected)
    || (payload.window_owner_id !== undefined && payload.window_owner_id !== record.windowOwnerId)) {
    const error = new Error("Desktop window binding is invalid or no longer active.");
    error.code = "ERR_WINDOW_BINDING";
    throw error;
  }
  return record;
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
    request.on("aborted", () => reject(new Error("Browser request disconnected before admission.")));
  });
}

function sendJson(response, status, payload) {
  if (response.destroyed) return;
  const body = JSON.stringify(payload);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(body);
}

function operationReceipt(operation) {
  const pending = !operation.settled;
  return {
    resource_kind: "browser", resource_id: operation.id, conversation_id: operation.owner,
    reason: operation.reason || "completed", requested: operation.controller.signal.aborted,
    acknowledged: true, completed: !pending, pending: pending ? 1 : 0,
    retry_safe: !pending && !operation.submitted, manual_recovery_required: pending,
    execution_outcome: pending ? "uncertain" : operation.status,
    submitted: operation.submitted,
  };
}

function createOperation(id, owner, windowInstanceId, windowBindingToken) {
  const operation = { id, owner, windowInstanceId, windowBindingToken, controller: new AbortController(), started: false,
    remoteHostId: getWindowRecordByInstanceId(windowInstanceId)?.remoteHostId || null,
    submitted: false, settled: false, status: "queued", reason: "", promise: null };
  operations.set(id, operation);
  return operation;
}

function cancelOperation(operation, reason) {
  operation.reason = reason;
  operation.controller.abort(new Error(`Browser operation ${reason}.`));
  if (!operation.started) {
    operation.settled = true;
    operation.status = reason;
  }
}

function ownedOperation(payload) {
  const operation = operations.get(payload.operation_id);
  if (operation && (operation.owner !== payload.conversation_id || operation.windowInstanceId !== payload.window_instance_id)) {
    throw new Error("Browser operation belongs to another conversation or window.");
  }
  if (operation) {
    const supplied = Buffer.from(typeof payload.window_binding_token === "string" ? payload.window_binding_token : "");
    const expected = Buffer.from(operation.windowBindingToken);
    if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) {
      const error = new Error("Desktop window binding does not own this browser operation.");
      error.code = "ERR_WINDOW_BINDING";
      throw error;
    }
  }
  return operation;
}

function backendNavigationAuthorization(payload) {
  const authorization = payload.navigation_authorization;
  if (authorization == null) return undefined;
  if (payload.action !== "navigate" || authorization.kind !== "owned_preview"
    || typeof payload.operation_id !== "string" || !payload.operation_id
    || authorization.url !== payload.url || authorization.conversation_id !== payload.conversation_id
    || authorization.operation_id !== payload.operation_id
    || typeof authorization.session_id !== "string" || !authorization.session_id
    || typeof authorization.preview_id !== "string" || !authorization.preview_id
    || !["bypass", "auto", "confirm", "plan"].includes(authorization.permission_mode)
    || new URL(authorization.preview_url).origin !== new URL(payload.url).origin) {
    throw new Error("Owned preview navigation authorization does not match this browser operation.");
  }
  return { ...authorization };
}

async function executeOwnedCommand(payload, response) {
  const id = payload.operation_id || crypto.randomUUID();
  const existing = ownedOperation({ ...payload, operation_id: id });
  if (existing) return { ok: false, status: existing.status,
    error: "This browser operation was already admitted; inspect its recorded outcome instead of executing it again.",
    cleanup_receipt: operationReceipt(existing) };
  // Only the token-authenticated backend bridge may supply this execution
  // option. Renderer/manual navigation payloads never become authority.
  const navigationAuthorization = backendNavigationAuthorization(payload);
  const operation = createOperation(id, payload.conversation_id, payload.window_instance_id, payload.window_binding_token);
  operation.promise = Promise.resolve().then(async () => {
    operation.controller.signal.throwIfAborted();
    operation.started = true;
    operation.status = "running";
    return manager.executeControlCommand(payload, {
      signal: operation.controller.signal,
      navigationAuthorization,
      requestPresentation: true,
      onSubmitted() { operation.submitted = true; },
    });
  }).then((result) => {
    operation.status = result?.ok === false ? "failed" : "completed";
    return result;
  }, (error) => {
    operation.status = operation.controller.signal.aborted ? operation.reason : "failed";
    return { ok: false, status: operation.status, error: error instanceof Error ? error.message : String(error) };
  }).finally(() => {
    operation.settled = true;
    operation.promise = null;
    if (!accepting) operations.delete(id);
  });
  const timeoutMs = payload.operation_timeout_ms ?? 30000;
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) {
    cancelOperation(operation, "cancelled");
    throw new Error("operation_timeout_ms must be between 1 and 30000.");
  }
  const timer = setTimeout(() => cancelOperation(operation, "timeout"), timeoutMs);
  if (response.destroyed) cancelOperation(operation, "cancelled");
  const disconnected = () => { if (!response.writableEnded) cancelOperation(operation, "cancelled"); };
  response.once("close", disconnected);
  const signal = operation.controller.signal;
  let aborted;
  const interrupted = new Promise((resolve) => {
    aborted = () => resolve({ ok: false, status: operation.reason,
      error: `Browser operation ${operation.reason}; submitted page actions may still complete.`,
      cleanup_receipt: operationReceipt(operation) });
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) aborted();
  });
  try {
    const result = await Promise.race([operation.promise, interrupted]);
    return { ...result, cleanup_receipt: operationReceipt(operation) };
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", aborted);
    response.off("close", disconnected);
    if (operation.settled) operation.promise = null;
  }
}

async function handleRequest(request, response) {
  if (!accepting) return sendJson(response, 503, { ok: false, error: "Embedded browser bridge is stopping" });
  const auth = String(request.headers.authorization || "");
  const suppliedToken = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  const remote = [...remoteHosts.entries()].find(([_id, host]) => tokenMatches(suppliedToken, host.ready.bridge_token));
  if (!remote && !tokenMatches(suppliedToken)) return sendJson(response, 401, { ok: false, error: "Unauthorized" });
  const remoteHostId = remote?.[0] || null;
  if (request.method === "GET" && request.url === "/v1/health") {
    return sendJson(response, 200, { ok: true, browser: "MiniCode Embedded Browser" });
  }
  if (request.method !== "POST" || !["/v1/command", "/v1/operation", "/v1/window-binding"].includes(request.url)) {
    return sendJson(response, 404, { ok: false, error: "Not found" });
  }
  try {
    let payload = await readJsonBody(request);
    const binding = request.url === "/v1/operation" ? null : assertWindowBinding(payload);
    const owner = binding || ownedOperation(payload) || assertWindowBinding(payload);
    if ((owner.remoteHostId || null) !== remoteHostId) {
      const error = new Error("Desktop bridge token belongs to a different host");
      error.code = "ERR_WINDOW_BINDING";
      throw error;
    }
    if (request.url === "/v1/window-binding") {
      if (payload.window_owner_id !== binding.windowOwnerId) throw new Error("Desktop window owner does not match its binding.");
      return sendJson(response, 200, { ok: true, window_instance_id: binding.windowInstanceId, window_owner_id: binding.windowOwnerId });
    }
    if (!manager?.executeControlCommand) throw new Error("Embedded browser manager is unavailable.");
    if (typeof payload.window_instance_id !== "string" || !payload.window_instance_id
      || typeof payload.conversation_id !== "string" || !payload.conversation_id) {
      throw new Error("Browser command requires a conversation and window instance owner.");
    }
    if (payload.operation_id != null && (typeof payload.operation_id !== "string" || !payload.operation_id
      || typeof payload.conversation_id !== "string" || !payload.conversation_id)) {
      throw new Error("Browser operation requires a string id and conversation owner.");
    }
    if (request.url === "/v1/operation") {
      if (!payload.operation_id || !payload.conversation_id) throw new Error("Browser operation requires its id and conversation owner.");
      let operation = ownedOperation(payload);
      if (!operation) assertWindowBinding(payload);
      if (payload.action === "cancel") {
        operation ||= createOperation(payload.operation_id, payload.conversation_id, payload.window_instance_id, payload.window_binding_token);
        cancelOperation(operation, payload.reason === "timeout" ? "timeout" : "cancelled");
      } else if (!["status", "wait"].includes(payload.action)) {
        throw new Error("Unsupported browser operation control.");
      }
      if (!operation) return sendJson(response, 404, { ok: false, error: "Browser operation not found." });
      if (payload.action === "wait" && operation.promise && !response.destroyed) {
        let disconnected;
        const closed = new Promise((resolve) => { disconnected = resolve; response.once("close", resolve); });
        try { await Promise.race([operation.promise, closed]); }
        finally { response.off("close", disconnected); }
      }
      return sendJson(response, 200, { ok: true, cleanup_receipt: operationReceipt(operation) });
    }
    if (remote && payload.action === "navigate" && payload.url) {
      const host = remote[1];
      const authorization = payload.navigation_authorization;
      payload = { ...payload, url: await host.mapUrl(payload.url),
        ...(authorization ? { navigation_authorization: { ...authorization,
          url: await host.mapUrl(authorization.url), preview_url: await host.mapUrl(authorization.preview_url) } } : {}) };
    }
    const result = await executeOwnedCommand(payload, response);
    return sendJson(response, result?.ok === false ? 400 : 200, result || { ok: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendDesktopLog(`[desktop] embedded browser bridge command failed: ${message}`);
    return sendJson(response, error.code === "ERR_WINDOW_BINDING" ? 403 : 400, { ok: false, error: message });
  }
}

async function start() {
  if (starting) return starting;
  if (server) {
    if (!accepting) throw new Error("Embedded browser bridge is stopping");
    return endpoint;
  }
  starting = (async () => {
    const target = configuredEndpoint ? bridgeBindTarget(configuredEndpoint) : { host: "127.0.0.1", port: 0 };
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
        active.listen(target.port, target.host, () => {
          active.off("error", reject);
          resolve();
        });
      });
      if (!accepting) throw new Error("Embedded browser bridge is stopping");
      endpoint = configuredEndpoint ? target.endpoint : `http://127.0.0.1:${active.address().port}`;
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
  for (const operation of operations.values()) cancelOperation(operation, "cancelled");
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
  for (const [id, operation] of operations) if (operation.settled) operations.delete(id);
  if (server === active) server = null;
  endpoint = "";
}

function releaseWindow(windowInstanceId) {
  for (const operation of operations.values()) {
    if (operation.windowInstanceId === windowInstanceId && !operation.settled) cancelOperation(operation, "window_closed");
  }
}

function registerRemoteHost(id, host) { remoteHosts.set(id, host); }
function unregisterRemoteHost(id, host) { if (remoteHosts.get(id) === host) remoteHosts.delete(id); }

module.exports = { init, start, stop, releaseWindow, registerRemoteHost, unregisterRemoteHost };
