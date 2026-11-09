import { Fragment, Schema, type Mark, type Node } from "prosemirror-model";
import { defaultMarkdownParser, defaultMarkdownSerializer, MarkdownParser, MarkdownSerializer, schema as markdownSchema, type MarkdownSerializerState } from "prosemirror-markdown";
import { tableNodes } from "prosemirror-tables";
import { composerReferenceFromLink, composerReferenceKey, composerReferenceLabel, composerReferenceMarkdown, type InlineComposerReference } from "./composer-references";
import type { ComposerSelection } from "./inputSelection";

export interface RichComposerDocument {
  markdown: string;
  document: ReturnType<Node["toJSON"]>;
  plainTextMode?: boolean;
  selection?: ComposerSelection;
}

// Remote pasted images remain document data. They must not bypass the existing
// attachment and remote-image permission consumers by loading inside the editor.
export const richComposerSchema = new Schema({
  nodes: markdownSchema.spec.nodes.update("image", {
    ...markdownSchema.nodes.image.spec,
    toDOM: node => ["span", { "data-rich-image": node.attrs.src }, node.attrs.alt || node.attrs.src],
  }).update("list_item", {
    ...markdownSchema.nodes.list_item.spec,
    attrs: { checked: { default: null } },
    parseDOM: [{ tag: "li", getAttrs: dom => ({ checked: (dom as HTMLElement).hasAttribute("data-checked")
      ? (dom as HTMLElement).getAttribute("data-checked") === "true" : null }) }],
    toDOM: node => node.attrs.checked === null ? ["li", 0] : ["li", { "data-checked": String(node.attrs.checked) },
      ["input", { type: "checkbox", checked: node.attrs.checked ? "checked" : null, contenteditable: "false", "aria-label": node.textContent }],
      ["div", 0]],
  }).append(tableNodes({ tableGroup: "block", cellContent: "block+", cellAttributes: {} })).addToEnd("context_mention", {
    inline: true, group: "inline", atom: true, selectable: false,
    attrs: { reference: {} },
    leafText: node => composerReferenceMarkdown(node.attrs.reference),
    parseDOM: [{ tag: "span[data-composer-reference]", getAttrs: dom => ({ reference: JSON.parse((dom as HTMLElement).getAttribute("data-composer-reference")!) }) }],
    toDOM: node => ["span", { "data-composer-reference": JSON.stringify(node.attrs.reference), contenteditable: "false" }, composerReferenceLabel(node.attrs.reference)],
  }),
  marks: markdownSchema.spec.marks.update("link", {
    ...markdownSchema.marks.link.spec,
    attrs: { ...markdownSchema.marks.link.spec.attrs, automatic: { default: false } },
  }).addToEnd("strike", {
    parseDOM: [{ tag: "s" }, { tag: "del" }],
    toDOM: () => ["s", 0],
  }),
});

const tokenizer = defaultMarkdownParser.tokenizer.enable(["strikethrough", "linkify", "table"]).set({ linkify: true });
const normalizeLink = tokenizer.normalizeLink, validateLink = tokenizer.validateLink;
tokenizer.normalizeLink = link => /^(?:plugin:|codex(?:-dev)?:|minicode:|file:|[a-z]:[/\\])/i.test(link) || !/^[a-z][a-z\d+.-]*:/i.test(link)
  ? link : normalizeLink(link);
