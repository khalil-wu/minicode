"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  availableDownloadPathIn,
  assertNavigationUrl,
  getBrowserSettings,
  init,
  isAllowedNavigationUrl,
  makeNetworkLogEntry,
  normalizeBrowserSettings,
  normalizeViewBounds,
  setBrowserSettings,
  assessNavigationTarget,
  assessNavigationTargetForRequest,
  actualPeerNavigationError,
} = require("./embedded-browser-manager");

test("embedded browser only accepts web navigation targets", () => {
  assert.equal(isAllowedNavigationUrl("https://example.com/path"), true);
  assert.equal(isAllowedNavigationUrl("http://127.0.0.1:5173"), true);
  assert.equal(isAllowedNavigationUrl("about:blank"), true);
  assert.equal(isAllowedNavigationUrl("file:///C:/secret.txt"), false);
  assert.equal(isAllowedNavigationUrl("javascript:alert(1)"), false);
  assert.throws(() => assertNavigationUrl("file:///C:/secret.txt"), /Only HTTP and HTTPS/);
});

test("embedded browser blocks private and credential-bearing navigation", () => {
  init({
    assessBrowserNavigationPolicy: (url) => ({
      url,
      host: new URL(url).hostname,
      risk: "private_or_local",
      requiresPrivateNetworkApproval: /^(?:192\.168\.|169\.254\.)/.test(new URL(url).hostname),
    }),
    isOwnedPreviewUrl: () => false,
  });
  assert.equal(assessNavigationTarget("https://example.com/docs").allowed, true);
  assert.equal(assessNavigationTarget("http://192.168.1.20/admin", "conv-a").allowed, false);
  assert.equal(assessNavigationTarget("http://169.254.169.254/latest/meta-data", "conv-a").allowed, false);
  assert.equal(assessNavigationTarget("https://user:pass@example.com/").allowed, false);
  const local = assessNavigationTarget("http://localhost:5173/", "conv-a");
  assert.equal(local.allowed, false);
  assert.equal(local.requiresPrivateNetworkApproval, true);
});

test("embedded browser allows only conversation-owned local previews without a prompt", () => {
  init({
    isOwnedPreviewUrl: (url, conversationId) => (
      conversationId === "conv-preview" && new URL(url).port === "5173"
    ),
  });
  assert.equal(assessNavigationTarget("http://localhost:5173/", "conv-preview").allowed, true);
  assert.equal(assessNavigationTarget("http://localhost:5173/", "conv-other").allowed, false);
  init({ isOwnedPreviewUrl: () => false });
});

test("embedded browser DNS and connected-peer checks fail closed", async () => {
  init({
    lookupHostAddresses: async () => [{ address: "10.0.0.8", family: 4 }],
    isOwnedPreviewUrl: () => false,
  });
  const rebound = await assessNavigationTargetForRequest("https://preview.example/app", "conv-a");
  assert.equal(rebound.allowed, false);
  assert.equal(rebound.requiresPrivateNetworkApproval, true);

  init({ lookupHostAddresses: async () => { throw new Error("dns unavailable"); } });
  const unknown = await assessNavigationTargetForRequest("https://docs.example/app", "conv-a");
  assert.equal(unknown.allowed, false);
  assert.equal(unknown.risk, "unverified");

  const entry = { conversationId: "conv-a", approvedPrivateOrigins: new Set() };
  assert.match(
    actualPeerNavigationError(entry, "https://preview.example/app", "169.254.169.254"),
    /private peer/,
  );
  assert.equal(
    actualPeerNavigationError(
      entry,
      "https://preview.example/app",
      "127.0.0.1",
      "PROXY 127.0.0.1:7897",
    ),
    "",
  );
  assert.match(
    actualPeerNavigationError(
      entry,
      "https://preview.example/app",
      "169.254.169.254",
      "DIRECT",
    ),
    /private peer/,
  );
  entry.approvedPrivateOrigins.add("https://preview.example");
  assert.equal(
    actualPeerNavigationError(entry, "https://preview.example/app", "169.254.169.254"),
    "",
  );
  init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
});

test("embedded browser bounds compensate for renderer zoom and stay clipped", () => {
  assert.deepEqual(
    normalizeViewBounds(
      { x: 1000, y: 120, width: 600, height: 700 },
      { width: 1800, height: 1000 },
      1.1,
    ),
    { x: 1100, y: 132, width: 660, height: 770 },
  );
  assert.deepEqual(
    normalizeViewBounds(
      { x: 1500, y: 900, width: 800, height: 500 },
      { width: 1800, height: 1000 },
      1,
    ),
    { x: 1500, y: 900, width: 300, height: 100 },
  );
});

test("embedded browser exposes a stable target list for frontend hydration", () => {
  const manager = require("./embedded-browser-manager");
  assert.equal(typeof manager.listTargets, "function");
  assert.deepEqual(manager.listTargets("conv-browser-owner"), []);
  assert.throws(() => manager.listTargets(), /conversation owner/i);
});

test("embedded browser network diagnostics retain only safe request metadata", () => {
  const entry = makeNetworkLogEntry({
    url: "https://example.com/api/tasks",
    method: "POST",
    statusCode: 201,
    resourceType: "xhr",
    fromCache: true,
    requestHeaders: { authorization: "secret" },
  }, "");

  assert.deepEqual({ ...entry, timestamp: 0 }, {
    url: "https://example.com/api/tasks",
    method: "POST",
    statusCode: 201,
    resourceType: "xhr",
    fromCache: true,
    error: "",
    timestamp: 0,
  });
  assert.ok(Number.isFinite(entry.timestamp));
});

test("embedded browser settings normalize origins and supported permissions", () => {
  assert.deepEqual(normalizeBrowserSettings({
    downloadPolicy: "unsafe",
    sitePermissions: {
      "https://example.com/path": ["media", "media", "unknown"],
      "file:///C:/secret": ["notifications"],
    },
  }), {
    downloadPolicy: "block",
    sitePermissions: { "https://example.com": ["media"] },
  });
});

test("embedded browser settings persist per origin", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-browser-settings-"));
  const settingsFile = path.join(root, "browser-settings.json");
  init({ browserSettingsPath: settingsFile });

  setBrowserSettings({ downloadPolicy: "allow" });
  setBrowserSettings({ origin: "https://example.com/page", permission: "notifications", allowed: true });

  assert.deepEqual(getBrowserSettings("https://example.com/other"), {
    downloadPolicy: "allow",
    origin: "https://example.com",
    permissions: ["notifications"],
  });
  assert.throws(
    () => setBrowserSettings({ origin: "file:///C:/secret", permission: "media", allowed: true }),
    /valid HTTP or HTTPS origin/,
  );
});

test("allowed downloads never overwrite an existing filename", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-browser-download-"));
  fs.writeFileSync(path.join(root, "report.pdf"), "one");
  fs.writeFileSync(path.join(root, "report (1).pdf"), "two");

  assert.equal(availableDownloadPathIn(root, "../report.pdf"), path.join(root, "report (2).pdf"));
});
