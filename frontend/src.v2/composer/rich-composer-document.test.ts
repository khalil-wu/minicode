import { describe, expect, it } from "vitest";
import type { Node } from "prosemirror-model";
import { composerReferenceMarkdown, type InlineComposerReference } from "./composer-references";
import {
  documentPosition, hasRichComposerContent, literalComposerDocument, markdownOffset,
  parseComposerMarkdown, restoreComposerDocument, richComposerSchema as schema,
  richDocumentState, serializeComposerDocument,
  inlineComposerReferences,
} from "./rich-composer-document";

const references: InlineComposerReference[] = [
  { kind: "plugin", name: "电脑", configName: "computer-use@runtime", path: "plugin://computer-use@runtime", pluginPath: "C:/plugins/computer-use", iconVariant: "composer" },
  { kind: "skill", name: "playwright", displayName: "Playwright CLI Skill", path: "C:/skills/playwright/SKILL.md" },
  { kind: "url", name: "继续完成 MiniCode UI 复刻", path: "codex://threads/owned-reference" },
  { kind: "file", name: "文件(草稿).ts", path: "C:/项目/文件(草稿).ts" },
  { kind: "folder", name: "资料", path: "C:/项目/资料" },
];

it.each([false, true])("keeps five inline reference identities, atom boundaries and Unicode carets in plain=%s", plain => {
  const document = schema.node("doc", null, schema.node("paragraph", null, [
    ...references.flatMap(reference => [schema.nodes.context_mention.create({ reference }), schema.text(" ")]), schema.text("123🙂"),
  ]));
  const markdown = serializeComposerDocument(document, plain);
  expect(markdown).toBe(references.map(composerReferenceMarkdown).join(" ") + " 123🙂");
  const saved = richDocumentState(document, plain);
  expect(restoreComposerDocument(markdown, saved, plain).toJSON()).toEqual(document.toJSON());
  expect(inlineComposerReferences(saved)).toEqual(references);
  const parsed = plain ? literalComposerDocument(markdown) : parseComposerMarkdown(markdown);
  expect(inlineComposerReferences(richDocumentState(parsed, plain))).toMatchObject(references.map(({ kind, name, path }) => ({ kind, name, path: kind === "folder" ? `${path}/` : path })));
  expect(serializeComposerDocument(parsed, plain)).toBe(markdown);
  assertCaretRoundtrip(document, plain);
});

it("leaves mention-looking Markdown literal inside actual code", () => {
  const markdown = "```text\n" + references.map(composerReferenceMarkdown).join(" ") + "\n```";
  const parsed = parseComposerMarkdown(markdown);
  expect(parsed.firstChild?.type.name).toBe("code_block");
  expect(inlineComposerReferences(richDocumentState(parsed))).toEqual([]);
});

it("serializes Windows filesystem separators without changing the saved original target or a deep-link href", () => {
  const reference = { kind: "file" as const, name: "notes.md", path: "C:\\项目\\(初稿) notes.md" };
  const markdown = composerReferenceMarkdown(reference);
  expect(markdown).toBe("[notes.md](<C:/项目/(初稿) notes.md>)");
  expect(inlineComposerReferences(richDocumentState(parseComposerMarkdown(markdown)))).toMatchObject([{ path: "C:/项目/(初稿) notes.md" }]);
  expect(composerReferenceMarkdown({ kind: "url", name: "chat", path: "codex://threads/real?owner=a%2Fb#source" })).toBe("[chat](codex://threads/real?owner=a%2Fb#source)");
});

const text = (value: string, mark?: string) => schema.text(value, mark ? [schema.marks[mark].create()] : []);
const paragraph = (...children: Node[]) => schema.node("paragraph", null, children);
const document = (...children: Node[]) => schema.node("doc", null, children);
const cell = (...children: Node[]) => schema.node("table_cell", null, children);
const header = (...children: Node[]) => schema.node("table_header", null, children);
const row = (...children: Node[]) => schema.node("table_row", null, children);
const table = (...children: Node[]) => schema.node("table", null, children);
const textPosition = (document: Node, content: string) => {
  let position = -1;
  document.descendants((node, from) => {
    if (node.isText && node.text!.includes(content) && position === -1) position = from + node.text!.indexOf(content);
  });
  expect(position).toBeGreaterThanOrEqual(0);
  return position;
};
const assertCaretRoundtrip = (document: Node, plain = false) => {
  const carets: number[] = [];
  document.descendants((node, position) => {
    if (node.isTextblock) for (let index = 0; index <= node.content.size; index++) carets.push(position + 1 + index);
  });
  const offsets = carets.map(position => markdownOffset(document, position, plain));
  expect(offsets).toEqual([...offsets].sort((left, right) => left - right));
  carets.forEach((position, index) => {
    if (offsets.indexOf(offsets[index]) === index) expect(documentPosition(document, offsets[index], plain)).toBe(position);
  });
};

