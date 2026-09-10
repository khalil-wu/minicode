type CitationMarkdownNode = {
  type: string;
  value?: string;
  children?: CitationMarkdownNode[];
};

const citationMarker = /(?<![A-Za-z0-9_])\[\d{1,3}\](?=([\s，。！？；：、,.!?;:)）\[]|$))/g;

/** Citation presentation only touches prose nodes; model source remains intact. */
export const removeCitationMarkers = () => (tree: CitationMarkdownNode): void => {
  const visit = (node: CitationMarkdownNode): void => {
    if (node.type === "code" || node.type === "inlineCode") return;
    if (node.type === "text" && node.value) node.value = node.value.replace(citationMarker, "");
    node.children?.forEach(visit);
  };
  visit(tree);
};
