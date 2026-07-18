import type { ChatMessage, Citation, ContentBlock, ProviderRawCitation } from "../stores/types";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

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

type IndexedWebSource = { url: string; title: string };

const cleanUrl = (value: string): string => (
  value.trim().replace(/[)>.,;，。；]+$/u, "")
);

const stringArg = (value: unknown): string => (
  typeof value === "string" ? value.trim() : ""
);

const directRecordUrl = (record: ToolCallRecord): string => {
  const candidate =
    record.sourceUrl ||
    stringArg(record.args.url) ||
    stringArg(record.args.source_url);
  return /^https?:\/\//i.test(candidate) ? cleanUrl(candidate) : "";
};

const hostLabel = (url: string): string => {
  try {
    return new URL(url).host.replace(/^www\./i, "");
  } catch {
    return url;
  }
};

const resultKindForRecord = (record: ToolCallRecord): string =>
  String(record.resultKind || record.activityKind || "").toLowerCase();

const isSuccessfulRecord = (record: ToolCallRecord): boolean =>
  record.status === "success" || record.status === "partial";

const isWebLikeRecord = (record: ToolCallRecord): boolean => {
  const name = record.name || "";
  const resultKind = resultKindForRecord(record);
  return (
    resultKind === "web" ||
    resultKind === "search" ||
    /^(?:web_search|web_fetch|search_web)$/i.test(name) ||
    /^mcp__websearch__/i.test(name) ||
    Boolean(directRecordUrl(record))
  );
};

const isFetchedEvidenceRecord = (record: ToolCallRecord): boolean => {
  if (!isSuccessfulRecord(record) || !isWebLikeRecord(record)) return false;
  if (String(record.extractionStatus || "").toLowerCase() === "failed") return false;
  if (String(record.evidenceType || "").toLowerCase() === "candidate") return false;
  return Boolean(directRecordUrl(record));
};

const recordTitle = (record: ToolCallRecord, url: string): string => {
  const summary = [
    record.displaySummary,
    record.summary,
    record.inputSummary,
  ].find((value) => typeof value === "string" && value.trim());
  if (!summary) return hostLabel(url);
  return summary
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line && !/^url:/i.test(line) && !/^https?:\/\//i.test(line)) ||
    hostLabel(url);
};

const recordsFromBlocks = (blocks: ContentBlock[]): ToolCallRecord[] =>
  blocks
    .filter((block): block is Extract<ContentBlock, { type: "tool_call" }> => block.type === "tool_call")
    .map((block) => block.record);

const extractIndexedSourcesFromText = (text: string): Map<number, IndexedWebSource> => {
  const results = new Map<number, IndexedWebSource>();
  if (!text.includes("URL:") && !/\[\d+\].*https?:\/\//i.test(text)) return results;

  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const itemMatch = lines[i].trim().match(/^\[(\d+)\]\s+(.+)$/);
    if (!itemMatch) continue;
    const index = Number(itemMatch[1]);
    if (!Number.isFinite(index) || index <= 0) continue;
    let title = itemMatch[2].trim();
    let url = "";
    const inlineUrl = title.match(/https?:\/\/\S+/i)?.[0];
    if (inlineUrl) {
      url = cleanUrl(inlineUrl);
      title = title.replace(inlineUrl, "").trim();
    }
    for (let j = i + 1; !url && j < Math.min(i + 4, lines.length); j += 1) {
      const urlMatch = lines[j].trim().match(/^URL:\s*(https?:\/\/\S+)/i);
      if (urlMatch) url = cleanUrl(urlMatch[1]);
    }
    if (url) results.set(index, { url, title: title || hostLabel(url) });
  }
  return results;
};

export const extractWebSearchIndexedSources = (
  blocks: ContentBlock[],
): Map<number, IndexedWebSource> => {
  const records = recordsFromBlocks(blocks).filter((record) =>
    isSuccessfulRecord(record) &&
    isWebLikeRecord(record) &&
    String(record.evidenceType || "").toLowerCase() !== "candidate",
  );

  const indexedByRecord = records
    .map((record) => extractIndexedSourcesFromText([
      record.summary,
      record.displaySummary,
      record.contentPreview,
      record.outputPreview,
    ].filter((value): value is string => typeof value === "string" && value.trim().length > 0).join("\n")))
    .filter((sources) => sources.size > 0);

  const results = new Map<number, IndexedWebSource>(indexedByRecord.at(-1) ?? []);
  const seenUrls = new Set([...results.values()].map((source) => source.url));
  let fallbackIndex = 1;
  for (const record of records) {
    if (!isFetchedEvidenceRecord(record)) continue;
    const url = directRecordUrl(record);
    if (!url || seenUrls.has(url)) continue;
    seenUrls.add(url);
    while (results.has(fallbackIndex)) fallbackIndex += 1;
    results.set(fallbackIndex, { url, title: recordTitle(record, url) });
    fallbackIndex += 1;
  }
  return results;
};

/**
 * Convert provider-native citations (from LLM response annotations) into
 * the frontend Citation shape. Provider citations are indexed by their
 * order of appearance so [1] [2] markers in the answer text bind to them.
 */
export const providerCitationsToBase = (
  providerCitations: ProviderRawCitation[] | undefined,
): Citation[] | undefined => {
  if (!providerCitations || providerCitations.length === 0) return undefined;
  return providerCitations.map((c) => ({
    source: c.url,
    url: c.url,
    title: c.title || undefined,
    label: (() => {
      try { return new URL(c.url).host.replace(/^www\./i, ""); }
      catch { return c.url; }
    })(),
    range: c.range ?? [0, 0],
  }));
};

export const mergeCitationsWithWebSearchFallback = (
  messageCitations: ChatMessage["citations"] | undefined,
  blocks: ContentBlock[],
  markdownSource: string,
  providerCitations?: ProviderRawCitation[],
): ChatMessage["citations"] => {
  const providerBase = providerCitationsToBase(providerCitations);
  // Provider citations take priority: they are the model's own source
  // bindings, more authoritative than tool-call heuristics.
  const base = [...(providerBase ?? messageCitations ?? [])];
  const citedIndexes = extractInlineCitationIndexes(markdownSource);
  if (citedIndexes.size === 0) {
    return [];
  }

  if (base.some((citation) => citationUrl(citation))) {
    return base.map((citation, index) =>
      citedIndexes.has(index + 1) ? citation : { source: "", range: [0, 0] as [number, number] },
    );
  }

  const indexedSources = extractWebSearchIndexedSources(blocks);
  const fallbackIndexes = [...indexedSources.keys()].filter((index) => citedIndexes.has(index));
  const boundedFallbackIndexes = fallbackIndexes.slice(0, 3);

  const maxIndex = Math.max(base.length, ...citedIndexes, ...boundedFallbackIndexes);
  const merged: Citation[] = Array.from(
    { length: maxIndex },
    (_, idx) => base[idx] ?? { source: "", range: [0, 0] as [number, number] },
  );

  for (const index of boundedFallbackIndexes) {
    const source = indexedSources.get(index);
    if (!source) continue;
    const pos = index - 1;
    const existing = merged[pos];
    if (citationUrl(existing)) continue;
    merged[pos] = {
      source: source.url,
      url: source.url,
      label: (() => {
        try {
          return new URL(source.url).host.replace(/^www\./i, "");
        } catch {
          return source.url;
        }
      })(),
      title: source.title,
      range: [0, 0],
    };
  }
  return merged.map((citation, index) =>
    citedIndexes.has(index + 1) ? citation : { source: "", range: [0, 0] as [number, number] },
  );
};
