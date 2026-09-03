import type { ChatMessage, Citation, ContentBlock, ProviderRawCitation } from "../stores/types";
import { extractInlineCitationIndexes } from "../lib/markdown";

export { extractInlineCitationIndexes } from "../lib/markdown";

export const citationUrl = (citation: Citation | undefined): string => {
  const candidate = String(citation?.url || citation?.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
};

export const providerCitationsToBase = (
  providerCitations: ProviderRawCitation[] | undefined,
): Citation[] | undefined => {
  if (!providerCitations?.length) return undefined;
  const normalized = providerCitations.flatMap((citation): Citation[] => {
    const url = String(citation.url || "").trim();
    const source = String(citation.source || url).trim();
    if (!source) return [];
    let fallbackLabel = citation.title || source;
    if (url) {
      try {
        fallbackLabel = new URL(url).host.replace(/^www\./i, "");
      } catch {
        fallbackLabel = url;
      }
    }
    return [{
      source,
      ...(url ? { url } : {}),
      title: citation.title || undefined,
      locationType: citation.location_type,
      providerNative: true,
      label: citation.label || fallbackLabel,
      range: citation.range ?? [0, 0],
    }];
  });
  return normalized.length > 0 ? normalized : undefined;
};

export const resolveCitations = (
  messageCitations: ChatMessage["citations"] | undefined,
  _blocks: ContentBlock[],
  markdownSource: string,
  providerCitations?: ProviderRawCitation[],
): ChatMessage["citations"] => {
  const providerOwned = providerCitationsToBase(providerCitations);
  const citations = providerOwned ?? messageCitations ?? [];
  const citedIndexes = extractInlineCitationIndexes(markdownSource);
  // Provider-native citation metadata is authoritative even when a provider
  // renders citations without model-authored [n] markers (Anthropic does this
  // for citations_delta). Backend/tool-derived citations remain hidden unless
  // the answer explicitly binds them, so tool output cannot fabricate sources.
  if (citedIndexes.size === 0) return providerOwned ?? [];

  // Bind only citations supplied by the provider or backend. Tool output order
  // cannot establish which source a model-authored marker refers to.
  return citations.map((citation, index) =>
    citedIndexes.has(index + 1)
      ? citation
      : { source: "", range: [0, 0] as [number, number] },
  );
};
