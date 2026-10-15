export const draftWindowOwnerId = (): string => {
  const nativeOwner = window.__MINICODE_RUNTIME__?.windowOwnerId;
  if (nativeOwner) return nativeOwner;
  const key = "minicode.composer.window_owner";
  const existing = sessionStorage.getItem(key);
  if (existing) return existing;
  const owner = `browser-${crypto.randomUUID()}`;
  sessionStorage.setItem(key, owner);
  return owner;
};

export const windowDraftWorkspaceKey = (workspace: string, ownerId = draftWindowOwnerId()): string =>
  JSON.stringify([ownerId, workspace]);

export const canClaimLegacyEditorDrafts = (ownerId = draftWindowOwnerId()): boolean =>
  ownerId === "primary" || ownerId.startsWith("browser-");
