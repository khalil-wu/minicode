import type { ChatMessage, Citation, ContentBlock, ProviderRawCitation } from "../stores/types";

export const citationUrl = (citation: Citation | undefined): string => {
  const candidate = String(citation?.url || citation?.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
};

export const extractInlineCitationIndexes = (content: string): Set<number> => {
  const indexes = new Set<number>();
  for (const match of content.matchAll(/\[(\d+)\]/g)) {
    const index = Number(match[1]);
    if (Number.isFinite(index) && index > 0) indexes.add(index);
  }
  return indexes;
};

export const providerCitationsToBase = (
  providerCitations: ProviderRawCitation[] | undefined,
): Citation[] | undefined => {
  if (!providerCitations?.length) return undefined;
  return providerCitations.map((citation) => ({
    source: citation.url,
    url: citation.url,
    title: citation.title || undefined,
    label: (() => {
      try {
        return new URL(citation.url).host.replace(/^www\./i, "");
      } catch {
        return citation.url;
      }
    })(),
    range: citation.range ?? [0, 0],
  }));
};

export const resolveCitations = (
  messageCitations: ChatMessage["citations"] | undefined,
  _blocks: ContentBlock[],
  markdownSource: string,
  providerCitations?: ProviderRawCitation[],
): ChatMessage["citations"] => {
  const citations = providerCitationsToBase(providerCitations) ?? messageCitations ?? [];
  const citedIndexes = extractInlineCitationIndexes(markdownSource);
  if (citedIndexes.size === 0) return [];

  // Bind only citations supplied by the provider or backend. Tool output order
  // cannot establish which source a model-authored marker refers to.
  return citations.map((citation, index) =>
    citedIndexes.has(index + 1)
      ? citation
      : { source: "", range: [0, 0] as [number, number] },
  );
};
