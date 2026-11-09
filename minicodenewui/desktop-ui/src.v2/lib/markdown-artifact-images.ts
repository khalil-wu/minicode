import { fromMarkdown } from "mdast-util-from-markdown";
import { artifactIdFromReference } from "./artifact-resource";

type MarkdownNode = {
  type: string;
  url?: string;
  identifier?: string;
  children?: MarkdownNode[];
};

/** Only images parsed by Markdown count; code samples, links and incomplete syntax do not. */
export const markdownArtifactImageIds = (markdown: string): Set<string> => {
  const tree = fromMarkdown(markdown);
  const definitions = new Map<string, string>();
  const ids = new Set<string>();
  const visit = (node: MarkdownNode, visitor: (node: MarkdownNode) => void) => {
    visitor(node);
    node.children?.forEach((child) => visit(child, visitor));
  };
  visit(tree, (node) => {
    if (node.type === "definition") definitions.set(node.identifier!, node.url!);
  });
  visit(tree, (node) => {
    const url = node.type === "image" ? node.url
      : node.type === "imageReference" ? definitions.get(node.identifier!) : undefined;
    const id = url ? artifactIdFromReference(url) : null;
    if (id) ids.add(id);
  });
  return ids;
};
