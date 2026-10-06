/** Shared Markdown projection helpers. */
import { fromMarkdown } from "mdast-util-from-markdown";
import type { Root, RootContent } from "mdast";

export { fromMarkdown };

export const CITATION_MARKER_RE = /(?<![A-Za-z0-9_])\[\d{1,3}\](?=([\s，。！？；：、,.!?;:)）\[]|$))/g;

export const markdownNodeText = (node: Root | RootContent): string => {
  if (node.type === "text" || node.type === "inlineCode") return node.value;
  if (node.type === "image" || node.type === "imageReference") return node.alt ?? "";
  return "children" in node ? node.children.map(markdownNodeText).join("") : "";
};

export const extractMarkdownReferences = (content: string): {
  citationIndexes: Set<number>;
  links: Array<{ label: string; url: string }>;
} => {
  const citationIndexes = new Set<number>();
  const links: Array<{ label: string; url: string }> = [];
  if (!/\[\d{1,3}\]|https?:/i.test(content)) return { citationIndexes, links };
  const pending: Array<Root | RootContent> = [fromMarkdown(content)];
  const definitions = new Map<string, string>();
  const candidates: Array<{ label: string; url?: string; identifier?: string }> = [];
  while (pending.length) {
    const node = pending.pop()!;
    if (node.type === "definition" && !definitions.has(node.identifier)) definitions.set(node.identifier, node.url);
    if (node.type === "link" && /^https?:\/\//i.test(node.url)) {
      candidates.push({ label: markdownNodeText(node), url: node.url });
    }
    if (node.type === "linkReference") candidates.push({ label: markdownNodeText(node), identifier: node.identifier });
    if (node.type === "text") {
      for (const match of node.value.matchAll(CITATION_MARKER_RE)) {
        const index = Number(match[0].slice(1, -1));
        if (index > 0) citationIndexes.add(index);
      }
    }
    if ("children" in node) {
      for (let index = node.children.length - 1; index >= 0; index -= 1) pending.push(node.children[index]);
    }
  }
  for (const candidate of candidates) {
    const url = candidate.url ?? definitions.get(candidate.identifier!);
    if (url && /^https?:\/\//i.test(url)) links.push({ label: candidate.label, url });
  }
  return { citationIndexes, links };
};

export const extractInlineCitationIndexes = (content: string): Set<number> =>
  extractMarkdownReferences(content).citationIndexes;

export const markdownHeadingSlug = (value: string): string => {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/[\s_]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "section";
};

/**
 * Decode only a Markdown fragment. Malformed percent escapes are content, not
 * a rendering failure, so preserve the source fragment when URI decoding is
 * invalid.
 */
export const decodeMarkdownFragment = (value: string): string => {
  try {
    return decodeURIComponent(value);
  } catch (error) {
    if (error instanceof URIError) return value;
    throw error;
  }
};

export type MarkdownHeadingIdAssigner = ((base: string, line?: number) => string) & {
  /** Reset the ordinal table before rendering a new Markdown tree. */
  reset: () => void;
};

/**
 * Assign scoped, distinct heading IDs.
 *
 * Heading components are invoked in document order. Resetting the ordinal
 * table at the start of each tree render keeps the first occurrence linked by
 * the unsuffixed fragment while making every duplicate unique, even when a
 * parser omits source positions or a streamed prefix changes line numbers.
 */
export const createMarkdownHeadingIdAssigner = (scopeId: string): MarkdownHeadingIdAssigner => {
  const assignments = new Map<string, number>();

  const assigner = ((rawBase: string, _line?: number) => {
    const base = markdownHeadingSlug(rawBase);
    let ordinal = (assignments.get(base) ?? 0) + 1;
    let candidate = `${base}${ordinal > 1 ? `-${ordinal}` : ""}`;
    while (assignments.has(candidate)) {
      ordinal += 1;
      candidate = `${base}-${ordinal}`;
    }
    assignments.set(base, ordinal);
    if (candidate !== base) assignments.set(candidate, 1);
    return `${scopeId}-${candidate}`;
  }) as MarkdownHeadingIdAssigner;

  assigner.reset = () => {
    assignments.clear();
  };

  return assigner;
};
