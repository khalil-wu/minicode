"use strict";

const { sleep, isHttpUrl } = require("./utils");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let appendDesktopLog = () => {};
const CDP_SCREENSHOT_MAX_DIMENSION = 4096;
// MiniCode's API image contract is a 5 MiB base64 payload, not raw bytes.
const CDP_SCREENSHOT_MAX_BASE64_CHARS = 5 * 1024 * 1024;
const CDP_TYPE_MAX_CHARS = 200_000;

function init({ logger }) {
  if (typeof logger === "function") appendDesktopLog = logger;
}

// ---------------------------------------------------------------------------
// CDP endpoint helpers
// ---------------------------------------------------------------------------

function normalizeLoopbackCdpEndpoint(targetEndpoint) {
  const raw = typeof targetEndpoint === "string" && targetEndpoint.trim() ? targetEndpoint.trim() : "http://127.0.0.1:9222";
  const withProtocol = /^[a-z]+:\/\//i.test(raw) ? raw : `http://${raw}`;
  let parsed;
  try {
    parsed = new URL(withProtocol);
  } catch {
    throw new Error("Invalid Chrome DevTools endpoint.");
  }
  if (parsed.protocol !== "http:") {
    throw new Error("Chrome DevTools endpoint must use http://.");
  }
  const host = parsed.hostname.toLowerCase();
  const isLoopback =
    host === "127.0.0.1" ||
    host === "localhost" ||
    host === "::1" ||
    host === "[::1]";
  if (!isLoopback) {
    throw new Error("Chrome DevTools endpoint must be local.");
  }
  const port = parsed.port || "9222";
  return `http://${host === "[::1]" ? "::1" : host}:${port}`;
}

function normalizeHost(value) {
  return String(value || "").replace(/^\[|\]$/g, "").toLowerCase();
}

function isLoopbackHost(value) {
  const host = normalizeHost(value);
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function validateWebSocketDebuggerUrl(value, discoveryEndpoint) {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw) return "";
  let parsed;
  let endpoint;
  try {
    parsed = new URL(raw);
    endpoint = new URL(discoveryEndpoint);
  } catch {
    return "";
  }
  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") return "";
  if (parsed.username || parsed.password || !parsed.hostname) return "";
  if (!isLoopbackHost(parsed.hostname)) return "";
  if (normalizeHost(parsed.hostname) !== normalizeHost(endpoint.hostname)) return "";
  const endpointPort = Number(endpoint.port || 9222);
  const socketPort = Number(parsed.port || (parsed.protocol === "wss:" ? 443 : 80));
  if (!Number.isInteger(socketPort) || socketPort !== endpointPort) return "";
  return parsed.toString();
}

async function fetchJsonWithTimeout(url, timeoutMs = 1800) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

