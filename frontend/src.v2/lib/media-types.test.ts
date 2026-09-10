import { describe, expect, it } from "vitest";
import {
  isImagePath,
  isPdfPath,
  isPreviewableMediaPath,
  mediaTypeForPath,
} from "./media-types";

describe("workspace media classification", () => {
  it("classifies Windows, query-string, and encoded image paths consistently", () => {
    expect(isImagePath("C:\\workspace\\assets\\PHOTO.PNG")).toBe(true);
    expect(isImagePath("assets%2Fphoto.png?download=1")).toBe(true);
    expect(mediaTypeForPath("assets%2Fphoto.png?download=1")).toBe("image/png");
    expect(isPreviewableMediaPath("assets/photo.webp#preview")).toBe(true);
  });

  it("keeps PDFs in the same previewable-media boundary", () => {
    expect(isPdfPath("docs/report.PDF")).toBe(true);
    expect(isPreviewableMediaPath("docs/report.PDF?inline=1")).toBe(true);
  });

  it("preserves # and percent characters in literal workspace filenames", () => {
    expect(isImagePath("assets/plot#1.png")).toBe(true);
    expect(isPdfPath("C:\\workspace\\report#final.pdf")).toBe(true);
    expect(isImagePath("assets/plot%23version#2.webp")).toBe(true);
    expect(mediaTypeForPath("https://example.test/image.png#report.pdf")).toBe("image/png");
  });
});
