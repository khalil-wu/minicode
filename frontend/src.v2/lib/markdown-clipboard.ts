/** Codex TBr/OBr: copy semantic Markdown DOM, excluding presentation controls. */
export type MarkdownClipboard = { "text/plain": string; "text/html": string };

const mathSource = "data-math-source";
const mathDisplay = "data-math-display";
const promptLabel = "data-prompt-link-label";
const promptHref = "data-prompt-link-href";
const promptSelector = `[${promptLabel}][${promptHref}]`;
const blockTags = new Set(["BLOCKQUOTE", "DIV", "H1", "H2", "H3", "H4", "H5", "H6", "LI", "P", "PRE"]);
const copyAttributes: Record<string, string[]> = {
  A: ["href", "title"], BLOCKQUOTE: ["cite"],
  IMG: ["src", "srcset", "sizes", "alt", "title", "width", "height"],
  LI: ["value"], OL: ["start"], Q: ["cite"],
  TD: ["colspan", "rowspan"], TH: ["colspan", "rowspan"], TIME: ["datetime"],
};

export function readMarkdownClipboard(root: HTMLElement): MarkdownClipboard | null {
  const range = root.ownerDocument.createRange();
  range.selectNodeContents(root);
  return copyRange(range);
}

export function writeMarkdownSelection(root: HTMLElement, data: DataTransfer | null): boolean {
  const selection = root.ownerDocument.getSelection();
  if (!data || !selection || selection.rangeCount === 0 || selection.isCollapsed) return false;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return false;
  const editable = closest(range.startContainer, '[contenteditable="true"], [contenteditable="plaintext-only"]');
  if (editable && editable === closest(range.endContainer, '[contenteditable="true"], [contenteditable="plaintext-only"]')) return false;
  const content = copyRange(range);
  if (!content) return false;
  data.setData("text/plain", content["text/plain"]);
  data.setData("text/html", content["text/html"]);
  return true;
}

function copyRange(original: Range): MarkdownClipboard | null {
  const range = original.cloneRange();
  for (const selector of [`[${mathSource}]`, ".katex-display", ".katex", promptSelector]) expandBoundary(range, selector);
  const start = closest(range.startContainer, "[data-markdown-copy]");
  const end = closest(range.endContainer, "[data-markdown-copy]");
  const common = start && start === end ? start : null;
  const kind = common?.getAttribute("data-markdown-copy");
  if (kind === "exclude") return null;
  if (kind === "code-block") {
    const text = range.toString();
    const full = range.startContainer === range.endContainer && range.startOffset === 0
      && range.endOffset === range.startContainer.childNodes.length
      && (range.startContainer === common || closest(range.startContainer, "svg") === range.startContainer);
    const value = (full || text.length === 0 ? common?.getAttribute("data-markdown-copy-text") : null) ?? text;
    return { "text/plain": value, "text/html": codeBlock(original.startContainer.ownerDocument!, value).outerHTML };
  }
  if (kind === "blank-lines") {
    const value = range.toString();
    return { "text/plain": value, "text/html": "<p></p>".repeat(value.length) };
  }
  const clone = range.cloneContents();
  for (const math of clone.querySelectorAll(".katex")) {
    const annotation = math.querySelector('annotation[encoding="application/x-tex"]');
    if (!annotation) continue;
    const display = math.closest(".katex-display");
    (display ?? math).replaceWith(mathNode(clone.ownerDocument, annotation.textContent ?? "", Boolean(display)));
  }
  const semantic = clone.ownerDocument.createDocumentFragment();
  for (const node of Array.from(clone.childNodes)) appendSemantic(semantic, node, clone.ownerDocument);
  if (kind === "inline-code") {
    const code = clone.ownerDocument.createElement("code");
    code.append(...Array.from(semantic.childNodes));
    semantic.append(code);
  }
  let plain = Array.from(semantic.childNodes).map(plainNode).join("").trim();
  const firstBlank = closest(range.startContainer, '[data-markdown-copy="blank-lines"]');
  if (firstBlank) {
    const prefix = range.cloneRange(); prefix.setEndAfter(firstBlank);
    plain = prefix.toString() + plain;
  }
  const lastBlank = closest(range.endContainer, '[data-markdown-copy="blank-lines"]');
  if (lastBlank) {
    const suffix = range.cloneRange(); suffix.setStartBefore(lastBlank);
    plain += "\n" + suffix.toString();
  }
  if (!plain.length) return null;
  const html = clone.ownerDocument.createElement("div");
  html.append(semantic);
  for (const math of html.querySelectorAll(`[${mathSource}]`)) {
    math.textContent = latexText(math.getAttribute(mathSource) ?? "", math.getAttribute(mathDisplay) === "true");
  }
  return { "text/plain": plain, "text/html": html.innerHTML };
}