tokenizer.validateLink = link => /^file:/i.test(link) || validateLink(link);
tokenizer.core.ruler.after("inline", "composer_tasks_and_cells", state => {
  const items: Array<{ token: typeof state.tokens[number]; first: boolean }> = [];
  const tokens: typeof state.tokens = [];
  let inCell = false;
  for (const token of state.tokens) {
    if (token.type === "td_open" || token.type === "th_open") inCell = true;
    if (token.type === "td_close" || token.type === "th_close") inCell = false;
    if (inCell && token.type === "inline") {
      // Only table cells use this transport. Decode line breaks before the
      // escaped literal characters, so a literal `<br>` stays literal text.
      const markdown = token.content.replace(/<br\s*\/?\s*>/gi, "\n")
        .replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
      const content = tokenizer.parse(markdown, state.env);
      // Markdown soft breaks normally become spaces. A cell transport break
      // represents a visible <br> in the cell.
      for (const block of content) for (const child of block.children ?? []) {
        if (child.type === "softbreak") child.type = "hardbreak";
      }
      tokens.push(...(content.length ? content : [new state.Token("paragraph_open", "p", 1), new state.Token("paragraph_close", "p", -1)]));
      continue;
    }
    if (token.type === "list_item_open") items.push({ token, first: true });
    if (token.type === "list_item_close") items.pop();
    if (token.type === "inline" && items.at(-1)?.first) {
      const item = items.at(-1)!; item.first = false;
      const match = /^\[([ xX])\] /.exec(token.content);
      if (match && token.children?.[0]?.type === "text") {
        item.token.attrSet("data-checked", String(match[1] !== " "));
        token.content = token.content.slice(4);
        token.children[0].content = token.children[0].content.slice(4);
      }
    }
    tokens.push(token);
  }
  state.tokens = tokens;
});
export const richComposerParser = new MarkdownParser(richComposerSchema, tokenizer, {
  ...defaultMarkdownParser.tokens, s: { mark: "strike" },
  link: { mark: "link", getAttrs: token => ({ href: token.attrGet("href"), title: token.attrGet("title"),
    automatic: token.markup === "autolink" || token.info === "auto" }) },
  list_item: { block: "list_item", getAttrs: token => ({ checked: token.attrGet("data-checked") === null ? null : token.attrGet("data-checked") === "true" }) },
  table: { block: "table" }, tr: { block: "table_row" }, td: { block: "table_cell" }, th: { block: "table_header" },
  thead: { ignore: true }, tbody: { ignore: true },
});
type LinkDelimiter = (state: MarkdownSerializerState, mark: Mark, parent: Node, index: number) => string;
const sourceLinkClose = defaultMarkdownSerializer.marks.link.close as LinkDelimiter;

interface MarkdownProjection {
  markdown: string;
  offsets: number[];
  carets: number[];
  aliases: Array<{ position: number; offset: number }>;
}
type SerializerOutput = MarkdownSerializerState & { out: string; atBlockStart: boolean };
type TextSpan = { from: number; start: number; end: number };
const richProjections = new WeakMap<Node, MarkdownProjection>();
const plainProjections = new WeakMap<Node, MarkdownProjection>();

const childOffset = (parent: Node, index: number) => {
  let offset = 0;
  for (let child = 0; child < index; child++) offset += parent.child(child).nodeSize;
  return offset;
};

function finishProjection(document: Node, markdown: string, offsets: number[], aliases: MarkdownProjection["aliases"] = []): MarkdownProjection {
  offsets[0] = 0;
  offsets[document.content.size] = markdown.length;
  const carets: number[] = [];
  document.descendants((node, position) => {
    if (node.isTextblock) for (let offset = 0; offset <= node.content.size; offset++) carets.push(position + 1 + offset);
  });
  // Container boundaries have no visible character. Keep them at the previous
  // emitted position rather than serializing an incomplete table/list prefix.
  let previous = 0;
  for (let position = 0; position <= document.content.size; position++) {
    if (offsets[position] === undefined) offsets[position] = previous;
    else previous = offsets[position];
  }
  return { markdown, offsets, carets, aliases: aliases.filter(({ position }) => document.resolve(position).parent.inlineContent) };
}

function plainProjection(document: Node): MarkdownProjection {
  let markdown = "", first = true;
  const offsets: number[] = [];
  const append = (text: string, from: number) => {
    for (let index = 0; index <= text.length; index++) offsets[from + index] = markdown.length + index;
    markdown += text;
  };
  document.descendants((node, position) => {
    if (!node.isTextblock) return;
    if (!first) markdown += "\n";
    first = false;
    offsets[position + 1] = markdown.length;
    node.forEach((child, offset) => {
      const from = position + 1 + offset;
      if (child.isText) append(child.text!, from);
      else {
        offsets[from] = markdown.length;
        markdown += child.type.name === "hard_break" ? "\n"
          : child.type.spec.leafText?.(child) ?? child.attrs.alt ?? child.attrs.src ?? "";
        offsets[from + child.nodeSize] = markdown.length;
      }
    });
    return false;
  });
  return finishProjection(document, markdown, offsets);
}

