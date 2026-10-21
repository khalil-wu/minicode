"use strict";

const { ipcRenderer } = require("electron");
const editedPasswords = new WeakSet();

document.addEventListener("click", event => {
  if (!event.isTrusted || event.defaultPrevented || event.button !== 0
    || !(event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)) return;
  const anchor = event.composedPath().find(node => node instanceof HTMLAnchorElement || node instanceof HTMLAreaElement || node instanceof SVGAnchorElement);
  if (!anchor) return;
  const svg = anchor instanceof SVGAnchorElement;
  if (svg ? !anchor.hasAttribute("href") && !anchor.hasAttributeNS("http://www.w3.org/1999/xlink", "href") : !anchor.hasAttribute("href")) return;
  const url = svg ? URL.parse(anchor.href.baseVal, anchor.baseURI)?.href : anchor.href;
  if (!url || !/^https?:/i.test(url)) return;
  const accepted = ipcRenderer.sendSync("minicode:browserLink:click", {
    documentUrl: location.href,
    url,
    modifiers: { ctrlKey: event.ctrlKey, metaKey: event.metaKey, altKey: event.altKey, shiftKey: event.shiftKey },
  });
  if (accepted) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

// This runs in the sandboxed isolated preload world. No IPC function or saved
// credential data is exposed to the page. Programmatic DOM input is excluded.
document.addEventListener("input", event => {
  if (event.isTrusted && event.target instanceof HTMLInputElement && event.target.type === "password") editedPasswords.add(event.target);
}, true);

document.addEventListener("submit", event => {
  if (!event.isTrusted || !(event.target instanceof HTMLFormElement) || !["http:", "https:"].includes(location.protocol)) return;
  const inputs = [...event.target.querySelectorAll("input")];
  const passwords = inputs.filter(input => input.type === "password" && !input.disabled && editedPasswords.has(input) && input.value);
  const password = passwords.find(input => input.autocomplete === "new-password") ?? passwords[0];
  if (!password) return;
  const username = inputs.find(input => input.autocomplete === "username" && !input.disabled)
    ?? inputs.slice(0, inputs.indexOf(password)).findLast(input => !input.disabled && ["email", "text"].includes(input.type));
  const oldPassword = password.autocomplete === "new-password" ? inputs.find(input => input.type === "password" && input.autocomplete === "current-password")?.value : undefined;
  ipcRenderer.send("minicode:browserPassword:submitted", { origin: location.origin, documentUrl: location.href, username: username?.value ?? "", password: password.value,
    ...(oldPassword ? { oldPassword } : {}) });
}, true);