function appendSemantic(parent: Node, node: Node, doc: Document): void {
  if (node.nodeType === Node.TEXT_NODE) { parent.appendChild(doc.createTextNode(node.textContent ?? "")); return; }
  if (!(node instanceof Element) || node.classList.contains("sr-only") || node.getAttribute("aria-hidden") === "true"
    || (node.tagName === "IMG" && node.getAttribute("alt") === "")) return;
  const kind = node.getAttribute("data-markdown-copy");
  if (kind === "exclude") return;
  if (kind === "contents") {
    for (const child of Array.from(node.childNodes)) appendSemantic(parent, child, doc);
    return;
  }
  if (kind === "blank-lines") {
    for (let i = 0; i < (node.textContent?.length ?? 0); i++) parent.appendChild(doc.createElement("p"));
    return;
  }
  const href = node.getAttribute(promptHref), label = node.getAttribute(promptLabel);
  if (href && label !== null) {
    const link = doc.createElement("span");
    link.textContent = `[${label.replaceAll("\\", "\\\\").replaceAll("](", "]\\(").replaceAll("]", "\\]")}](${href.replaceAll("\\", "\\\\").replaceAll(")", "\\)")})`;
    link.setAttribute(promptLabel, label); link.setAttribute(promptHref, href);
    parent.appendChild(link); return;
  }
  if (node.matches('[role="checkbox"]')) {
    const input = doc.createElement("input"); input.type = "checkbox"; input.disabled = true;
    if (node.getAttribute("aria-checked") === "true") input.setAttribute("checked", "");
    parent.appendChild(input); return;
  }
  if (node.matches("button, [data-file-reference]")) {
    if (!node.querySelector(promptSelector)) {
      parent.appendChild(doc.createTextNode(node.getAttribute("data-markdown-copy-text") ?? node.textContent ?? ""));
    } else for (const child of Array.from(node.childNodes)) appendSemantic(parent, child, doc);
    return;
  }
  if (kind === "code-block") { parent.appendChild(codeBlock(doc, codeText(node))); return; }
  if (kind === "rich-block") {
    const content = node.querySelector("[data-markdown-copy-content]");
    if (content) appendSemantic(parent, content, doc);
    return;
  }
  if (kind === "inline-code") {
    const code = doc.createElement("code"); code.dir = "ltr";
    for (const child of Array.from(node.childNodes)) appendSemantic(code, child, doc);
    parent.appendChild(code); return;
  }
  const source = node.getAttribute(mathSource);
  if (source !== null) {
    parent.appendChild(mathNode(doc, source, node.getAttribute(mathDisplay) === "true", node.textContent ?? "")); return;
  }
  if (node.namespaceURI !== "http://www.w3.org/1999/xhtml") {
    if (node.textContent) parent.appendChild(doc.createTextNode(node.textContent));
    return;
  }
  const element = doc.createElement(node.localName);
  for (const attr of copyAttributes[node.tagName] ?? []) {
    const value = node.getAttribute(attr); if (value !== null) element.setAttribute(attr, value);
  }
  for (const child of Array.from(node.childNodes)) appendSemantic(element, child, doc);
  parent.appendChild(element);
}

function codeText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (!(node instanceof Element)) return "";
  return node.getAttribute("data-markdown-copy-text")
    ?? (node.getAttribute("data-markdown-copy") === "exclude" ? "" : Array.from(node.childNodes).map(codeText).join(""));
}
function codeBlock(doc: Document, text: string): HTMLPreElement {
  const pre = doc.createElement("pre"), code = doc.createElement("code"); pre.dir = "ltr";
  code.textContent = text; pre.append(code); return pre;
}
function latexText(source: string, display: boolean): string { return display ? `\\[\n${source}\n\\]` : `\\(${source}\\)`; }
function mathNode(doc: Document, source: string, display: boolean, text = latexText(source, display)): HTMLSpanElement {
  const span = doc.createElement("span"); span.setAttribute(mathSource, source); span.setAttribute(mathDisplay, String(display)); span.textContent = text; return span;
}
function closest(node: Node, selector: string): Element | null { return (node instanceof Element ? node : node.parentElement)?.closest(selector) ?? null; }
function expandBoundary(range: Range, selector: string): void {
  const start = closest(range.startContainer, selector), end = closest(range.endContainer, selector);
  if (start) range.setStartBefore(start); if (end) range.setEndAfter(end);
}
function childText(node: Node): string { return Array.from(node.childNodes).map(plainNode).join(""); }
function rowText(row: Element): string { return Array.from(row.children).filter(cell => cell.tagName === "TH" || cell.tagName === "TD").map(cell => childText(cell).trim()).join("\t"); }
function plainNode(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (!(node instanceof Element)) return "";
  switch (node.tagName) {
    case "INPUT": return node.getAttribute("type") === "checkbox" ? `[${node.hasAttribute("checked") ? "x" : " "}] ` : "";
    case "TABLE": return Array.from(node.querySelectorAll("tr")).map(rowText).join("\n");
    case "TR": return rowText(node) + "\n";
    case "TD": case "TH": return childText(node).trim() + "\t";
    case "THEAD": case "TBODY": case "TFOOT": return Array.from(node.children).map(plainNode).join("");
    case "BR": return "\n";
    case "OL": case "UL": return listText(node, "");
    default: return childText(node) + (blockTags.has(node.tagName) || node.getAttribute(mathDisplay) === "true" ? "\n" : "");
  }
}
function listText(list: Element, indent: string): string {
  const ordered = list.tagName === "OL";
  let index = list instanceof HTMLOListElement ? list.start : 1;
  return Array.from(list.children).filter(child => child.tagName === "LI").map(item => {
    if (ordered && item instanceof HTMLLIElement && item.hasAttribute("value")) index = item.value;
    return listItemText(item, indent, ordered ? `${index++}. ` : "- ");
  }).join("");
}
function listItemText(item: Element, indent: string, marker: string): string {
  const nestedIndent = indent + " ".repeat(marker.length);
  let wrote = false, pending = "", output = "";
  const flush = () => {
    const lines = pending.trim().split("\n"); pending = "";
    if (lines.length === 1 && lines[0] === "") return;
    for (const line of lines) { output += `${wrote ? nestedIndent : indent + marker}${line}\n`; wrote = true; }
  };
  for (const child of Array.from(item.childNodes)) {
    if (child instanceof Element && (child.tagName === "OL" || child.tagName === "UL")) {
      flush(); if (!wrote) { output += indent + marker.trimEnd() + "\n"; wrote = true; }
      output += listText(child, nestedIndent);
    } else pending += plainNode(child);
  }
  flush(); return wrote ? output : indent + marker.trimEnd() + "\n";
}