function encodeCell(markdown: string) {
  let text = "";
  const offsets: number[] = [];
  for (let index = 0; index < markdown.length; index++) {
    offsets[index] = text.length;
    const character = markdown[index];
    text += character === "\n" ? "<br>" : character === "|" ? "\\|"
      : character === "&" ? "&amp;" : character === "<" ? "&lt;" : character === ">" ? "&gt;" : character;
  }
  offsets[markdown.length] = text.length;
  return { text, offsets };
}

function richProjection(document: Node, preserveParagraphSpacing = false): MarkdownProjection {
  const offsets: number[] = [];
  const aliases: MarkdownProjection["aliases"] = [];
  const writtenCharacters = new Set<number>();
  const frames: Array<{ node: Node; from: number }> = [{ node: document, from: -1 }];
  let target: TextSpan | undefined, codeSpan: TextSpan | undefined, closedLink: number | undefined;
  let inline: { parent: Node; from: number; whitespace: Array<{ from: number; to: number }> } | undefined;
  let instrumented = false;
  const record = (position: number, offset: number, prefer = false) => {
    if (offsets[position] === undefined) offsets[position] = offset;
    else if (offsets[position] !== offset) {
      aliases.push({ position, offset: prefer ? offsets[position] : offset });
      if (prefer) offsets[position] = offset;
    }
  };
  const positionOf = (parent: Node, index: number) => frames.at(-1)!.from + 1 + childOffset(parent, index);
  const instrument = (state: MarkdownSerializerState) => {
    if (instrumented) return;
    instrumented = true;
    const output = state as SerializerOutput;
    const writeText = state.text, renderInline = state.renderInline;
    state.text = (text, escape = true) => {
      const span = target ?? codeSpan, linkEnd = closedLink;
      codeSpan = undefined; closedLink = undefined;
      const lines = text.split("\n"), starts: Array<{ offset: number; blockStart: boolean }> = [];
      const write = state.write;
      state.write = content => {
        write.call(state, content);
        if (content !== undefined) return;
        let offset = output.out.length;
        // The upstream serializer inserts a slash before a preceding literal
        // exclamation when a text link opens. Shift already emitted positions.
        if (!escape && lines[starts.length][0] === "[" && /(^|[^\\])\!$/.test(output.out)) {
          for (let position = 0; position < offsets.length; position++) if (offsets[position] > offset - 1) offsets[position]++;
          aliases.forEach(alias => { if (alias.offset > offset - 1) alias.offset++; });
          offset++;
        }
        starts.push({ offset, blockStart: output.atBlockStart });
      };
      writeText.call(state, text, escape);
      state.write = write;
      const emitted: number[] = [];
      let input = 0;
      lines.forEach((line, index) => {
        const encoded = escape ? state.esc(line, starts[index].blockStart) : line;
        let written = 0;
        for (let character = 0; character < line.length; character++) {
          emitted[input + character] = starts[index].offset + written;
          written += escape && encoded[written] === "\\" && encoded[written + 1] === line[character] ? 2 : 1;
        }
        emitted[input + line.length] = starts[index].offset + written;
        input += line.length + 1;
      });
      if (span) {
        for (let index = span.start; index <= span.end; index++) record(span.from + index - span.start, emitted[index]);
        for (let index = span.start; index < span.end; index++) writtenCharacters.add(span.from + index - span.start);
      } else if (inline && /^\s+$/.test(text)) {
        for (let index = 0; index < text.length; index++) inline.whitespace.push({ from: emitted[index], to: emitted[index + 1] });
      }
      if (linkEnd !== undefined) record(linkEnd, output.out.length, true);
    };
    state.renderInline = (parent, fromBlockStart) => {
      const previous = inline;
      const context = { parent, from: frames.at(-1)!.from + 1, whitespace: [] as Array<{ from: number; to: number }> };
      inline = context;
      if (!parent.content.size) record(context.from, output.out.length);
      renderInline.call(state, parent, fromBlockStart);
      // Emphasis moves enclosing whitespace outside its delimiters. Those
      // writes bypass the text-node callback, but still have real output spans.
      let emitted = 0;
      parent.forEach((node, offset) => {
        if (!node.isText) return;
        for (let index = 0; index < node.text!.length; index++) {
          const from = context.from + offset + index;
          if (!/\s/.test(node.text![index]) || writtenCharacters.has(from)) continue;
          const whitespace = context.whitespace[emitted++];
          record(from, whitespace.from); record(from + 1, whitespace.to);
        }
      });
      inline = previous;
    };
  };
  const nodeSerializers: MarkdownSerializer["nodes"] = {
    ...defaultMarkdownSerializer.nodes,
    context_mention: (state, node) => state.write(composerReferenceMarkdown(node.attrs.reference)),
    text: (state, node) => state.text(node.text!, !node.marks.some(mark => mark.type.name === "link" && mark.attrs.automatic)),
    list_item: (state, node) => {
      if (node.attrs.checked !== null) state.write(node.attrs.checked ? "[x] " : "[ ] ");
      state.renderContent(node);
    },
    table: (state, node) => {
      const output = state as SerializerOutput, tableStart = frames.at(-1)!.from;
      node.forEach((row, rowOffset, index) => {
        state.write("| ");
        row.forEach((cell, cellOffset, cellIndex) => {
          if (cellIndex) state.write(" | ");
          const projection = richProjection(richComposerSchema.node("doc", null, cell.content), true);
          const encoded = encodeCell(projection.markdown), start = output.out.length;
          const cellStart = tableStart + 3 + rowOffset + cellOffset;
          projection.offsets.forEach((offset, position) => record(cellStart + position, start + encoded.offsets[offset]));
          projection.aliases.forEach(({ position, offset }) => aliases.push({ position: cellStart + position, offset: start + encoded.offsets[offset] }));
          state.write(encoded.text);
        });
        state.write(" |\n");
        if (index === 0) state.write("| " + Array.from({ length: row.childCount }, () => "---").join(" | ") + " |\n");
      });
      state.closeBlock(node);
    },
  };
  const nodes = Object.fromEntries(Object.entries(nodeSerializers).map(([name, serialize]) => [name,
    (state: MarkdownSerializerState, node: Node, parent: Node, index: number) => {
      const from = positionOf(parent, index), previousTarget = target;
      instrument(state);
      frames.push({ node, from });
      if (node.isText) target = { from: from + parent.child(index).text!.indexOf(node.text!), start: 0, end: node.text!.length };
      else if (name === "code_block") target = { from: from + 1, start: 0, end: node.textContent.length };
      if (node.isInline && node.isLeaf && !node.isText) { state.write(); record(from, (state as SerializerOutput).out.length); }
      serialize(state, node, parent, index);
      if (node.isInline && node.isLeaf && !node.isText) record(from + node.nodeSize, (state as SerializerOutput).out.length);
      target = previousTarget;
      frames.pop();
    },
  ]));
  const code = defaultMarkdownSerializer.marks.code;
  const serializer = new MarkdownSerializer(nodes, {
    ...defaultMarkdownSerializer.marks,
    code: { ...code, open: (state, mark, parent, index) => {
      const delimiter = (code.open as LinkDelimiter)(state, mark, parent, index);
      codeSpan = { from: positionOf(parent, index), start: delimiter.length, end: delimiter.length + parent.child(index).text!.length };
      return delimiter;
    } },
    link: { ...defaultMarkdownSerializer.marks.link,
      open: (_state, mark) => mark.attrs.automatic ? "" : "[",
      close: (state, mark, parent, index) => {
        closedLink = positionOf(parent, index);
        return mark.attrs.automatic ? "" : sourceLinkClose(state, mark, parent, index);
      },
    },
    strike: { open: "~~", close: "~~", mixable: true, expelEnclosingWhitespace: true },
  });
  if (!preserveParagraphSpacing && document.content.content.every(node => node.type.name === "paragraph")) {
    let markdown = "";
    document.forEach((node, position, index) => {
      if (index) markdown += "\n";
      const projection = richProjection(richComposerSchema.node("doc", null, node), true);
      projection.offsets.forEach((offset, child) => { offsets[position + child] = markdown.length + offset; });
      projection.aliases.forEach(alias => aliases.push({ position: position + alias.position, offset: markdown.length + alias.offset }));
      markdown += projection.markdown;
    });
    return finishProjection(document, markdown, offsets, aliases);
  }
  const markdown = serializer.serialize(document);
  return finishProjection(document, markdown, offsets, aliases);
}

