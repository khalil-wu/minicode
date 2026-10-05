import type { ChatMessage } from "../stores/types";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { extractMarkdownReferences } from "../lib/markdown";

export interface ConversationSource {
  id: string;
  label: string;
  url?: string;
  detail: string;
  messageId: string;
}

export const markdownSourceLabelKey = (content: string): string => {
  const references = extractMarkdownReferences(content);
  return `${JSON.stringify(references.links)}\u0000${[...references.citationIndexes].join(",")}`;
};

/** Both source summaries read this collection; only the view limits rows. */
export function collectConversationSources(messages: ChatMessage[]): ConversationSource[] {
  const sources = new Map<string, ConversationSource>();
  const articleTitles = new Map<string, string>();
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    const references = extractMarkdownReferences(message.content);
    const markdownTitles = new Map<string, string>();
    for (const link of references.links) {
      markdownTitles.set(link.url, link.label);
    }
    const upsert = (url: string, title?: string, label?: string, articleTitle = false, incomplete?: boolean) => {
      if (!URL.canParse(url)) return;
      const id = `source:${url}`;
      if (articleTitle && title) articleTitles.set(id, title);
      const parsed = new URL(url);
      const host = parsed.hostname.replace(/^www\./i, "");
      const pathLabel = parsed.pathname === "/" ? host : `${host}${parsed.pathname}`;
      const named = [title, markdownTitles.get(url), label].find((value) => value && value !== host && value !== url);
      const previous = sources.get(id);
      sources.delete(id);
      sources.set(id, {
        id, url, label: articleTitles.get(id) || named || previous?.label || pathLabel,
        detail: incomplete === undefined ? previous?.detail || host : `${host}${incomplete ? " · 内容不完整" : ""}`,
        messageId: markdownTitles.has(url) || named ? message.id : previous?.messageId || message.id,
      });
    };
    for (const record of getToolCallsFromMessage(message)) {
      const url = String(record.sourceUrl || record.args.url || record.args.source_url || "");
      if (record.extractionStatus !== "failed" && (record.status === "success" || record.status === "partial")
        && record.evidenceType === "fetched" && /^https?:\/\//i.test(url)) {
        upsert(url, undefined, undefined, false, record.extractionStatus === "partial" || record.status === "partial");
      }
    }
    for (const [url, label] of markdownTitles) upsert(url, label);
    const citedIndexes = references.citationIndexes;
    for (const [index, citation] of (message.citations ?? []).entries()) {
      const url = String(citation.url || citation.source || "");
      if (!citation.providerNative && !citedIndexes.has(index + 1) && !markdownTitles.has(url)) continue;
      if (/^https?:\/\//i.test(url)) upsert(url, citation.title, citation.label, true);
      else if (url) sources.set(`source:${url}`, { id: `source:${url}`, label: citation.title || citation.label || url, detail: citation.label || url, messageId: message.id });
    }
  }
  return [...sources.values()].reverse();
}
