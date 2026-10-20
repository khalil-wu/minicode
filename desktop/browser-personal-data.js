"use strict";

const fs = require("node:fs");
const { randomUUID } = require("node:crypto");
const { atomicWriteTextSync } = require("./utils");

const CONTACT_FIELDS = ["fullName", "organization", "addressLine1", "addressLine2", "city", "region", "postalCode", "country", "email", "phone"];

function passwordOrigin(value) {
  const parsed = new URL(value);
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("Password websites must use HTTP or HTTPS without URL credentials.");
  return parsed.origin;
}

function createBrowserPersonalData({ filePath, safeStorage }) {
  let data;
  const available = () => safeStorage.isEncryptionAvailable() && safeStorage.getSelectedStorageBackend?.() !== "basic_text";
  function load() {
    if (!available()) throw new Error("The operating system's secure storage is unavailable.");
    if (data !== undefined) return data;
    if (!fs.existsSync(filePath)) {
      data = { passwordAutofillEnabled: true, contactAutofillEnabled: true, passwords: [], contacts: [] };
      return data;
    }
    const stored = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (stored.version !== 1 || typeof stored.encrypted !== "string") throw new Error("Unsupported browser personal data file.");
    const decoded = JSON.parse(safeStorage.decryptString(Buffer.from(stored.encrypted, "base64")));
    if (!Array.isArray(decoded.passwords) || !Array.isArray(decoded.contacts)
      || typeof decoded.passwordAutofillEnabled !== "boolean" || typeof decoded.contactAutofillEnabled !== "boolean") throw new Error("Invalid browser personal data.");
    data = decoded;
    return data;
  }
  function commit(next) {
    const encrypted = safeStorage.encryptString(JSON.stringify(next)).toString("base64");
    atomicWriteTextSync(filePath, JSON.stringify({ version: 1, encrypted }));
    data = next;
  }
  function passwordById(id) {
    const item = load().passwords.find(record => record.id === id);
    if (!item) throw new Error("The saved password no longer exists.");
    return item;
  }
  function listPasswords() {
    if (!available()) return { available: false, passwordAutofillEnabled: false, contactAutofillEnabled: false, items: [] };
    const current = load();
    return { available: true, passwordAutofillEnabled: current.passwordAutofillEnabled, contactAutofillEnabled: current.contactAutofillEnabled,
      items: current.passwords.map(({ id, origin, username }) => ({ id, origin, username })) };
  }
  function getPassword({ id }) { return { ...passwordById(id) }; }
  function savePassword(raw) {
    if (typeof raw.origin !== "string" || typeof raw.username !== "string" || typeof raw.password !== "string" || !raw.password) throw new Error("Enter a website and password.");
    const current = load();
    if (raw.id) passwordById(raw.id);
    const record = { id: raw.id || randomUUID(), origin: passwordOrigin(raw.origin), username: raw.username.trim(), password: raw.password };
    if (current.passwords.some(item => item.id !== record.id && item.origin === record.origin && item.username === record.username)) throw new Error("A password for this website and username already exists.");
    commit({ ...current, passwords: [...current.passwords.filter(item => item.id !== record.id), record] });
  }
  function removePassword({ id }) {
    passwordById(id);
    const current = load();
    commit({ ...current, passwords: current.passwords.filter(item => item.id !== id) });
  }
  function listContacts() { return load().contacts.map(item => ({ ...item })); }
  function saveContact(raw) {
    const record = { id: raw.id || randomUUID() };
    for (const field of CONTACT_FIELDS) {
      if (typeof raw[field] !== "string") throw new Error("Contact fields must contain text.");
      record[field] = raw[field].trim();
    }
    if (!CONTACT_FIELDS.some(field => record[field])) throw new Error("Enter contact information.");
    const current = load();
    if (raw.id && !current.contacts.some(item => item.id === raw.id)) throw new Error("The saved contact no longer exists.");
    commit({ ...current, contacts: [...current.contacts.filter(item => item.id !== record.id), record] });
  }
  function removeContact({ id }) {
    const current = load();
    if (!current.contacts.some(item => item.id === id)) throw new Error("The saved contact no longer exists.");
    commit({ ...current, contacts: current.contacts.filter(item => item.id !== id) });
  }
  function setAutofillSettings(patch) {
    const next = { ...load() };
    for (const field of ["passwordAutofillEnabled", "contactAutofillEnabled"]) {
      if (patch[field] !== undefined) {
        if (typeof patch[field] !== "boolean") throw new Error("Autofill settings must be boolean values.");
        next[field] = patch[field];
      }
    }
    commit(next);
    return listPasswords();
  }
  function passwordsForOrigin(origin) {
    const current = load();
    return current.passwordAutofillEnabled ? current.passwords.filter(item => item.origin === origin).map(({ id, username }) => ({ id, username })) : [];
  }
  function contactChoices() {
    const current = load();
    return current.contactAutofillEnabled ? current.contacts.map(({ id, fullName, email, addressLine1, phone }) => ({ id, label: fullName || email || addressLine1 || phone })) : [];
  }
  function passwordForFill(id, origin) {
    const item = passwordById(id);
    if (!load().passwordAutofillEnabled || item.origin !== origin) throw new Error("This password is not available for the current website.");
    return { username: item.username, password: item.password };
  }
  function contactForFill(id) {
    const current = load();
    if (!current.contactAutofillEnabled) throw new Error("Contact autofill is disabled.");
    const contact = current.contacts.find(item => item.id === id);
    if (!contact) throw new Error("The saved contact no longer exists.");
    return { ...contact };
  }
  return { listPasswords, getPassword, savePassword, removePassword, listContacts, saveContact, removeContact, setAutofillSettings,
    passwordsForOrigin, contactChoices, passwordForFill, contactForFill };
}

module.exports = { createBrowserPersonalData, passwordOrigin, CONTACT_FIELDS };
