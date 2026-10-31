"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { isProbablyTextBuffer, rotateDesktopLogIfOversized } = require("./utils");

test("text detection accepts UTF-8 split at the old sample boundary", () => {
  const content = Buffer.concat([
    Buffer.alloc(4095, "a"),
    Buffer.from("中文 Markdown", "utf8"),
  ]);

  assert.equal(isProbablyTextBuffer(content, "README.md"), true);
});

test("text detection rejects invalid UTF-8, NUL bytes, and denied extensions", () => {
  assert.equal(isProbablyTextBuffer(Buffer.from([0xc3, 0x28]), "broken.md"), false);
  assert.equal(isProbablyTextBuffer(Buffer.from("text\0binary"), "data.txt"), false);
  assert.equal(isProbablyTextBuffer(Buffer.from("plain text"), "image.png"), false);
});

test("desktop log rotates once it exceeds the byte budget and keeps one rotated copy", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-log-"));
  const logPath = path.join(dir, "desktop.log");
  try {
    fs.writeFileSync(logPath, "first\n");
    assert.equal(rotateDesktopLogIfOversized(logPath, 1024), false);
    fs.writeFileSync(logPath, "x".repeat(2048));
    assert.equal(rotateDesktopLogIfOversized(logPath, 1024), true);
    assert.equal(fs.existsSync(logPath), false);
    assert.equal(fs.readFileSync(`${logPath}.1`, "utf8").length, 2048);
    fs.writeFileSync(logPath, "y".repeat(4096));
    assert.equal(rotateDesktopLogIfOversized(logPath, 1024), true);
    assert.equal(fs.readFileSync(`${logPath}.1`, "utf8")[0], "y");
    assert.equal(rotateDesktopLogIfOversized(path.join(dir, "missing.log"), 1024), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
