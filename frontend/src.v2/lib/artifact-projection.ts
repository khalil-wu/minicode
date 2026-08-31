import type { ArtifactContentState, ArtifactPreview } from "../stores/types";
import type { ToolCallRecord } from "./tool-call-reducer";

/**
 * Artifact metadata arrives from both the live tool stream and persisted
 * transcript snapshots.  Keep the UI's discriminator stable at this boundary
 * so an old `browser_screenshot` (or an unknown kind with an image MIME) does
 * not silently become a file.
 */
export type CanonicalArtifactKind = "file" | "diff" | "image" | "json" | "code" | "text";

const CANONICAL_KINDS = new Set<CanonicalArtifactKind>([
  "file",
  "diff",
  "image",
  "json",
  "code",
  "text",
]);

const BROWSER_TOOL_NAMES = new Set(["browser_control", "browser", "computer", "browser_tool"]);
const SCREENSHOT_KIND_ALIASES = new Set(["screenshot", "browser_screenshot", "browser-screenshot"]);
const SCREENSHOT_ACTION_ALIASES = new Set([
  "screenshot",
  "capture_screenshot",
  "take_screenshot",
  "capture_screen",
  "screen_capture",
]);
const SCREENSHOT_RESULT_ALIASES = new Set(["browser", "browser_screenshot", "screenshot"]);
const SCREENSHOT_SUMMARY_ALIASES = new Set(["browser screenshot", "浏览器截图"]);

export const normalizeArtifactMediaType = (value?: unknown): string =>
  String(value || "").split(";", 1)[0].trim().toLowerCase();

const normalizedValue = (value?: unknown): string => String(value || "").trim().toLowerCase();

/**
 * Older transcript snapshots sometimes retain only the artifact id and the
 * user-facing screenshot summary.  These are the two labels emitted by the
 * browser tool; keep this compatibility rule exact so an arbitrary filename
 * or prose label cannot become an image by guesswork.
 */
export const isBrowserScreenshotSummary = (value?: unknown): boolean =>
  SCREENSHOT_SUMMARY_ALIASES.has(normalizedValue(value));

export const isBrowserScreenshotRecord = (
  record: Pick<ToolCallRecord, "artifactId" | "artifactKind" | "artifactMediaType" | "name" | "args" | "resultKind" | "activityKind" | "displaySummary" | "summary">,
): boolean => {
  const artifactId = String(record.artifactId || "").trim();
  const name = normalizedValue(record.name);
  const action = normalizedValue(record.args?.action);
  const declaredKind = normalizedValue(record.artifactKind);
  const mediaType = normalizeArtifactMediaType(record.artifactMediaType);
  const resultKind = normalizedValue(record.resultKind);
  const activityKind = normalizedValue(record.activityKind);
  const hasScreenshotSummary = isBrowserScreenshotSummary(record.displaySummary)
    || isBrowserScreenshotSummary(record.summary);
  return Boolean(artifactId) && (SCREENSHOT_KIND_ALIASES.has(declaredKind)
    || (BROWSER_TOOL_NAMES.has(name) && SCREENSHOT_ACTION_ALIASES.has(action))
    || ((SCREENSHOT_RESULT_ALIASES.has(resultKind)
      || SCREENSHOT_RESULT_ALIASES.has(activityKind)
      || (resultKind === "preview" && SCREENSHOT_ACTION_ALIASES.has(action)))
      && (declaredKind === "image" || mediaType.startsWith("image/") || hasScreenshotSummary)));
};

export const recordHasImageArtifact = (
  record: Pick<ToolCallRecord, "artifactId" | "artifactKind" | "artifactMediaType" | "name" | "args" | "resultKind" | "activityKind" | "displaySummary" | "summary" | "outputFiles">,
): boolean => {
  const artifactId = String(record.artifactId || "").trim();
  if (!artifactId) return false;
  const mediaType = normalizeArtifactMediaType(record.artifactMediaType);
  const outputImage = record.outputFiles?.some((file) => (
    file.isImage === true || normalizeArtifactMediaType(file.mimeType).startsWith("image/")
  ));
  return normalizedValue(record.artifactKind) === "image"
    || mediaType.startsWith("image/")
    || outputImage === true
    || isBrowserScreenshotRecord(record);
};

