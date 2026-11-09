import { CITATION_MARKER_RE } from "../../lib/markdown";

type CitationMarkdownNode = {
  type: string;
  value?: string;
  children?: CitationMarkdownNode[];
};

/** Citation presentation only touches prose nodes; model source remains intact. */
export const removeCitationMarkers = (boundIndexes: ReadonlySet<number>) => (tree: CitationMarkdownNode): void => {
  const visit = (node: CitationMarkdownNode): void => {
    if (node.type === "code" || node.type === "inlineCode") return;
    if (node.type === "text" && node.value) node.value = node.value.replace(CITATION_MARKER_RE, (marker) =>
      boundIndexes.has(Number(marker.slice(1, -1))) ? "" : marker);
    node.children?.forEach(visit);
  };
  visit(tree);
};
