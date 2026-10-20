export type LinkClickModifiers = Pick<MouseEvent, "ctrlKey" | "metaKey" | "altKey" | "shiftKey">;
export type ExternalBrowserLinkShortcut = "unassigned" | "primary" | "alt" | "primaryShift";
export type PrimaryNumberShortcutTarget = "tabs" | "sidebar";

export function matchesExternalBrowserClick(event: LinkClickModifiers, shortcut: ExternalBrowserLinkShortcut, mac = /Mac/i.test(navigator.platform)): boolean {
  const primary = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  if (shortcut === "alt") return event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey;
  if (shortcut === "primary" || shortcut === "primaryShift") return primary && !event.altKey && event.shiftKey === (shortcut === "primaryShift");
  return false;
}

export function numberedShortcut(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">, primaryTarget: PrimaryNumberShortcutTarget, mac = /Mac/i.test(navigator.platform)): { target: PrimaryNumberShortcutTarget; index: number } | null {
  if (!/^[1-9]$/.test(event.key) || event.shiftKey) return null;
  const primary = mac ? event.metaKey && !event.ctrlKey && !event.altKey : event.ctrlKey && !event.metaKey && !event.altKey;
  const secondary = mac ? event.ctrlKey && !event.metaKey && !event.altKey : event.altKey && !event.ctrlKey && !event.metaKey;
  if (!primary && !secondary) return null;
  return { target: primary ? primaryTarget : primaryTarget === "tabs" ? "sidebar" : "tabs", index: Number(event.key) - 1 };
}
