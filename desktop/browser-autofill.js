"use strict";
const { randomUUID } = require("node:crypto");

// Values are resolved only after the owner selects a native menu item. The
// browser bridge and guest renderer never receive a personal-data IPC surface.
function fillBrowserForm(kind, values, expectedOrigin, handle) {
  if (kind === "release") { delete globalThis[handle]; return true; }
  if (location.origin !== expectedOrigin) return false;
  const writable = field => !field.disabled && !field.readOnly && field.type !== "hidden";
  const contactToken = field => field.autocomplete.trim().split(/\s+/).at(-1);
  const contactTokens = ["name", "given-name", "family-name", "organization", "street-address", "address-line1", "address-line2", "address-level2", "address-level1", "postal-code", "country", "country-name", "email", "tel"];
  if (kind === "prepare") {
    const active = document.activeElement;
    if (!(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) || !writable(active)) return null;
    const scope = active.form || document;
    const inputs = Array.from(scope.querySelectorAll("input, textarea, select")).filter(field => field.form === active.form);
    const password = active.type === "password" && active.autocomplete !== "new-password" ? active
      : inputs.slice(inputs.indexOf(active) + 1).find(field => writable(field) && field.type === "password" && field.autocomplete !== "new-password");
    const username = password ? inputs.slice(0, inputs.indexOf(password)).findLast(field => writable(field)
      && (field.autocomplete === "username" || field.type === "email" || field.type === "text")) : null;
    const contacts = inputs.filter(field => writable(field) && !["password", "checkbox", "radio", "submit", "button", "file"].includes(field.type)
      && (contactTokens.includes(contactToken(field)) || field.type === "email" || field.type === "tel"));
    const choices = { password: Boolean(password && (active === password || active === username)), contact: contacts.includes(active) };
    if (!choices.password && !choices.contact) return null;
    Object.defineProperty(globalThis, handle, { configurable: true, value: { document, active, scope, password, username, contacts, choices } });
    return choices;
  }
  const selection = globalThis[handle];
  delete globalThis[handle];
  if (!selection || selection.document !== document || !selection.active.isConnected || selection.active.ownerDocument !== document
    || selection.scope !== (selection.active.form || document) || !selection.choices[kind]) return false;
  const owned = field => field.isConnected && field.ownerDocument === document
    && (field.form || document) === selection.scope && writable(field);
  const assign = (field, value) => {
    const prototype = field instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  };
  if (kind === "password") {
    if (!owned(selection.password) || selection.password.type !== "password" || selection.password.autocomplete === "new-password") return false;
    if (selection.username && owned(selection.username)) assign(selection.username, values.username);
    assign(selection.password, values.password);
    return true;
  }
  const nameParts = values.fullName.split(/\s+/);
  const fields = { name: values.fullName, "given-name": nameParts[0], "family-name": nameParts.slice(1).join(" "), organization: values.organization,
    "street-address": [values.addressLine1, values.addressLine2].filter(Boolean).join("\n"), "address-line1": values.addressLine1,
    "address-line2": values.addressLine2, "address-level2": values.city, "address-level1": values.region,
    "postal-code": values.postalCode, country: values.country, "country-name": values.country, email: values.email, tel: values.phone };
  let filled = false;
  for (const field of selection.contacts) {
    if (!owned(field) || ["password", "checkbox", "radio", "submit", "button", "file"].includes(field.type)) continue;
    const token = contactToken(field);
    const value = fields[token] || (field.type === "email" ? values.email : field.type === "tel" ? values.phone : "");
    if (!value) continue;
    assign(field, value);
    filled = true;
  }
  return filled;
}

function attachGuestAutofill(entry, getRepository, { Menu, dialog, text, appendDesktopLog }) {
  const contents = entry.view.webContents;
  const documents = new WeakMap();
  let menuSequence = 0;
  contents.on("did-start-navigation", details => {
    if (details.frame) documents.set(details.frame, (documents.get(details.frame) ?? 0) + 1);
  });
  const openMenu = async params => {
    const repository = getRepository();
    if (!repository || !params.isEditable || entry.window.isDestroyed()) return;
    const parsed = new URL(contents.getURL());
    const frame = params.frame;
    if (!["http:", "https:"].includes(parsed.protocol) || !frame || frame.isDestroyed() || frame.detached || frame.origin !== parsed.origin) return;
    const documentVersion = documents.get(frame) ?? 0;
    const navigation = entry.navigationVersion;
    const sequence = ++menuSequence;
    const handle = `__minicode_autofill_${randomUUID().replaceAll("-", "")}`;
    const current = () => !contents.isDestroyed() && !frame.isDestroyed() && !frame.detached
      && entry.navigationVersion === navigation && (documents.get(frame) ?? 0) === documentVersion;
    const evaluate = (kind, values = null) => frame.executeJavaScript(`(${fillBrowserForm.toString()})(${JSON.stringify(kind)},${JSON.stringify(values)},${JSON.stringify(parsed.origin)},${JSON.stringify(handle)})`, kind === "password" || kind === "contact");
    let prepared = false;
    let values;
    try {
      const choices = await evaluate("prepare");
      prepared = Boolean(choices);
      if (!choices || sequence !== menuSequence || !current()) return;
      const status = repository.listPasswords();
      if (!status.available) return;
      const passwords = choices.password ? repository.passwordsForOrigin(parsed.origin) : [];
      const contacts = choices.contact ? repository.contactChoices() : [];
      const selected = await new Promise(resolve => {
        const template = [];
        if (passwords.length) template.push({ label: text("Passwords", "密码"), submenu: passwords.map(item => ({ label: item.username || parsed.hostname, click: () => resolve({ kind: "password", id: item.id }) })) });
        if (contacts.length) template.push({ label: text("Contact info", "联系信息"), submenu: contacts.map(item => ({ label: item.label, click: () => resolve({ kind: "contact", id: item.id }) })) });
        if (!template.length) { resolve(null); return; }
        template.push({ type: "separator" }, { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" });
        Menu.buildFromTemplate(template).popup({ window: entry.window, frame, callback: () => resolve(null) });
      });
      if (!selected) return;
      if (!current() || sequence !== menuSequence || frame.origin !== parsed.origin) throw new Error(text("The browser page changed.", "浏览器页面已变化。"));
      values = selected.kind === "password" ? repository.passwordForFill(selected.id, parsed.origin) : repository.contactForFill(selected.id);
      const filled = await evaluate(selected.kind, values);
      prepared = false;
      if (!filled) throw new Error(text("This form has no matching fields.", "此表单没有可填写的字段。"));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const detail = values?.password ? message.replaceAll(values.password, "[redacted]") : message;
      if (!entry.window.isDestroyed()) await dialog.showMessageBox(entry.window, { type: "error", message: text("Could not fill this form.", "无法填写此表单。"), detail, buttons: [text("OK", "好")] });
      else appendDesktopLog(`[browser-autofill] ${detail}`);
    } finally {
      if (prepared && !frame.isDestroyed() && !frame.detached) {
        await evaluate("release").catch(error => {
          const detail = values?.password ? String(error).replaceAll(values.password, "[redacted]") : String(error);
          appendDesktopLog(`[browser-autofill] form target release failed: ${detail}`);
        });
      }
    }
  };
  contents.on("context-menu", (_event, params) => {
    void openMenu(params).catch(error => appendDesktopLog(`[browser-autofill] ${String(error)}`));
  });
}

module.exports = { attachGuestAutofill, fillBrowserForm };
