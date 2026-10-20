"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { createCipheriv, createDecipheriv, randomBytes } = require("node:crypto");
const { createBrowserPersonalData } = require("./browser-personal-data");

function fixture(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-personal-data-"));
  t.after(() => { assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir())); fs.rmSync(directory, { recursive: true }); });
  const key = randomBytes(32);
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString(value) {
      const iv = randomBytes(12);
      const cipher = createCipheriv("aes-256-gcm", key, iv);
      const encrypted = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), encrypted]);
    },
    decryptString(value) {
      const decipher = createDecipheriv("aes-256-gcm", key, value.subarray(0, 12));
      decipher.setAuthTag(value.subarray(12, 28));
      return Buffer.concat([decipher.update(value.subarray(28)), decipher.final()]).toString("utf8");
    },
    ...overrides,
  };
  const filePath = path.join(directory, "browser-personal-data.enc");
  return { store: createBrowserPersonalData({ filePath, safeStorage }), filePath, reopen: () => createBrowserPersonalData({ filePath, safeStorage }) };
}
const contact = { fullName: "Test Contact", organization: "Local Fixture", addressLine1: "123 Test Street", addressLine2: "", city: "Test City", region: "Test Region", postalCode: "12345", country: "US", email: "contact@example.test", phone: "123456789" };

test("password and contact persistence uses the OS encryption boundary and metadata does not return secrets", t => {
  const { store, filePath, reopen } = fixture(t);
  store.savePassword({ origin: "https://example.test/sign-in", username: "fixture-user", password: "fixture-secret-password" });
  store.saveContact(contact);
  const disk = fs.readFileSync(filePath, "utf8");
  for (const secret of ["fixture-secret-password", "fixture-user", "123 Test Street", "contact@example.test"]) assert.equal(disk.includes(secret), false);
  const reloaded = reopen();
  const summary = reloaded.listPasswords();
  assert.equal(summary.available, true);
  assert.deepEqual(Object.keys(summary.items[0]).sort(), ["id", "origin", "username"]);
  assert.equal(summary.items[0].origin, "https://example.test");
  assert.equal(reloaded.getPassword({ id: summary.items[0].id }).password, "fixture-secret-password");
  assert.equal(reloaded.listContacts()[0].addressLine1, contact.addressLine1);
});

test("password editing, duplicate identities, exact origin fill and deletion remain consistent after reopening", t => {
  const { store, reopen } = fixture(t);
  store.savePassword({ origin: "https://example.test", username: "user", password: "first" });
  const { id } = store.listPasswords().items[0];
  store.savePassword({ id, origin: "https://example.test", username: "user", password: "updated" });
  assert.equal(reopen().passwordForFill(id, "https://example.test").password, "updated");
  assert.throws(() => store.passwordForFill(id, "http://example.test"), /current website/);
  assert.throws(() => store.passwordForFill(id, "https://example.test.evil.test"), /current website/);
  assert.throws(() => store.savePassword({ origin: "https://example.test/page", username: "user", password: "duplicate" }), /already exists/);
  assert.throws(() => store.savePassword({ origin: "https://user:password@example.test", username: "user", password: "x" }), /without URL credentials/);
  store.removePassword({ id });
  assert.deepEqual(reopen().listPasswords().items, []);
  assert.throws(() => store.getPassword({ id }), /no longer exists/);
});

test("autofill choices and resolution consume the saved enable settings", t => {
  const { store, reopen } = fixture(t);
  store.savePassword({ origin: "https://example.test", username: "user", password: "secret" });
  store.saveContact(contact);
  const password = store.listPasswords().items[0];
  const savedContact = store.listContacts()[0];
  assert.equal(store.passwordsForOrigin(password.origin).length, 1);
  assert.equal(store.contactChoices()[0].label, contact.fullName);
  store.setAutofillSettings({ passwordAutofillEnabled: false, contactAutofillEnabled: false });
  assert.deepEqual(reopen().passwordsForOrigin(password.origin), []);
  assert.deepEqual(store.contactChoices(), []);
  assert.throws(() => store.passwordForFill(password.id, password.origin), /current website/);
  assert.throws(() => store.contactForFill(savedContact.id), /disabled/);
  store.setAutofillSettings({ contactAutofillEnabled: true });
  store.saveContact({ ...savedContact, city: "Updated City" });
  assert.equal(reopen().contactForFill(savedContact.id).city, "Updated City");
  store.removeContact({ id: savedContact.id });
  assert.deepEqual(reopen().listContacts(), []);
});

test("unavailable or plaintext OS storage never writes a personal-data file", t => {
  for (const settings of [{ isEncryptionAvailable: () => false }, { getSelectedStorageBackend: () => "basic_text" }]) {
    const { store, filePath } = fixture(t, settings);
    assert.equal(store.listPasswords().available, false);
    assert.throws(() => store.savePassword({ origin: "https://example.test", username: "user", password: "secret" }), /secure storage is unavailable/);
    assert.throws(() => store.listContacts(), /secure storage is unavailable/);
    assert.equal(fs.existsSync(filePath), false);
  }
});

test("an unreadable encrypted store preserves the file and surfaces the error", t => {
  const { store, filePath } = fixture(t);
  const corrupt = JSON.stringify({ version: 1, encrypted: Buffer.from("invalid cipher").toString("base64") });
  fs.writeFileSync(filePath, corrupt);
  assert.throws(() => store.listPasswords());
  assert.throws(() => store.saveContact(contact));
  assert.equal(fs.readFileSync(filePath, "utf8"), corrupt);
});