describe("composer document projection", () => {
  it("maps original inline-code backtick runs without changing their delimiters", () => {
    const doc = document(paragraph(text("a``b", "code")));
    const before = doc.toJSON(), markdown = serializeComposerDocument(doc), from = textPosition(doc, "a``b");
    expect(markdown).toBe("``` a``b ```");
    for (let index = 0; index <= 4; index++) expect(markdownOffset(doc, from + index)).toBe(4 + index);
    assertCaretRoundtrip(doc);
    expect(doc.toJSON()).toEqual(before);
  });

  it("uses the original fenced-code delimiter and language for every body caret", () => {
    const content = "x\n```\n中😀";
    const doc = document(schema.node("code_block", { params: "js" }, text(content)));
    const markdown = serializeComposerDocument(doc);
    expect(markdown).toBe("````js\n" + content + "\n````");
    for (let index = 0; index <= content.length; index++) expect(markdownOffset(doc, index + 1)).toBe(7 + index);
    assertCaretRoundtrip(doc);
  });

  it("maps marked Unicode and escaped literal text in UTF-16 offsets", () => {
    const marked = document(paragraph(text("中😀e", "strong")));
    expect(markdownOffset(marked, 4)).toBe(5);
    expect(documentPosition(marked, 5)).toBe(4);
    assertCaretRoundtrip(marked);
    const literal = literalComposerDocument("a[b] _x_ \\ 中😀");
    expect(serializeComposerDocument(literal)).toContain("a\\[b\\]");
    assertCaretRoundtrip(literal);
  });

  it("preserves real whitespace spans expelled from adjacent emphasis marks", () => {
    const doc = document(paragraph(text(" a ", "strong"), text(" b ", "em"), text(" c ")));
    expect(serializeComposerDocument(doc)).toBe(" **a**  *b*  c ");
    expect(markdownOffset(doc, 1)).toBe(0);
    expect(markdownOffset(doc, 10)).toBe(serializeComposerDocument(doc).length);
    assertCaretRoundtrip(doc);
  });

  it("accounts for the slash inserted before a literal exclamation followed by a link", () => {
    const link = schema.marks.link.create({ href: "https://example.com" });
    const doc = document(paragraph(text("!"), schema.text("label", [link]), text(" tail")));
    expect(serializeComposerDocument(doc)).toBe("\\![label](https://example.com) tail");
    expect(markdownOffset(doc, 1)).toBe(0);
    expect(markdownOffset(doc, 2)).toBe(2);
    expect(documentPosition(doc, 3)).toBe(2);
    expect(markdownOffset(doc, 7)).toBe(serializeComposerDocument(doc).indexOf(" tail"));
    assertCaretRoundtrip(doc);
  });

  it("maps both table columns and rows without serializing partial containers", () => {
    const doc = document(table(row(header(paragraph(text("a"))), header(paragraph(text("b")))),
      row(cell(paragraph(text("c"))), cell(paragraph(text("d"))))));
    const markdown = serializeComposerDocument(doc);
    for (const content of ["a", "b", "c", "d"]) {
      const position = textPosition(doc, content), offset = markdown.indexOf(content);
      expect(markdownOffset(doc, position)).toBe(offset);
      expect(documentPosition(doc, offset)).toBe(position);
    }
    const allOffsets = Array.from({ length: doc.content.size + 1 }, (_, position) => markdownOffset(doc, position));
    expect(allOffsets).toEqual([...allOffsets].sort((left, right) => left - right));
    assertCaretRoundtrip(doc);
  });

  it("maps nested task/list prefixes and linked table text using actual output", () => {
    const checked = schema.node("list_item", { checked: true }, paragraph(text("完成中😀")));
    const doc = document(schema.node("bullet_list", null, checked),
      table(row(header(paragraph(text("head")))), row(cell(paragraph(text("a``b", "code"))))));
    const markdown = serializeComposerDocument(doc), position = textPosition(doc, "a``b");
    expect(markdown).toContain("[x] 完成中😀");
    expect(markdownOffset(doc, position + 2)).toBe(markdown.indexOf("a``b") + 2);
    assertCaretRoundtrip(doc);
  });

  it("preserves paragraph-only literal line spacing and empty-format caret positions", () => {
    const doc = literalComposerDocument("one\n\nlast\n");
    expect(serializeComposerDocument(doc)).toBe("one\n\nlast\n");
    assertCaretRoundtrip(doc);
    for (const empty of [document(paragraph()), document(schema.node("code_block", { params: "ts" })),
      document(table(row(header(paragraph())), row(cell(paragraph()))))]) {
      expect(hasRichComposerContent(richDocumentState(empty))).toBe(false);
      assertCaretRoundtrip(empty);
    }
  });
});