async function discoverChromeCdp(targetEndpoint) {
  const endpoint = normalizeLoopbackCdpEndpoint(targetEndpoint);
  try {
    const [version, rawTargets] = await Promise.all([
      fetchJsonWithTimeout(`${endpoint}/json/version`),
      fetchJsonWithTimeout(`${endpoint}/json/list`),
    ]);
    const targets = Array.isArray(rawTargets)
      ? rawTargets.map((target) => ({
          id: typeof target?.id === "string" ? target.id : "",
          type: typeof target?.type === "string" ? target.type : "other",
          title: typeof target?.title === "string" ? target.title : "",
          url: typeof target?.url === "string" ? target.url : "",
          attached: Boolean(target?.attached),
          faviconUrl: typeof target?.faviconUrl === "string" ? target.faviconUrl : "",
          devtoolsFrontendUrl:
            typeof target?.devtoolsFrontendUrl === "string" ? target.devtoolsFrontendUrl : "",
          webSocketDebuggerUrl: validateWebSocketDebuggerUrl(
            target?.webSocketDebuggerUrl,
            endpoint,
          ),
        }))
      : [];
    return {
      status: "connected",
      endpoint,
      browser: typeof version?.Browser === "string" ? version.Browser : "",
      protocolVersion: typeof version?.["Protocol-Version"] === "string" ? version["Protocol-Version"] : "",
      userAgent: typeof version?.["User-Agent"] === "string" ? version["User-Agent"] : "",
      webSocketDebuggerUrl: validateWebSocketDebuggerUrl(
        version?.webSocketDebuggerUrl,
        endpoint,
      ),
      targets,
    };
  } catch (error) {
    return {
      status: "error",
      endpoint,
      browser: "",
      protocolVersion: "",
      userAgent: "",
      webSocketDebuggerUrl: "",
      targets: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

// ---------------------------------------------------------------------------
// WebSocket helpers
// ---------------------------------------------------------------------------

async function readWebSocketEventText(data) {
  if (typeof data === "string") return data;
  if (data && typeof data.text === "function") return await data.text();
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  if (ArrayBuffer.isView(data)) {
    return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  }
  return Buffer.from(data).toString("utf8");
}

async function openWebSocketWithTimeout(url, timeoutMs = 5000) {
  let parsed;
  try {
    parsed = new URL(String(url || ""));
  } catch {
    throw new Error("Invalid Chrome DevTools WebSocket URL.");
  }
  if ((parsed.protocol !== "ws:" && parsed.protocol !== "wss:") || !isLoopbackHost(parsed.hostname)) {
    throw new Error("Chrome DevTools WebSocket URL must be loopback ws(s).");
  }
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timer = setTimeout(() => {
      cleanup();
      try {
        socket.close();
      } catch {
        // noop
      }
      reject(new Error("CDP WebSocket connection timed out."));
    }, timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      socket.removeEventListener("open", onOpen);
      socket.removeEventListener("error", onError);
    };
    const onOpen = () => {
      cleanup();
      resolve(socket);
    };
    const onError = () => {
      cleanup();
      reject(new Error("Failed to connect to the Chrome target."));
    };
    socket.addEventListener("open", onOpen);
    socket.addEventListener("error", onError);
  });
}

// ---------------------------------------------------------------------------
// CDP session management
// ---------------------------------------------------------------------------

async function withCdpSession(webSocketDebuggerUrl, action) {
  const socket = await openWebSocketWithTimeout(webSocketDebuggerUrl, 5000);
  const pending = new Map();
  let nextId = 1;

  const settlePending = (error) => {
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.clear();
  };

  const onClose = () => {
    settlePending(new Error("Chrome target connection closed."));
  };
  const onError = () => {
    settlePending(new Error("Chrome target connection failed."));
  };
  const onMessage = (event) => {
    void (async () => {
      try {
        const text = await readWebSocketEventText(event.data);
        const payload = JSON.parse(text);
        if (!payload || typeof payload !== "object") return;
        if (typeof payload.id !== "number" || !pending.has(payload.id)) return;
        const request = pending.get(payload.id);
        pending.delete(payload.id);
        clearTimeout(request.timer);
        if (payload.error) {
          request.reject(new Error(payload.error.message || "CDP command failed."));
          return;
        }
        request.resolve(payload.result ?? null);
      } catch (error) {
        appendDesktopLog(`[browser] failed to parse CDP message: ${error instanceof Error ? error.message : String(error)}`);
      }
    })();
  };

  socket.addEventListener("close", onClose);
  socket.addEventListener("error", onError);
  socket.addEventListener("message", onMessage);

  const call = (method, params = {}, timeoutMs = 8000) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timed out.`));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      try {
        socket.send(JSON.stringify({ id, method, params }));
      } catch (error) {
        clearTimeout(timer);
        pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });

  try {
    return await action({ call, socket });
  } finally {
    socket.removeEventListener("close", onClose);
    socket.removeEventListener("error", onError);
    socket.removeEventListener("message", onMessage);
    settlePending(new Error("Chrome target connection closed."));
    try {
      socket.close();
    } catch {
      // noop
    }
  }
}

// ---------------------------------------------------------------------------
// CDP evaluation helpers
// ---------------------------------------------------------------------------

async function evaluateValue(call, expression, timeoutMs = 3000) {
  const result = await call(
    "Runtime.evaluate",
    {
      expression,
      returnByValue: true,
      awaitPromise: true,
    },
    timeoutMs,
  );
  if (result?.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Runtime evaluation failed.");
  }
  return result?.result?.value ?? null;
}

async function waitForDocumentReady(call, timeoutMs = 12000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const state = await evaluateValue(call, "document.readyState", 2500).catch(() => null);
    if (state === "interactive" || state === "complete") {
      return state;
    }
    await sleep(150);
  }
  throw new Error("Page did not finish loading in time.");
}

async function getPageMetadata(call) {
  const value = await evaluateValue(
    call,
    "(() => ({ title: document.title || '', url: location.href || '' }))()",
    3000,
  ).catch(() => null);
  return {
    title: typeof value?.title === "string" ? value.title : "",
    url: typeof value?.url === "string" ? value.url : "",
  };
}

async function resolveSelectorTarget(call, selector) {
  const normalized = typeof selector === "string" ? selector.trim() : "";
  if (!normalized) {
    throw new Error("CSS selector is required.");
  }
  const selectorLiteral = JSON.stringify(normalized);
  const value = await evaluateValue(
    call,
    `(() => {
      const selector = ${selectorLiteral};
      const el = document.querySelector(selector);
      if (!el) return { ok: false, error: 'Element not found for selector: ' + selector };
      el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
      const rect = el.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        return { ok: false, error: 'Element has no visible box.' };
      }
      return {
        ok: true,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        width: rect.width,
        height: rect.height,
      };
    })()`,
    4000,
  );
  if (!value?.ok) {
    throw new Error(typeof value?.error === "string" ? value.error : "Element lookup failed.");
  }
  return value;
}

// ---------------------------------------------------------------------------
// Screenshot
// ---------------------------------------------------------------------------

async function captureScreenshotWithCall(call) {
  const metrics = await call("Page.getLayoutMetrics", {}, 3000).catch(() => null);
  const contentWidth = typeof metrics?.contentSize?.width === "number" ? metrics.contentSize.width : 0;
  const contentHeight = typeof metrics?.contentSize?.height === "number" ? metrics.contentSize.height : 0;
  const width = Math.max(1, Math.min(Math.round(contentWidth || CDP_SCREENSHOT_MAX_DIMENSION), CDP_SCREENSHOT_MAX_DIMENSION));
  const height = Math.max(1, Math.min(Math.round(contentHeight || CDP_SCREENSHOT_MAX_DIMENSION), CDP_SCREENSHOT_MAX_DIMENSION));
  const screenshot = await call(
    "Page.captureScreenshot",
    {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      optimizeForSpeed: true,
      clip: { x: 0, y: 0, width, height, scale: 1 },
    },
    12000,
  );
  if (!screenshot || typeof screenshot.data !== "string" || !screenshot.data) {
    throw new Error("Chrome did not return screenshot data.");
  }
  if (screenshot.data.length > CDP_SCREENSHOT_MAX_BASE64_CHARS) {
    throw new Error("Chrome screenshot exceeded the desktop transfer limit.");
  }
  return { data: screenshot.data, width, height };
}

async function captureScreenshotViaCdp(webSocketDebuggerUrl) {
  return await withCdpSession(webSocketDebuggerUrl, async ({ call }) => {
    await call("Page.enable", {}, 3000).catch(() => null);
    try {
      await call("Page.bringToFront", {}, 3000);
    } catch {
      // Some pages may not support bringToFront; screenshot can still work.
    }
    return await captureScreenshotWithCall(call);
  });
}

// ---------------------------------------------------------------------------
// Page target helpers
// ---------------------------------------------------------------------------

async function withChromePageTarget(targetEndpoint, targetId, action) {
  const discovery = await discoverChromeCdp(targetEndpoint);
  if (discovery.status !== "connected") {
    throw new Error(discovery.error || "Chrome DevTools endpoint is unavailable.");
  }
  const target = discovery.targets.find((candidate) => candidate.id === targetId);
  if (!target) {
    throw new Error("Chrome target not found.");
  }
  if (target.type !== "page") {
    throw new Error("Browser actions currently support page targets only.");
  }
  if (!target.webSocketDebuggerUrl) {
    throw new Error("Selected target does not expose a debugger WebSocket.");
  }
  return await withCdpSession(target.webSocketDebuggerUrl, async ({ call }) => {
    await call("Page.enable", {}, 3000).catch(() => null);
    await call("Runtime.enable", {}, 3000).catch(() => null);
    await call("DOM.enable", {}, 3000).catch(() => null);
    try {
      await call("Page.bringToFront", {}, 3000);
    } catch {
      // Some pages may not support bringToFront.
    }
    return await action({ call, target, discovery });
  });
}

async function captureChromeTargetScreenshot(targetEndpoint, targetId) {
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const screenshot = await captureScreenshotWithCall(call);
    return {
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: target.title,
      url: target.url,
      mimeType: "image/png",
      data: screenshot.data,
      width: screenshot.width,
      height: screenshot.height,
      capturedAt: Date.now(),
    };
  });
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function normalizeBrowserNavigationUrl(target) {
  const raw = typeof target === "string" ? target.trim() : "";
  if (!raw) {
    throw new Error("Navigation URL is required.");
  }
  const withProtocol = /^[a-z]+:\/\//i.test(raw) ? raw : `http://${raw}`;
  if (!isHttpUrl(withProtocol)) {
    throw new Error("Navigation only supports http(s) URLs.");
  }
  return withProtocol;
}

