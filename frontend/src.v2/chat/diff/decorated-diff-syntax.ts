import { diffChars } from "diff";
import type { DiffWordSpan } from "./diff-word-spans";

/** Monaco colorize renders spaces as NBSP and expands tabs. Match its emitted
 * text back to source UTF-16 offsets before decorating the existing token tree. */
function renderedOffsets(text: string, rendered: string): number[] {
  if (text === rendered) return Array.from({ length: text.length + 1 }, (_, index) => index);
  const offsets = [0];
  let sourceOffset = 0, renderedOffset = 0;
  for (const change of diffChars(text.replace(/\u00a0/g, " "), rendered.replace(/\u00a0/g, " "))) {
    if (change.added) {
      renderedOffset += change.value.length;
      offsets[sourceOffset] = renderedOffset;
    } else if (change.removed) {
      for (let index = 0; index < change.value.length; index++) offsets[++sourceOffset] = renderedOffset;
    } else {
      for (let index = 0; index < change.value.length; index++) offsets[++sourceOffset] = ++renderedOffset;
    }
  }
  return offsets;
}

/** html is the already-sanitized useColorizedLines result. Decorations only
 * split text nodes; token classes/styles, decoded entities and code stay intact. */
export function decorateDiffLineHtml(text: string, html: string | undefined, spans: readonly DiffWordSpan[]): string {
  const template = document.createElement("template");
  if (html) template.innerHTML = html;
  else template.content.append(document.createTextNode(text));
  if (spans.length === 0) return template.innerHTML;

  const offsets = renderedOffsets(text, template.content.textContent ?? "");
  const ranges = spans.map(span => ({ start: offsets[span.start], end: offsets[span.end] }));
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text);
  let lineOffset = 0;
  for (const node of nodes) {
    const value = node.data, nodeEnd = lineOffset + value.length;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const range of ranges) {
      const start = Math.max(range.start, lineOffset), end = Math.min(range.end, nodeEnd);
      if (start >= end) continue;
      fragment.append(document.createTextNode(value.slice(cursor, start - lineOffset)));
      const changed = document.createElement("span");
      changed.dataset.diffSpan = "";
      changed.textContent = value.slice(start - lineOffset, end - lineOffset);
      fragment.append(changed);
      cursor = end - lineOffset;
    }
    fragment.append(document.createTextNode(value.slice(cursor)));
    node.replaceWith(fragment);
    lineOffset = nodeEnd;
  }
  return template.innerHTML;
}
