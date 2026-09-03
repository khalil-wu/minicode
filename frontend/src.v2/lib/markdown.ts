/** Shared Markdown projection helpers. */

export const extractInlineCitationIndexes = (content: string): Set<number> => {
  const indexes = new Set<number>();
  for (const match of content.matchAll(/\[(\d+)\]/g)) {
    const index = Number(match[1]);
    if (Number.isFinite(index) && index > 0) indexes.add(index);
  }
  return indexes;
};

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
    const ordinal = (assignments.get(base) ?? 0) + 1;
    assignments.set(base, ordinal);
    const candidate = `${base}${ordinal > 1 ? `-${ordinal}` : ""}`;
    return `${scopeId}-${candidate}`;
  }) as MarkdownHeadingIdAssigner;

  assigner.reset = () => {
    assignments.clear();
  };

  return assigner;
};
