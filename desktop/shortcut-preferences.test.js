"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const preferences = require("./preferences");

test("shortcut navigation and four modifier choices are durable without changing other preferences", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-shortcut-preferences-"));
  const file = path.join(directory, "preferences.json");
  preferences.init(file);
  assert.equal(preferences.getPreferences().primaryNumberShortcutTarget, "tabs");
  assert.equal(preferences.getPreferences().externalBrowserLinkShortcut, "primary");
  preferences.setPreferences({ primaryNumberShortcutTarget: "sidebar", notificationSound: "none" });
  for (const shortcut of ["unassigned", "primary", "alt", "primaryShift"]) {
    preferences.setPreferences({ externalBrowserLinkShortcut: shortcut });
    preferences.init(file);
    assert.equal(preferences.getPreferences().externalBrowserLinkShortcut, shortcut);
    assert.equal(preferences.getPreferences().primaryNumberShortcutTarget, "sidebar");
    assert.equal(preferences.getPreferences().notificationSound, "none");
  }
  assert.throws(() => preferences.setPreferences({ externalBrowserLinkShortcut: "ctrlAlt" }), /Unsupported desktop preference/);
});
