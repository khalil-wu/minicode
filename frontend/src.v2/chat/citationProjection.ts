import type { ChatMessage, Citation, ContentBlock } from "../stores/types";

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

export const extractWebSearchIndexedSources = (
  blocks: ContentBlock[],
): Map<number, { url: string; title: string }> => {
  const results = new Map<number, { url: string; title: string }>();
  const webSearchRecords = blocks
    .filter((block): block is Extract<ContentBlock, { type: "tool_call" }> => block.type === "tool_call")
    .map((block) => block.record)
    .filter((record) =>
      record.name === "web_search" &&
      record.status === "success" &&
      typeof record.summary === "string" &&
      record.summary.includes("URL:"),
    );
  const latest = webSearchRecords.at(-1);
  if (!latest?.summary) return results;

  const lines = latest.summary.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const itemMatch = lines[i].trim().match(/^\[(\d+)\]\s+(.+)$/);
    if (!itemMatch) continue;
    const index = Number(itemMatch[1]);
    if (!Number.isFinite(index) || index <= 0) continue;
    const title = itemMatch[2].trim();
    let url = "";
    for (let j = i + 1; j < Math.min(i + 4, lines.length); j += 1) {
      const urlMatch = lines[j].trim().match(/^URL:\s*(https?:\/\/\S+)/i);
      if (urlMatch) {
        url = urlMatch[1].trim();
        break;
      }
    }
    if (url) results.set(index, { url, title });
  }
  return results;
};

export const mergeCitationsWithWebSearchFallback = (
  messageCitations: ChatMessage["citations"] | undefined,
  blocks: ContentBlock[],
  markdownSource: string,
): ChatMessage["citations"] => {
  const base = [...(messageCitations ?? [])];
  const citedIndexes = extractInlineCitationIndexes(markdownSource);
  if (citedIndexes.size === 0) {
    return base.some(citationUrl) ? base : [];
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
