/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_IMAGE_SOURCE_BYTES,
  MAX_NATIVE_IMAGE_BYTES,
  prepareNativeImageFile,
} from "./imagePreparation";

describe("prepareNativeImageFile", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps a small image unchanged when native decoding is unavailable", async () => {
    vi.stubGlobal("createImageBitmap", undefined);
    const file = new File(["small"], "small.png", { type: "image/png" });

    await expect(prepareNativeImageFile(file)).resolves.toBe(file);
  });

  it("resizes an oversized image into the provider envelope", async () => {
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 4000, height: 3000, close })));
    const drawImage = vi.fn();
    const clearRect = vi.fn();
    const canvas = {
      width: 0,
      height: 0,
      getContext: vi.fn(() => ({ drawImage, clearRect })),
      toBlob: (callback: BlobCallback, type?: string) => callback(new Blob(["compressed"], { type })),
    } as unknown as HTMLCanvasElement;
    vi.spyOn(document, "createElement").mockReturnValue(canvas);
    const file = new File([new Uint8Array(MAX_NATIVE_IMAGE_BYTES + 1)], "large.png", { type: "image/png" });

    const prepared = await prepareNativeImageFile(file);

    expect(prepared.size).toBeLessThanOrEqual(MAX_NATIVE_IMAGE_BYTES);
    expect(prepared.type).toBe("image/png");
    expect(canvas.width).toBe(2000);
    expect(canvas.height).toBe(1500);
    expect(drawImage).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("rejects source images above Claude Code's 20 MiB processing ceiling", async () => {
    const file = new File([new Uint8Array(MAX_IMAGE_SOURCE_BYTES + 1)], "huge.png", { type: "image/png" });

    await expect(prepareNativeImageFile(file)).rejects.toThrow("超过 20 MB");
  });
});
