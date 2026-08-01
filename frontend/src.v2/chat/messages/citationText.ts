import type { Citation } from "../../stores/types";

const standaloneCitationMarkerPattern = /(?<![A-Za-z0-9_])\[\d{1,3}\](?=([\s，。！？；：、,.!?;:)）\[]|$))/g;

const removeModelAuthoredCitationMarkers = (content: string): string => (
  content
    .replace(standaloneCitationMarkerPattern, "")
    .replace(/[ \t]+([，。！？；：、,.!?;:])/g, "$1")
    .replace(/([（(【「『])\s+/g, "$1")
    .replace(/\s+([）)】」』])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .trim()
);

/**
 * Keep model-authored answer text intact. Structured citation metadata may
 * replace numeric markers, but source prose and links remain model-owned.
 */
export const normalizeCitationText = (content: string, citations: Citation[] = []): string => {
  const normalized = content.trim();
  return citations.length > 0
    ? removeModelAuthoredCitationMarkers(normalized)
    : normalized;
};
