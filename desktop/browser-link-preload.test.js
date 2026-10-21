"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function preloadFixture() {
  const listeners = new Map();
  const calls = [];
  let accepted = false;
  class Anchor {
    constructor(href = "https://destination.example/resolved", hasHref = true) { this.href = href; this.hasHref = hasHref; }
    hasAttribute(name) { assert.equal(name, "href"); return this.hasHref; }
  }
  class Area extends Anchor {}
  class SvgAnchor {
    constructor(href, xlink = false) { this.href = { baseVal: href }; this.baseURI = "https://source.example/base/page"; this.xlink = xlink; }
    hasAttribute(name) { assert.equal(name, "href"); return !this.xlink; }
    hasAttributeNS(namespace, name) { assert.equal(namespace, "http://www.w3.org/1999/xlink"); assert.equal(name, "href"); return this.xlink; }
  }
  const context = { document: { addEventListener: (type, listener, capture) => { assert.equal(capture, true); listeners.set(type, listener); } },
    location: { href: "https://source.example/current-document" }, HTMLAnchorElement: Anchor, HTMLAreaElement: Area, SVGAnchorElement: SvgAnchor, URL,
    require(name) { assert.equal(name, "electron"); return { ipcRenderer: { sendSync(channel, request) { calls.push({ channel, request: structuredClone(request) }); return accepted; } } }; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "browser-password-preload.js"), "utf8"), context);
  const click = (overrides = {}) => {
    let prevented = 0, stopped = 0;
    const event = { isTrusted: true, defaultPrevented: false, button: 0, ctrlKey: true, metaKey: false, altKey: false, shiftKey: false,
      composedPath: () => [{}, new Anchor()], preventDefault() { prevented++; }, stopImmediatePropagation() { stopped++; }, ...overrides };
    listeners.get("click")(event);
    return { prevented, stopped };
  };
  return { click, calls, Anchor, Area, SvgAnchor, accept: value => { accepted = value; } };
}

test("native guest link preload sends the trusted original anchor and modifiers before suppressing an accepted click", () => {
  const f = preloadFixture(); f.accept(true);
  assert.deepEqual(f.click(), { prevented: 1, stopped: 1 });
  assert.deepEqual(f.calls, [{ channel: "minicode:browserLink:click", request: {
    documentUrl: "https://source.example/current-document", url: "https://destination.example/resolved",
    modifiers: { ctrlKey: true, metaKey: false, altKey: false, shiftKey: false },
  } }]);
  assert.deepEqual(f.click({ ctrlKey: false, altKey: true, composedPath: () => [new f.Area("https://destination.example/image-map")] }), { prevented: 1, stopped: 1 });
  assert.equal(f.calls.at(-1).request.url, "https://destination.example/image-map");
  assert.deepEqual(f.calls.at(-1).request.modifiers, { ctrlKey: false, metaKey: false, altKey: true, shiftKey: false });
});

test("native guest SVG links resolve the original SVG href or xlink against its document base", () => {
  const f = preloadFixture(); f.accept(true);
  assert.deepEqual(f.click({ composedPath: () => [{}, new f.SvgAnchor("../drawing?section=1")] }), { prevented: 1, stopped: 1 });
  assert.equal(f.calls.at(-1).request.url, "https://source.example/drawing?section=1");
  assert.deepEqual(f.click({ altKey: true, ctrlKey: false, composedPath: () => [new f.SvgAnchor("https://destination.example/icon", true)] }), { prevented: 1, stopped: 1 });
  assert.equal(f.calls.at(-1).request.url, "https://destination.example/icon");
  assert.deepEqual(f.click({ composedPath: () => [new f.SvgAnchor("http://[")] }), { prevented: 0, stopped: 0 });
  assert.deepEqual(f.click({ composedPath: () => [new f.SvgAnchor("javascript:alert(1)")] }), { prevented: 0, stopped: 0 });
  assert.equal(f.calls.length, 2);
});

test("native guest link preload leaves a rejected modifier gesture to the page", () => {
  const f = preloadFixture();
  assert.deepEqual(f.click({ ctrlKey: false, metaKey: true, shiftKey: true }), { prevented: 0, stopped: 0 });
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.calls[0].request.modifiers, { ctrlKey: false, metaKey: true, altKey: false, shiftKey: true });
});

test("native guest link preload excludes synthetic, ordinary, prevented, non-link and non-HTTP clicks", () => {
  const f = preloadFixture(); f.accept(true);
  for (const overrides of [{ isTrusted: false }, { ctrlKey: false }, { defaultPrevented: true }, { button: 1 }, { button: 2 },
    { composedPath: () => [{}] }, { composedPath: () => [new f.Anchor("https://destination.example", false)] },
    { composedPath: () => [new f.Anchor("file:///C:/private.txt")] }, { composedPath: () => [new f.Anchor("javascript:alert(1)")] }]) {
    assert.deepEqual(f.click(overrides), { prevented: 0, stopped: 0 });
  }
  assert.equal(f.calls.length, 0);
});
