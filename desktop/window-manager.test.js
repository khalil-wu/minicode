"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { isTrustedRendererUrl } = require("./window-manager");

test("renderer navigation stays on the configured development origin", () => {
  const target = { type: "url", value: "http://127.0.0.1:5173/" };

  assert.equal(isTrustedRendererUrl(target, "http://127.0.0.1:5173/settings"), true);
  assert.equal(isTrustedRendererUrl(target, "http://127.0.0.1:8000/"), false);
  assert.equal(isTrustedRendererUrl(target, "https://example.com/"), false);
});

test("packaged renderer navigation stays on the exact local entry file", () => {
  const entry = path.resolve("frontend", "dist", "index.html");
  const entryUrl = new URL(`file:///${entry.replace(/\\/g, "/")}`).toString();
  const otherUrl = new URL(`file:///${path.resolve("other.html").replace(/\\/g, "/")}`).toString();

  assert.equal(isTrustedRendererUrl({ type: "file", value: entry }, entryUrl), true);
  assert.equal(isTrustedRendererUrl({ type: "file", value: entry }, otherUrl), false);
});