function projectionOf(document: Node, plainTextMode: boolean): MarkdownProjection {
  const cache = plainTextMode ? plainProjections : richProjections;
  let projection = cache.get(document);
  if (!projection) {
    projection = plainTextMode ? plainProjection(document) : richProjection(document);
    cache.set(document, projection);
  }
  return projection;
}

export function literalComposerDocument(value: string): Node {
  return richComposerSchema.node("doc", null, value.split("\n").map(line =>
    richComposerSchema.node("paragraph", null, literalInlineReferences(line))));
}

function literalInlineReferences(value: string): Node[] {
  const nodes: Node[] = [];
  let end = 0;
  for (const match of value.matchAll(/\[((?:\\.|[^\]\\\n])+)\]\((<[^>\n]*>|(?:\\.|[^)\n])*)\)/g)) {
    const label = match[1].replace(/\\([\\[\]])/g, "$1");
    const href = match[2].replace(/^<|>$/g, "").replace(/\\([\\()])/g, "$1");
    const reference = composerReferenceFromLink(label, href);
    if (!reference) continue;
    if (match.index! > end) nodes.push(richComposerSchema.text(value.slice(end, match.index)));
    nodes.push(richComposerSchema.nodes.context_mention.create({ reference }));
    end = match.index! + match[0].length;
  }
  if (end < value.length) nodes.push(richComposerSchema.text(value.slice(end)));
  return nodes;
}

