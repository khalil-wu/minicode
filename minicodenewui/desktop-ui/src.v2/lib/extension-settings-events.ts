import { apiBase, authHeaders } from "../protocol/api";

export type ExtensionSettingsKind = "plugins" | "skills" | "marketplaces";
export type ExtensionSettingsOwner = { base: string; token: string };
type ExtensionSettingsChange = { kind: ExtensionSettingsKind; owner: ExtensionSettingsOwner; source: object };
const eventName = "extensions:settings-committed";

export const captureExtensionSettingsOwner = (): ExtensionSettingsOwner => ({
  base: apiBase(), token: new Headers(authHeaders()).get("X-MiniCode-Token") || "",
});

export function extensionSettingsOwnerMatches(owner: ExtensionSettingsOwner): boolean {
  const current = captureExtensionSettingsOwner();
  return owner.base === current.base && owner.token === current.token;
}

export function publishExtensionSettingsChange(kind: ExtensionSettingsKind, owner: ExtensionSettingsOwner, source: object): void {
  window.dispatchEvent(new CustomEvent<ExtensionSettingsChange>(eventName, { detail: { kind, owner, source } }));
}

export function subscribeExtensionSettingsChanges(kind: ExtensionSettingsKind, source: object, listener: () => void): () => void {
  const receive = (event: Event) => {
    const change = (event as CustomEvent<ExtensionSettingsChange>).detail;
    if (change.kind === kind && change.source !== source && extensionSettingsOwnerMatches(change.owner)) listener();
  };
  window.addEventListener(eventName, receive);
  return () => window.removeEventListener(eventName, receive);
}
