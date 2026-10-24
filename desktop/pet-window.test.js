"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const animations = require("../frontend/public/pets/animations.json");

async function renderer(t, tasks = [], overrides = {}, onAction) {
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, "pet-window.html"), "utf8"), { runScripts: "outside-only", pretendToBeVisual: true });
  t.after(() => dom.window.close());
  const actions = [];
  const spriteRequests = [];
  let changed;
  const state = { pets: [{ id: "codex", displayName: "Codex", spriteVersionNumber: 2, updatedAt: 0 }], selectedPetId: "codex", size: 112, petVisible: true, miniOpen: true,
    activity: { running: false, waiting: false, failed: false, review: false, tasks }, reducedMotion: true, locale: "en-US", animations, layout: { noticeHeight: tasks.length * 76 }, ...overrides };
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  dom.window.miniPet = { getState: async () => state, getSprite: async id => { spriteRequests.push(id); return "data:image/webp;base64,fixture"; }, act: async (action, payload) => { actions.push({ action, payload }); return onAction?.(action, payload); }, onState: callback => { changed = callback; }, onError() {} };
  dom.window.eval(fs.readFileSync(path.join(__dirname, "pet-window.js"), "utf8"));
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  const pet = dom.window.document.getElementById("pet");
  const bar = dom.window.document.getElementById("mini-bar");
  for (const element of [pet, bar]) { element.setPointerCapture = () => {}; element.hasPointerCapture = () => false; }
  function pointer(type, x, y, element = pet) {
    const event = new dom.window.MouseEvent(type, { button: 0, screenX: x, screenY: y, bubbles: true });
    Object.defineProperty(event, "pointerId", { value: 1 }); element.dispatchEvent(event);
  }
  return { dom, pet, bar, actions, spriteRequests, state, pointer, changed: next => changed(next), flush };
}

test("None renders a live Mini bar and actual notices without requesting a sprite", async t => {
  const h = await renderer(t, [{ id: "actual-task", title: "Actual task", status: "running" }], { selectedPetId: null });
  assert.equal(h.pet.hidden, true); assert.equal(h.bar.hidden, false);
  assert.deepEqual(h.spriteRequests, []);
  assert.equal(h.dom.window.document.getElementById("error").hidden, true);
  assert.equal(h.dom.window.document.getElementById("mini-count").textContent, "1");
  assert.equal(h.dom.window.document.querySelector(".notice-title").textContent, "Actual task");
  h.dom.window.document.getElementById("mini-open").click();
  h.dom.window.document.getElementById("mini-menu").click(); await h.flush();
  assert.deepEqual(h.actions.filter(item => ["open-main", "context-menu"].includes(item.action)).map(item => item.action), ["open-main", "context-menu"]);
  h.actions.length = 0;
  h.pointer("pointerdown", 100, 100, h.bar); h.pointer("pointermove", 80, 100, h.bar); h.pointer("pointerup", 80, 100, h.bar); await h.flush();
  assert.deepEqual(h.actions.filter(item => item.action !== "element-size-changed").map(item => item.action), ["drag-start", "drag-move", "drag-end"]);
  h.changed({ ...h.state, selectedPetId: "codex" }); await h.flush();
  assert.equal(h.bar.hidden, true); assert.equal(h.pet.hidden, false); assert.deepEqual(h.spriteRequests, ["codex"]);
  h.changed({ ...h.state, selectedPetId: null }); await h.flush();
  assert.equal(h.bar.hidden, false); assert.equal(h.pet.hidden, true); assert.deepEqual(h.spriteRequests, ["codex"]);
});

test("the Mini attention card opens the exact actual task and treats its title as text", async t => {
  const h = await renderer(t, [{ id: "real-approval", title: "<img src=x> Actual approval", status: "waiting" }]);
  const card = h.dom.window.document.querySelector(".notice");
  assert.equal(card.querySelector("img"), null);
  assert.equal(card.querySelector(".notice-title").textContent, "<img src=x> Actual approval");
  assert.equal(card.querySelector(".notice-review").textContent, "Review");
  card.querySelector(".notice-open").click(); await h.flush();
  const open = h.actions.find(item => item.action === "open-task"); assert.equal(open.payload.id, "real-approval");
});

test("clicking the pet opens the main composer while a real drag does not", async t => {
  const h = await renderer(t);
  h.pointer("pointerdown", 100, 100); h.pointer("pointerup", 100, 100); await h.flush();
  assert.deepEqual(h.actions.filter(item => item.action !== "element-size-changed").map(item => item.action), ["drag-start", "drag-end", "open-main"]);
  h.actions.length = 0;
  h.pointer("pointerdown", 100, 100); h.pointer("pointermove", 80, 100); h.pointer("pointerup", 80, 100); await h.flush();
  assert.deepEqual(h.actions.filter(item => item.action !== "element-size-changed").map(item => item.action), ["drag-start", "drag-move", "drag-end"]);
});

test("reduced motion and the true running or approval state use the correct atlas row", async t => {
  const h = await renderer(t, [], { activity: { running: true, waiting: false, failed: false, review: false, tasks: [] } });
  assert.equal(h.pet.style.backgroundPosition, "0% 70%");
  h.changed({ ...h.state, activity: { running: false, waiting: true, failed: false, review: false, tasks: [] } }); await h.flush();
  assert.equal(h.pet.style.backgroundPosition, "0% 60%");
});

test("real request labels are the only reply buttons and a rejected reply stays in the card", async t => {
  const h = await renderer(t, [{ id: "approval", title: "Actual request", status: "waiting", detail: "Actual command",
    actions: [{ id: "request-7:allow", label: "Allow once" }, { id: "request-7:decline", label: "Decline" }] }], {}, async action => {
    if (action === "task-action") throw new Error("Actual backend rejection");
  });
  const buttons = [...h.dom.window.document.querySelectorAll(".notice-actions button")];
  assert.deepEqual(buttons.map(button => button.textContent), ["Allow once", "Decline"]);
  assert.equal(h.dom.window.document.querySelector(".notice-review"), null);
  buttons[0].click(); await h.flush();
  assert.match(h.dom.window.document.querySelector(".notice-error").textContent, /Actual backend rejection/);
  assert.equal(h.dom.window.document.querySelector(".notice-title").textContent, "Actual request");
  assert.equal(h.pet.hidden, false);
  const request = h.actions.find(item => item.action === "task-action");
  assert.equal(request.payload.taskId, "approval"); assert.equal(request.payload.actionId, "request-7:allow");
});

test("dismiss and native mute carry only the original notification identity", async t => {
  const h = await renderer(t, [{ id: "actual", turnKey: "actual-turn", title: "Actual task", status: "waiting" }]);
  const card = h.dom.window.document.querySelector(".notice");
  card.dispatchEvent(new h.dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
  card.querySelector(".notice-dismiss").click(); await h.flush();
  assert.deepEqual(JSON.parse(JSON.stringify(h.actions.find(item => item.action === "task-context-menu").payload)), { taskId: "actual" });
  assert.deepEqual(JSON.parse(JSON.stringify(h.actions.find(item => item.action === "dismiss-task").payload)), { taskId: "actual", turnKey: "actual-turn" });
});