export function convertComposerReferenceLinks(node: Node): Node {
  if (node.isText) {
    const link = node.marks.find(mark => mark.type.name === "link");
    const reference = link && composerReferenceFromLink(node.text!, link.attrs.href);
    return reference ? richComposerSchema.nodes.context_mention.create({ reference }) : node;
  }
  return node.isLeaf ? node : node.copy(Fragment.fromArray(node.content.content.map(convertComposerReferenceLinks)));
}

export function inlineComposerReferences(document: RichComposerDocument): InlineComposerReference[] {
  const references = new Map<string, InlineComposerReference>();
  richComposerSchema.nodeFromJSON(document.document).descendants(node => {
    if (node.type.name === "context_mention") references.set(composerReferenceKey(node.attrs.reference), node.attrs.reference);
  });
  return [...references.values()];
}

export function serializeComposerDocument(document: Node, plainTextMode = false): string {
  return projectionOf(document, plainTextMode).markdown;
}

export function restoreComposerDocument(value: string, saved?: RichComposerDocument, plainTextMode = false): Node {
  if (saved?.markdown === value && Boolean(saved.plainTextMode) === plainTextMode) return richComposerSchema.nodeFromJSON(saved.document);
  return plainTextMode ? literalComposerDocument(value) : parseComposerMarkdown(value);
}

export function parseComposerMarkdown(value: string): Node {
  const parsed = convertComposerReferenceLinks(richComposerParser.parse(value));
  let formatted = false;
  parsed.descendants(node => {
    if (node.type.name !== "paragraph" && !node.isText || node.marks.length > 0) formatted = true;
  });
  return formatted ? parsed : literalComposerDocument(value);
}

export function markdownOffset(document: Node, position: number, plainTextMode = false): number {
  return projectionOf(document, plainTextMode).offsets[position];
}

export function documentPosition(document: Node, offset: number, plainTextMode = false): number {
  const { offsets, carets, aliases } = projectionOf(document, plainTextMode);
  let left = 0, right = carets.length - 1;
  while (left < right) {
    const middle = Math.floor((left + right) / 2);
    if (offsets[carets[middle]] < offset) left = middle + 1;
    else right = middle;
  }
  let position = carets[left] ?? 0, mapped = offsets[position] >= offset ? offsets[position] : Infinity;
  // A caret at a mark boundary has two serialized edges: outside its syntax
  // and inside the next text span. Either edge must resolve to that same caret.
  for (const alias of aliases) if (alias.offset >= offset && (alias.offset < mapped || alias.offset === mapped && alias.position < position)) {
    position = alias.position; mapped = alias.offset;
  }
  return position;
}

export function hasRichComposerContent(document?: RichComposerDocument, value = document?.markdown ?? ""): boolean {
  if (!document || document.markdown !== value) return value.trim().length > 0;
  let content = false;
  richComposerSchema.nodeFromJSON(document.document).descendants(node => {
    if (node.isText ? node.text!.trim().length > 0 : node.isInline && node.isAtom && node.type.name !== "hard_break") content = true;
    return !content;
  });
  return content;
}

export const richDocumentState = (document: Node, plainTextMode = false): RichComposerDocument => ({
  markdown: serializeComposerDocument(document, plainTextMode), document: document.toJSON(), ...(plainTextMode ? { plainTextMode: true } : {}),
});