describe("table-cell Markdown transport", () => {
  it("roundtrips separate paragraphs, formatting, pipes and literal break tags", () => {
    const doc = document(table(row(header(paragraph(text("head")))),
      row(cell(paragraph(text("one", "strong")), paragraph(text("two | <br> &"))))));
    const markdown = serializeComposerDocument(doc);
    expect(markdown).toContain("**one**<br><br>two \\| &lt;br&gt; &amp;");
    const restored = parseComposerMarkdown(markdown);
    expect(restored.toJSON()).toEqual(doc.toJSON());
    expect(serializeComposerDocument(restored)).toBe(markdown);
    assertCaretRoundtrip(doc);
  });

  it("roundtrips fenced code with line breaks and literal HTML inside a cell", () => {
    const content = 'a``b\n<br>\n<img src="https://example.com/image.png">';
    const doc = document(table(row(header(paragraph(text("head")))),
      row(cell(schema.node("code_block", { params: "text" }, text(content))))));
    const restored = parseComposerMarkdown(serializeComposerDocument(doc));
    expect(restored.toJSON()).toEqual(doc.toJSON());
    const images: Node[] = [];
    restored.descendants(node => { if (node.type.name === "image") images.push(node); });
    expect(images).toHaveLength(0);
    assertCaretRoundtrip(doc);
  });

  it("limits break decoding to table cells and leaves arbitrary HTML as text", () => {
    const doc = parseComposerMarkdown('| h |\n| --- |\n| first<br>second |\n\noutside <br> literal\n');
    const cellParagraph = doc.firstChild!.lastChild!.firstChild!.firstChild!;
    expect(cellParagraph.toJSON().content).toEqual([
      { type: "text", text: "first" }, { type: "hard_break" }, { type: "text", text: "second" },
    ]);
    expect(parseComposerMarkdown(serializeComposerDocument(doc)).toJSON()).toEqual(doc.toJSON());
    expect(doc.lastChild!.textContent).toBe("outside <br> literal");
    const literal = document(table(row(header(paragraph(text("head")))), row(cell(paragraph(text("<script>alert(1)</script>"))))));
    expect(parseComposerMarkdown(serializeComposerDocument(literal)).lastChild!.lastChild!.textContent).toBe("<script>alert(1)</script>");
  });

  it("retains automatic links and task-list state when restoring table blocks", () => {
    const doc = parseComposerMarkdown('| h |\n| --- |\n| https://example.com<br><br>* [x] done |');
    const cell = doc.firstChild!.lastChild!.firstChild!;
    expect(cell.firstChild!.firstChild!.marks[0].attrs.automatic).toBe(true);
    expect(cell.lastChild!.firstChild!.attrs.checked).toBe(true);
    expect(parseComposerMarkdown(serializeComposerDocument(doc)).toJSON()).toEqual(doc.toJSON());
  });
});

describe("plain-mode documents and meaningful content", () => {
  it("keeps plain Markdown literal and marks persisted state with its mode", () => {
    const value = "# Heading\n* literal\n中😀 `code`\n\n";
    const doc = restoreComposerDocument(value, undefined, true), saved = richDocumentState(doc, true);
    expect(saved.plainTextMode).toBe(true);
    expect(saved.markdown).toBe(value);
    expect(restoreComposerDocument(value, saved, true).toJSON()).toEqual(doc.toJSON());
    expect(restoreComposerDocument(value, saved).firstChild!.type.name).toBe("heading");
    assertCaretRoundtrip(doc, true);
  });

  it("restores structured documents only for the same text and mode", () => {
    const doc = document(paragraph(text("bold", "strong"))), saved = richDocumentState(doc);
    expect(restoreComposerDocument(saved.markdown, saved).toJSON()).toEqual(doc.toJSON());
    expect(restoreComposerDocument(saved.markdown, saved, true).textContent).toBe("**bold**");
    expect(restoreComposerDocument("new", saved).textContent).toBe("new");
    expect(serializeComposerDocument(literalComposerDocument("**literal**"), true)).toBe("**literal**");
  });

  it("ignores empty formatting, whitespace and breaks while counting text and inline atoms", () => {
    for (const doc of [document(paragraph(text(" \t "))), document(paragraph(schema.node("hard_break"))),
      document(schema.node("horizontal_rule")), document(schema.node("bullet_list", null,
        schema.node("list_item", { checked: false }, paragraph())))]) {
      expect(hasRichComposerContent(richDocumentState(doc))).toBe(false);
    }
    expect(hasRichComposerContent(richDocumentState(document(schema.node("code_block", { params: "js" }, text("x")))))).toBe(true);
    const image = schema.node("image", { src: "https://example.com/image.png", alt: "image" });
    expect(hasRichComposerContent(richDocumentState(document(paragraph(image))))).toBe(true);
    expect((schema.nodes.image.spec.toDOM!(image) as unknown[])[0]).toBe("span");
    expect(hasRichComposerContent(undefined, " plain ")).toBe(true);
    expect(hasRichComposerContent(undefined, "\n ")).toBe(false);
  });
});
