import {
  artifactRawResourceUrlWithToken,
  attachmentRawResourceUrlWithToken,
} from "../protocol/api";

/** Sources whose bytes are protected by the session/conversation owner scope. */
export type ScopedArtifactSource = "artifact" | "attachment";

export interface ArtifactImageResourceInput {
  artifactId?: string;
  conversationId?: string;
  sessionId?: string;
  source?: ScopedArtifactSource | "workspace" | "local";
  originalUrl?: string;
  isConnected: boolean;
}

const INLINE_IMAGE_URL = /^(?:data:image\/(?:png|jpe?g|gif|webp|avif);base64,[a-z0-9+/=\s]+|blob:)/i;

export const normalizeArtifactMediaType = (value?: unknown): string =>
  String(value || "").split(";", 1)[0].trim().toLowerCase();

export const isDisplayableImageMediaType = (value?: unknown): boolean => {
  const mediaType = normalizeArtifactMediaType(value);
  return mediaType.startsWith("image/") && mediaType !== "image/svg+xml";
};

/**
 * Browser screenshots and generated images are represented either by a
 * trusted inline bitmap while it is live, or by an owner-scoped raw endpoint
 * once persisted. SVG is deliberately not part of this preview surface.
 */
export const inlineImageResourceUrl = (value?: unknown): string => {
  const url = String(value || "").trim();
  return INLINE_IMAGE_URL.test(url) ? url : "";
};

export const isInlineImageResourceUrl = (value?: unknown): boolean =>
  Boolean(inlineImageResourceUrl(value));

/**
 * Build the one canonical URL for a preview image. Persisted artifacts never
 * reuse a supplied URL: a reconnect must rebuild the signed owner-scoped URL
 * from the current transport session instead of retaining a stale credential.
 */
export const artifactImageResourceUrl = ({
  artifactId,
  conversationId,
  sessionId,
  source,
  originalUrl,
  isConnected,
}: ArtifactImageResourceInput): string => {
  const inlineUrl = inlineImageResourceUrl(originalUrl);
  if (inlineUrl) return inlineUrl;

  const id = String(artifactId || "").trim();
  const owner = String(conversationId || "").trim();
  const session = String(sessionId || "").trim();
  if (source === "artifact") {
    return isConnected && id && owner && session
      ? artifactRawResourceUrlWithToken(id, session, owner)
      : "";
  }
  if (source === "attachment") {
    return isConnected && id && owner && session
      ? attachmentRawResourceUrlWithToken(id, session, owner)
      : "";
  }
  return String(originalUrl || "").trim();
};

/** Add a cache-busting nonce without changing inline URLs or their payload. */
export const withPreviewCacheBust = (url: string, attempt: number): string => {
  const normalized = String(url || "").trim();
  const retry = Math.max(0, Math.floor(Number(attempt) || 0));
  if (!normalized || retry === 0 || isInlineImageResourceUrl(normalized)) return normalized;

  const hashIndex = normalized.indexOf("#");
  const base = hashIndex >= 0 ? normalized.slice(0, hashIndex) : normalized;
  const hash = hashIndex >= 0 ? normalized.slice(hashIndex) : "";
  return `${base}${base.includes("?") ? "&" : "?"}preview_retry=${retry}${hash}`;
};
