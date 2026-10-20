"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { attachGuestAutofill, fillBrowserForm } = require("./browser-autofill");

function formFixture(origin = "https://example.test") {
  class Input {
    constructor(type, autocomplete = "") { this.type = type; this.autocomplete = autocomplete; this.events = []; this.disabled = false; this.readOnly = false; this.isConnected = true; }
    set value(value) { this.currentValue = value; }
    get value() { return this.currentValue || ""; }
    dispatchEvent(event) { this.events.push(event.type); }
  }
  class Textarea extends Input {}
  class Select extends Input {}
  const username = new Input("text", "username");
  const password = new Input("password", "current-password");
  const email = new Input("email", "email");
  const address = new Input("text", "address-line1");
  const hidden = new Input("hidden", "address-line1");
  const fields = [username, password, email, address, hidden];
  const form = { querySelectorAll: () => fields };
  for (const field of fields) field.form = form;
  const document = { activeElement: password, querySelectorAll: () => fields };
  for (const field of fields) field.ownerDocument = document;
  const globals = { location: { origin }, document, HTMLInputElement: Input, HTMLTextAreaElement: Textarea, HTMLSelectElement: Select,
    Event: class { constructor(type) { this.type = type; } } };
  const run = (kind, values, expectedOrigin = origin, handle = "fixture-form") => vm.runInNewContext(`(${fillBrowserForm.toString()})(${JSON.stringify(kind)},${JSON.stringify(values)},${JSON.stringify(expectedOrigin)},${JSON.stringify(handle)})`, globals);
  return { globals, username, password, email, address, hidden, run,
    fill: (kind, values, expectedOrigin = origin) => { run("prepare", null, expectedOrigin); return run(kind, values, expectedOrigin); } };
}

function menuFixture(form = formFixture(), { chinese = false } = {}) {
  const templates = [], prompts = [], logs = [], selections = [];
  const frame = { url: "https://example.test/login", detached: false, isDestroyed: () => false,
    get origin() { return new URL(this.url).origin; },
    async executeJavaScript(expression) { selections.push(expression); return vm.runInNewContext(expression, form.globals); } };
  const contents = Object.assign(new EventEmitter(), { url: "https://example.test/login", getURL() { return this.url; }, isDestroyed: () => false,
    executeJavaScript() { assert.fail("autofill must never execute in the main WebContents instead of its captured frame"); } });
  let resolved = 0;
  const repository = { listPasswords: () => ({ available: true }), passwordsForOrigin: () => [{ id: "saved", username: "test-user" }], contactChoices: () => [{ id: "contact", label: "Test Person" }],
    passwordForFill(id, origin) { resolved++; assert.equal(id, "saved"); assert.equal(origin, "https://example.test"); return { username: "test-user", password: "test-secret" }; },
    contactForFill() { return { fullName: "Test Person", email: "test@example.test", addressLine1: "123 Test St" }; } };
  const entry = { navigationVersion: 0, view: { webContents: contents }, window: { isDestroyed: () => false } };
  attachGuestAutofill(entry, () => repository, { text: (en, zh) => chinese ? zh : en, appendDesktopLog: message => logs.push(message),
    Menu: { buildFromTemplate(items) { templates.push(items); return { popup(options) { this.options = options; selections.push(options); } }; } },
    dialog: { async showMessageBox(_owner, prompt) { prompts.push(prompt); } } });
  return { form, frame, contents, entry, templates, prompts, logs, selections, repository,
    resolved: () => resolved,
    async open(target = frame) { contents.emit("context-menu", {}, { isEditable: true, frame: target, frameURL: target?.url }); await new Promise(resolve => setImmediate(resolve)); return templates.at(-1); },
    async choose(kind = "password") { const template = templates.at(-1); const label = kind === "password" ? (chinese ? "密码" : "Passwords") : (chinese ? "联系信息" : "Contact info"); template.find(item => item.label === label).submenu[0].click(); await new Promise(resolve => setImmediate(resolve)); },
  };
}

test("credential fill changes the selected form and dispatches input events without submitting", () => {
  const form = formFixture();
  assert.equal(form.fill("password", { username: "test-user", password: "test-password" }), true);
  assert.equal(form.username.value, "test-user");
  assert.equal(form.password.value, "test-password");
  assert.deepEqual(form.password.events, ["input", "change"]);
  assert.equal(form.email.value, "");
  const changed = formFixture("https://other.test");
  assert.equal(changed.fill("password", { username: "test-user", password: "test-password" }, "https://example.test"), false);
  assert.equal(changed.password.value, "");
});