export const canonicalArtifactKind = (
  declaredKind?: unknown,
  mediaType?: unknown,
  record?: Pick<ToolCallRecord, "artifactId" | "artifactKind" | "artifactMediaType" | "name" | "args" | "resultKind" | "activityKind" | "displaySummary" | "summary" | "outputFiles">,
): CanonicalArtifactKind => {
  const normalizedKind = normalizedValue(declaredKind);
  const normalizedMedia = normalizeArtifactMediaType(mediaType);
  const imageEvidence = normalizedMedia.startsWith("image/")
    || (record ? recordHasImageArtifact(record) : false)
    || normalizedKind === "image"
    || SCREENSHOT_KIND_ALIASES.has(normalizedKind);
  if (imageEvidence) return "image";
  if (CANONICAL_KINDS.has(normalizedKind as CanonicalArtifactKind)) {
    return normalizedKind as CanonicalArtifactKind;
  }
  if (normalizedMedia === "application/pdf") return "file";
  if (normalizedMedia.startsWith("text/")) return "text";
  return "file";
};

export const artifactMediaTypeForProjection = (
  mediaType: unknown,
  kind: CanonicalArtifactKind,
): string | undefined => {
  const normalized = normalizeArtifactMediaType(mediaType);
  return normalized || (kind === "image" ? "image/png" : undefined);
};

export const artifactFallbackLabel = (
  kind?: unknown,
  mediaType?: unknown,
): string => {
  const normalized = `${normalizedValue(kind)} ${normalizeArtifactMediaType(mediaType)}`;
  if (normalized.includes("image") || normalized.includes("screenshot")) return "生成图片";
  if (normalized.includes("pdf")) return "生成的 PDF";
  if (normalized.includes("file") || normalized.includes("text")) return "生成文件";
  return "未命名产物";
};

export const cleanArtifactLabel = (value?: unknown): string =>
  typeof value === "string" ? value.trim() : "";

export const artifactSummaryForRecord = (
  record: Pick<ToolCallRecord, "displaySummary" | "summary" | "name" | "args" | "artifactKind" | "artifactMediaType" | "artifactId" | "resultKind" | "activityKind" | "outputFiles">,
): string => {
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  return cleanArtifactLabel(record.displaySummary)
    || cleanArtifactLabel(record.summary)
    || (isBrowserScreenshotRecord(record) ? "浏览器截图" : artifactFallbackLabel(kind, record.artifactMediaType));
};

export const normalizeArtifactPreview = (artifact: ArtifactPreview): ArtifactPreview => {
  const summary = cleanArtifactLabel(artifact.summary);
  const kind = isBrowserScreenshotSummary(summary)
    ? "image"
    : canonicalArtifactKind(artifact.kind, artifact.mediaType);
  return {
    ...artifact,
    kind,
    summary: summary || artifactFallbackLabel(kind, artifact.mediaType),
    mediaType: artifactMediaTypeForProjection(artifact.mediaType, kind),
  };
};

const normalizedArtifactContentStates = new WeakMap<object, ArtifactContentState>();

/**
 * Normalize the richer file-preview state at the store boundary.  Unlike an
 * artifact summary, this state can carry legacy kinds such as `binary` or
 * `document`; preserve those labels while canonicalizing image evidence and
 * its missing MIME type.
 */
export const normalizeArtifactContentState = (
  artifact: ArtifactContentState,
): ArtifactContentState => {
  const cached = normalizedArtifactContentStates.get(artifact);
  if (cached) return cached;
  const kind = canonicalArtifactKind(artifact.kind, artifact.mediaType);
  const nextKind = kind === "image" ? "image" : artifact.kind;
  const nextMediaType = artifactMediaTypeForProjection(artifact.mediaType, kind);
  if (nextKind === artifact.kind && nextMediaType === artifact.mediaType) return artifact;
  const normalized = {
    ...artifact,
    kind: nextKind,
    mediaType: nextMediaType,
  };
  normalizedArtifactContentStates.set(artifact, normalized);
  return normalized;
};
