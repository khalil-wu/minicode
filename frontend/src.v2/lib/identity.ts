/**
 * Stable identity helpers shared by stream, runtime, and notice projections.
 */

export const eventMessageId = (event: unknown): string | undefined => {
  const payload = event as { message_id?: unknown; messageId?: unknown } | null | undefined;
  const value = payload?.message_id ?? payload?.messageId;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

/** FNV-1a text hash used for deterministic local projection identifiers. */
export const stableTextHash = (value: string): string => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
};
