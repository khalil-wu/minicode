import { afterEach, describe, expect, it, vi } from "vitest";
import {
  beginPreviewRequest,
  isPreviewRequestCurrent,
  matchesPreviewRequestId,
  releasePreviewScope,
  resetPreviewRequestScopesForTests,
  setPreviewObjectUrl,
  setPreviewRequestId,
} from "./previewRequestScope";

afterEach(() => {
  resetPreviewRequestScopesForTests();
  vi.restoreAllMocks();
});

describe("preview request scope lifetime", () => {
  it.each([undefined, "conv-a"])("does not revive released leases for %s", (conversationId) => {
    const old = beginPreviewRequest(conversationId, { abortable: true });
    releasePreviewScope(conversationId);
    const current = beginPreviewRequest(conversationId);

    expect(old.controller?.signal.aborted).toBe(true);
    expect(current.generation).not.toBe(old.generation);
    expect(isPreviewRequestCurrent(old)).toBe(false);
    expect(isPreviewRequestCurrent(current)).toBe(true);
  });

  it("keeps request IDs owned by the reopened scope", () => {
    const old = beginPreviewRequest("conv-a");
    releasePreviewScope("conv-a");
    const current = beginPreviewRequest("conv-a");

    expect(setPreviewRequestId(current, "current-request")).toBe(true);
    expect(setPreviewRequestId(old, "late-request")).toBe(false);
    expect(matchesPreviewRequestId("conv-a", "current-request")).toBe(true);
    expect(matchesPreviewRequestId("conv-a", "late-request")).toBe(false);
  });

  it("revokes stale object URLs without replacing the reopened preview", () => {
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const old = beginPreviewRequest("conv-a");
    setPreviewObjectUrl(old, "blob:old");
    releasePreviewScope("conv-a");
    const current = beginPreviewRequest("conv-a");

    expect(setPreviewObjectUrl(current, "blob:current")).toBe(true);
    expect(setPreviewObjectUrl(old, "blob:late")).toBe(false);
    expect(revoke.mock.calls).toEqual([["blob:old"], ["blob:late"]]);
  });

  it("does not invalidate requests in other conversation slots", () => {
    const first = beginPreviewRequest("conv-a");
    const second = beginPreviewRequest("conv-b");
    releasePreviewScope("conv-a");
    beginPreviewRequest("conv-a");

    expect(isPreviewRequestCurrent(first)).toBe(false);
    expect(isPreviewRequestCurrent(second)).toBe(true);
  });
});
