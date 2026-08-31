import { describe, expect, it } from "vitest";
import {
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  isBrowserScreenshotRecord,
  normalizeArtifactMediaType,
  normalizeArtifactPreview,
  recordHasImageArtifact,
} from "./artifact-projection";

describe("artifact projection contract", () => {
  it("canonicalizes an unknown declared kind when the MIME identifies an image", () => {
    expect(canonicalArtifactKind("browser_screenshot", "IMAGE/PNG; charset=binary")).toBe("image");
    expect(canonicalArtifactKind("legacy_kind", "image/webp")).toBe("image");
    expect(normalizeArtifactMediaType(" IMAGE/PNG; charset=binary ")).toBe("image/png");
  });

  it("recognizes browser screenshots across current and legacy tool records", () => {
    expect(isBrowserScreenshotRecord({
      artifactId: "shot-1",
      artifactKind: "image",
      artifactMediaType: "image/png",
      name: "browser_control",
      args: { action: "screenshot" },
      resultKind: "browser",
      activityKind: "browser",
      displaySummary: "Browser screenshot",
      summary: "",
    })).toBe(true);
    expect(isBrowserScreenshotRecord({
      artifactId: "shot-2",
      artifactKind: "browser_screenshot",
      artifactMediaType: "",
      name: "legacy_tool",
      args: {},
      resultKind: "",
      activityKind: "",
      displaySummary: "",
      summary: "",
    })).toBe(true);
    expect(isBrowserScreenshotRecord({
      artifactId: "image-1",
      artifactKind: "image",
      artifactMediaType: "image/png",
      name: "image_generation",
      args: {},
      resultKind: "image",
      activityKind: "genericTool",
      displaySummary: "Generated image",
      summary: "",
    })).toBe(false);
  });

  it("uses output file evidence and supplies a usable image MIME", () => {
    const record = {
      artifactId: "shot-3",
      artifactKind: "legacy",
      artifactMediaType: "",
      name: "browser_control",
      args: { action: "screenshot" },
      resultKind: "browser",
      activityKind: "browser",
      displaySummary: "",
      summary: "",
      outputFiles: [{ path: "shot.png", size: 12, mimeType: "image/png" }],
    } as const;
    expect(recordHasImageArtifact(record)).toBe(true);
    expect(canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)).toBe("image");
    expect(artifactMediaTypeForProjection("", "image")).toBe("image/png");
    expect(artifactSummaryForRecord(record)).toBe("浏览器截图");
  });

  it("normalizes persisted previews without changing their identity", () => {
    const normalized = normalizeArtifactPreview({
      artifactId: "legacy-image",
      kind: "browser_screenshot" as never,
      summary: " ",
      mediaType: "IMAGE/JPEG; charset=binary",
    });
    expect(normalized).toMatchObject({
      artifactId: "legacy-image",
      kind: "image",
      summary: "生成图片",
      mediaType: "image/jpeg",
    });
  });

  it("projects a sparse legacy preview from the browser screenshot summary", () => {
    const normalized = normalizeArtifactPreview({
      artifactId: "summary-only-shot",
      kind: "file",
      summary: "Browser screenshot",
    });
    expect(normalized).toMatchObject({
      artifactId: "summary-only-shot",
      kind: "image",
      summary: "Browser screenshot",
      mediaType: "image/png",
    });
  });

  it("does not infer an image from an unrelated file summary", () => {
    const normalized = normalizeArtifactPreview({
      artifactId: "ordinary-file",
      kind: "file",
      summary: "Screenshot instructions.txt",
    });
    expect(normalized.kind).toBe("file");
    expect(normalized.mediaType).toBeUndefined();
  });

  it("requires an exact historical screenshot summary when result metadata is weak", () => {
    const record = {
      artifactId: "ordinary-file",
      artifactKind: "legacy",
      artifactMediaType: "",
      name: "legacy_tool",
      args: {},
      resultKind: "browser",
      activityKind: "browser",
      displaySummary: "Screenshot instructions",
      summary: "",
    } as const;
    expect(isBrowserScreenshotRecord(record)).toBe(false);
    expect(isBrowserScreenshotRecord({ ...record, displaySummary: "Browser screenshot" })).toBe(true);
    expect(isBrowserScreenshotRecord({ ...record, displaySummary: "浏览器截图" })).toBe(true);
  });
});
