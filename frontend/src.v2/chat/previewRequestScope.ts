const GLOBAL_PREVIEW_SLOT = "__global_preview__";

interface PreviewSlotState {
  generation: number;
  controller?: AbortController;
  requestId?: string;
  objectUrl?: string;
}

export interface PreviewRequestLease {
  slot: string;
  generation: number;
  controller?: AbortController;
}

const previewSlots = new Map<string, PreviewSlotState>();

const slotFor = (conversationId?: string): string =>
  String(conversationId || "").trim() || GLOBAL_PREVIEW_SLOT;

const revokeObjectUrl = (url?: string): void => {
  if (!url) return;
  try {
    URL.revokeObjectURL(url);
  } catch {
    // The URL may belong to a test environment without a full browser API.
  }
};

export const beginPreviewRequest = (
  conversationId?: string,
  options: { abortable?: boolean } = {},
): PreviewRequestLease => {
  const slot = slotFor(conversationId);
  const previous = previewSlots.get(slot);
  previous?.controller?.abort();
  revokeObjectUrl(previous?.objectUrl);
  const controller = options.abortable ? new AbortController() : undefined;
  const generation = (previous?.generation ?? 0) + 1;
  previewSlots.set(slot, { generation, controller });
  return { slot, generation, controller };
};

export const isPreviewRequestCurrent = (lease: PreviewRequestLease): boolean => {
  const current = previewSlots.get(lease.slot);
  return Boolean(current && current.generation === lease.generation);
};

export const setPreviewRequestId = (lease: PreviewRequestLease, requestId: string): boolean => {
  const current = previewSlots.get(lease.slot);
  if (!current || current.generation !== lease.generation) return false;
  current.requestId = requestId;
  return true;
};

export const matchesPreviewRequestId = (
  conversationId: string | undefined,
  requestId: string | undefined,
): boolean => {
  const current = previewSlots.get(slotFor(conversationId));
  return Boolean(requestId && current?.requestId === requestId);
};

export const setPreviewObjectUrl = (lease: PreviewRequestLease, url: string): boolean => {
  const current = previewSlots.get(lease.slot);
  if (!current || current.generation !== lease.generation) {
    revokeObjectUrl(url);
    return false;
  }
  revokeObjectUrl(current.objectUrl);
  current.objectUrl = url;
  return true;
};

export const releasePreviewScope = (conversationId?: string): void => {
  const slot = slotFor(conversationId);
  const current = previewSlots.get(slot);
  current?.controller?.abort();
  revokeObjectUrl(current?.objectUrl);
  previewSlots.delete(slot);
};

export const resetPreviewRequestScopesForTests = (): void => {
  for (const slot of previewSlots.values()) {
    slot.controller?.abort();
    revokeObjectUrl(slot.objectUrl);
  }
  previewSlots.clear();
};
