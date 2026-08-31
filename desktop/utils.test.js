"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { isProbablyTextBuffer } = require("./utils");

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