test("contact fill consumes recognized autocomplete fields without touching passwords or hidden inputs", () => {
  const form = formFixture();
  form.globals.document.activeElement = form.email;
  assert.equal(form.fill("contact", { fullName: "Test Person", email: "test@example.test", addressLine1: "123 Test St" }), true);
  assert.equal(form.email.value, "test@example.test");
  assert.equal(form.address.value, "123 Test St");
  assert.equal(form.password.value, "");
  assert.equal(form.hidden.value, "");
});

test("native choices contain metadata only and resolve credentials after explicit owner selection", async () => {
  const h = menuFixture();
  const template = await h.open();
  assert.equal(h.resolved(), 0);
  assert.equal(JSON.stringify(template).includes("test-secret"), false);
  await h.choose();
  assert.equal(h.resolved(), 1);
  assert.equal(h.form.password.value, "test-secret");
  assert.equal(h.prompts.length, 0); assert.equal(h.logs.length, 0);
  assert.equal(Object.getOwnPropertyNames(h.form.globals).some(key => key.startsWith("__minicode_autofill_")), false);
});

test("cross-origin frames and navigation while the owner menu is open cannot receive saved data", async () => {
  const h = menuFixture();
  await h.open({ ...h.frame, url: "https://other.test/form", origin: "https://other.test" });
  assert.equal(h.templates.length, 0);
  await h.open();
  h.contents.emit("did-start-navigation", { frame: h.frame, isSameDocument: false });
  h.contents.url = "https://other.test/";
  await h.choose();
  assert.equal(h.resolved(), 0);
  assert.equal(h.prompts.length, 1);
});

test("same-origin iframe fill uses the original frame and native menu frame target", async () => {
  const h = menuFixture(); h.frame.url = "https://example.test/embedded-form";
  await h.open();
  assert.equal(h.selections.find(value => typeof value === "object").frame, h.frame);
  await h.choose();
  assert.equal(h.form.username.value, "test-user"); assert.equal(h.form.password.value, "test-secret");
});

test("same-origin same-URL navigation invalidates an open fill menu before credential lookup", async () => {
  const h = menuFixture(); await h.open();
  h.contents.emit("did-start-navigation", { frame: h.frame, isSameDocument: false });
  await h.choose();
  assert.equal(h.resolved(), 0); assert.equal(h.form.password.value, "");
  assert.match(h.prompts[0].detail, /page changed/);
});

test("form target identity survives focus changes and refuses a replaced form or document", () => {
  const form = formFixture();
  form.run("prepare", null);
  form.globals.document.activeElement = form.email;
  assert.equal(form.run("password", { username: "u", password: "secret" }), true);
  assert.equal(form.password.value, "secret");
  form.run("prepare", null);
  form.email.form = { querySelectorAll: () => [] };
  assert.equal(form.run("contact", { fullName: "P", email: "new@example.test" }), false);
  const another = formFixture(); another.run("prepare", null);
  another.globals.document = { activeElement: another.password };
  assert.equal(another.run("password", { username: "u", password: "new-secret" }), false);
  assert.equal(another.password.value, "");
});

test("new-password-only and unrelated editable fields do not get saved-credential menu entries", async () => {
  const h = menuFixture(); h.form.password.autocomplete = "new-password";
  await h.open();
  assert.equal(h.templates.length, 0); assert.equal(h.resolved(), 0);
  h.form.globals.document.activeElement = h.form.hidden;
  await h.open();
  assert.equal(h.templates.length, 0);
});

test("cancel closes the menu target and Chinese errors retain no password value", async () => {
  const h = menuFixture(undefined, { chinese: true }); await h.open();
  assert.equal(h.templates[0][0].label, "密码");
  h.selections.find(value => typeof value === "object").callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(Object.getOwnPropertyNames(h.form.globals).some(key => key.startsWith("__minicode_autofill_")), false);
  await h.open();
  h.frame.executeJavaScript = async () => { throw new Error("target failure test-secret"); };
  await h.choose();
  assert.equal(h.prompts[0].message, "无法填写此表单。");
  assert.equal(h.prompts[0].detail.includes("test-secret"), false);
  assert.equal(h.prompts[0].detail.includes("[redacted]"), true);
  assert.equal(h.logs.join("\n").includes("test-secret"), false);
});