function parseIpv4Host(host) {
  const parts = host.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
  return parts;
}

function isPrivateOrLocalBrowserHost(host) {
  const normalized = String(host || "").replace(/^\[|\]$/g, "").toLowerCase();
  if (!normalized) return false;
  if (normalized === "localhost" || normalized === "localhost.localdomain" || normalized.endsWith(".localhost")) return true;
  if (normalized.startsWith("::ffff:")) return true;
  if (normalized === "::1" || normalized === "::" || normalized.startsWith("fc") || normalized.startsWith("fd") || /^fe[89ab]/.test(normalized) || normalized.startsWith("ff") || normalized.startsWith("2001:db8:")) return true;
  const ipv4 = parseIpv4Host(normalized);
  if (!ipv4) return false;
  const [a, b] = ipv4;
  return a === 0
    || a === 10
    || a === 127
    || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 0 || b === 168))
    || (a === 198 && (b === 18 || b === 19 || b === 51))
    || (a === 203 && b === 0)
    || a >= 224;
}

function assertBrowserNavigationPolicy(targetUrl, options = {}) {
  const assessment = assessBrowserNavigationPolicy(targetUrl);
  if (assessment.requiresPrivateNetworkApproval && !options.allowPrivateNetwork) {
    throw new Error("Local or private browser navigation requires approval.");
  }
  return assessment;
}

