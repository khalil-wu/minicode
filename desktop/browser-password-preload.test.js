"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("the sandbox preload captures only a trusted edited password and trusted form submission without exporting page APIs", () => {
  const listeners = new Map(); const sent = [];
  class Input { constructor(type, value, autocomplete = "") { this.type = type; this.value = value; this.autocomplete = autocomplete; } }
  const username = new Input("text", "fixture-user", "username"); const password = new Input("password", "fixture-secret");
  let inputs = [username, password];
  class Form { querySelectorAll() { return inputs; } }
  const form = new Form();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "browser-password-preload.js"), "utf8"), {
    document: { addEventListener: (name, callback) => listeners.set(name, callback) }, HTMLInputElement: Input, HTMLFormElement: Form,
    location: { protocol: "https:", origin: "https://example.test", href: "https://example.test/login" },
    require(name) { assert.equal(name, "electron"); return { ipcRenderer: { send: (channel, payload) => sent.push({ channel, payload }) } }; },
  });
  listeners.get("input")({ isTrusted: false, target: password }); listeners.get("submit")({ isTrusted: true, target: form }); assert.equal(sent.length, 0);
  listeners.get("input")({ isTrusted: true, target: password }); listeners.get("submit")({ isTrusted: false, target: form }); assert.equal(sent.length, 0);
  listeners.get("submit")({ isTrusted: true, target: form });
  assert.equal(sent.length, 1); assert.equal(sent[0].channel, "minicode:browserPassword:submitted");
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0].payload)), { origin: "https://example.test", documentUrl: "https://example.test/login", username: "fixture-user", password: "fixture-secret" });
  password.autocomplete = "current-password";
  const replacement = new Input("password", "replacement-secret", "new-password"); inputs = [password, replacement];
  listeners.get("input")({ isTrusted: true, target: replacement }); listeners.get("submit")({ isTrusted: true, target: form });
  assert.equal(sent[1].payload.password, "replacement-secret"); assert.equal(sent[1].payload.oldPassword, "fixture-secret");
  assert.equal(sent[1].payload.username, "");
});