function assessBrowserNavigationPolicy(targetUrl) {
  const normalizedUrl = normalizeBrowserNavigationUrl(targetUrl);
  let parsed;
  try {
    parsed = new URL(normalizedUrl);
  } catch {
    throw new Error("Navigation URL is invalid.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Navigation URLs must not contain credentials.");
  }
  const requiresPrivateNetworkApproval = isPrivateOrLocalBrowserHost(parsed.hostname);
  return {
    url: normalizedUrl,
    host: parsed.hostname,
    risk: requiresPrivateNetworkApproval ? "private_or_local" : "public",
    requiresPrivateNetworkApproval,
  };
}

async function navigateChromeTarget(targetEndpoint, targetId, url, options = {}) {
  const assessment = assertBrowserNavigationPolicy(url, options);
  const targetUrl = assessment.url;
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    await call("Page.navigate", { url: targetUrl }, 8000);
    await waitForDocumentReady(call, 15000).catch(() => null);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "navigate",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || targetUrl,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || targetUrl,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

// ---------------------------------------------------------------------------
// Click
// ---------------------------------------------------------------------------

async function clickChromeTarget(targetEndpoint, targetId, selector) {
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const point = await resolveSelectorTarget(call, selector);
    await call("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, buttons: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 }, 3000);
    await sleep(250);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "click",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || target.url,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || target.url,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

// ---------------------------------------------------------------------------
// Type
// ---------------------------------------------------------------------------

async function typeIntoChromeTarget(targetEndpoint, targetId, selector, text) {
  const value = typeof text === "string" ? text : "";
  if (!value) {
    throw new Error("Text is required.");
  }
  if (value.length > CDP_TYPE_MAX_CHARS) {
    throw new Error(`Text exceeds the ${CDP_TYPE_MAX_CHARS} character browser input limit.`);
  }
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const selectorLiteral = JSON.stringify(typeof selector === "string" ? selector.trim() : "");
    const prepare = await evaluateValue(
      call,
      `(() => {
        const selector = ${selectorLiteral};
        if (!selector) return { ok: false, error: 'CSS selector is required.' };
        const el = document.querySelector(selector);
        if (!el) return { ok: false, error: 'Element not found for selector: ' + selector };
        el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
        const rect = el.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) {
          return { ok: false, error: 'Element has no visible box.' };
        }
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
          el.focus();
          el.value = '';
          el.dispatchEvent(new Event('input', { bubbles: true }));
        } else if (el.isContentEditable) {
          el.focus();
          el.textContent = '';
        } else {
          el.focus?.();
        }
        return {
          ok: true,
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
        };
      })()`,
      5000,
    );
    if (!prepare?.ok) {
      throw new Error(typeof prepare?.error === "string" ? prepare.error : "Failed to focus element.");
    }
    await call("Input.dispatchMouseEvent", { type: "mouseMoved", x: prepare.x, y: prepare.y, buttons: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mousePressed", x: prepare.x, y: prepare.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mouseReleased", x: prepare.x, y: prepare.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.insertText", { text: value }, 5000);
    await sleep(200);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "type",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || target.url,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || target.url,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  assessBrowserNavigationPolicy,
  discoverChromeCdp,
  captureChromeTargetScreenshot,
  navigateChromeTarget,
  clickChromeTarget,
  typeIntoChromeTarget,
  captureScreenshotViaCdp,
  validateWebSocketDebuggerUrl,
};
