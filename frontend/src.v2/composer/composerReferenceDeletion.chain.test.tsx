/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TextSelection } from "prosemirror-state";
import { EditorView } from "prosemirror-view";
import type { Node } from "prosemirror-model";
import { RichComposerEditor } from "./RichComposerEditor";
import { inlineComposerReferences, markdownOffset, richComposerSchema as schema, richDocumentState, type RichComposerDocument } from "./rich-composer-document";
import type { InlineComposerReference } from "./composer-references";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

const references: InlineComposerReference[] = [
  { kind: "plugin", name: "Computer", configName: "computer-use@runtime", path: "plugin://computer-use@runtime" },
  { kind: "skill", name: "playwright", displayName: "Playwright CLI Skill", path: "C:/skills/playwright/SKILL.md" },
  { kind: "url", name: "Referenced chat", path: "codex://threads/referenced-chat" },
  { kind: "file", name: "main.ts", path: "C:/reference-deletion/main.ts" },
  { kind: "folder", name: "src", path: "C:/reference-deletion/src" },
];
const mention = (reference = references[1]) => schema.nodes.context_mention.create({ reference });
const paragraph = (...content: Node[]) => schema.nodes.paragraph.create(null, content);
const documentOf = (...content: Node[]) => schema.node("doc", null, content);
const noop = () => {};
let view: EditorView;

function Harness({ document, position, plainTextMode }: { document: Node; position: number; plainTextMode: boolean }) {
  const initialSelection = { start: markdownOffset(document, position, plainTextMode), end: markdownOffset(document, position, plainTextMode) };
  const [saved, setSaved] = useState<RichComposerDocument>(() => ({ ...richDocumentState(document, plainTextMode), selection: initialSelection }));
  return <RichComposerEditor value={saved.markdown} document={saved} initialSelection={initialSelection}
    initialFocus disabled={false} plainTextMode={plainTextMode} placeholder="Prompt" ariaLabel="Reference deletion"
    enterBehavior="enter" references={inlineComposerReferences(saved)}
    onChange={(_value, _selection, changed) => setSaved(changed)} onSelectionChange={noop}
    onFocusChange={noop} onKeyDown={noop} onPaste={noop} onDrop={noop} onCompositionChange={noop} />;
}

beforeEach(() => {
  const originalSetProps = EditorView.prototype.setProps;
  // Observe the production view without replacing any keymap, transaction or
  // deletion command. Geometry below only lets PM scroll in jsdom.
  vi.spyOn(EditorView.prototype, "setProps").mockImplementation(function (this: EditorView, props) {
    view = this;
    return originalSetProps.call(this, props);
  });
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const open = (document: Node, position: number, plainTextMode: boolean) => {
  render(<Harness document={document} position={position} plainTextMode={plainTextMode} />);
  act(() => { view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, position))); view.focus(); });
  return screen.getByRole("textbox", { name: "Reference deletion" });
};
const backspace = (input: HTMLElement) => fireEvent.keyDown(input, { key: "Backspace", keyCode: 8 });
const undo = (input: HTMLElement) => fireEvent.keyDown(input, { key: "z", keyCode: 90, ctrlKey: true });
const redo = (input: HTMLElement) => fireEvent.keyDown(input, { key: "z", keyCode: 90, ctrlKey: true, shiftKey: true });
const expectDocument = (document: Node, position: number) => {
  expect(view.state.doc.toJSON()).toEqual(document.toJSON());
  expect(view.state.selection).toBeInstanceOf(TextSelection);
  expect(view.state.selection.from).toBe(position);
  expect(view.state.selection.to).toBe(position);
};

describe.each([false, true])("reference deletion with plainTextMode=%s", plainTextMode => {
  it.each([
    { shortcut: "Shift+Enter", modifiers: { shiftKey: true } },
    { shortcut: "Alt+Enter", modifiers: { altKey: true } },
  ])("splits adjacent references into real paragraphs with $shortcut and retains their metadata", ({ modifiers }) => {
    const nodes = references.map(reference => mention(reference));
    const document = documentOf(paragraph(...nodes, schema.text("tail")));
    const split = documentOf(paragraph(...nodes.slice(0, 2)), paragraph(...nodes.slice(2), schema.text("tail")));
    const input = open(document, 3, plainTextMode);
    expect(fireEvent.keyDown(input, { key: "Enter", keyCode: 13, ...modifiers })).toBe(false);
    expectDocument(split, 5);
    expect(input.querySelectorAll("p")).toHaveLength(2);
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(5);
    expect(inlineComposerReferences(richDocumentState(view.state.doc, plainTextMode))).toEqual(references);
    undo(input);
    expectDocument(document, 3);
    redo(input);
    expectDocument(split, 5);
  });

  it("delegates the trailing space to native editing instead of consuming Backspace or inventing a paragraph", () => {
    const document = documentOf(paragraph(mention(), schema.text(" ")));
    const input = open(document, 3, plainTextMode);
    // fireEvent does not perform Chromium's default text deletion. Assert the
    // handoff here; electron-multi-agent.spec.ts covers the actual first key.
    expect(backspace(input)).toBe(true);
    expectDocument(document, 3);
    expect(input.querySelectorAll("p")).toHaveLength(1);
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(1);
  });

  it("deletes an adjacent Skill on the first real keymap transaction and restores it with undo/redo", () => {
    const document = documentOf(paragraph(mention()));
    const empty = documentOf(paragraph());
    const input = open(document, 2, plainTextMode);
    expect(backspace(input)).toBe(false);
    expectDocument(empty, 1);
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(0);
    undo(input);
    expectDocument(document, 2);
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(1);
    redo(input);
    expectDocument(empty, 1);
  });

  it("preserves a user-created empty paragraph until Backspace joins it without deleting the Skill", () => {
    const document = documentOf(paragraph(mention()), paragraph());
    const joined = documentOf(paragraph(mention()));
    const input = open(document, 4, plainTextMode);
    expect(input.querySelectorAll("p")).toHaveLength(2);
    expect(backspace(input)).toBe(false);
    expectDocument(joined, 2);
    expect(input.querySelectorAll("p")).toHaveLength(1);
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(1);
    undo(input);
    expectDocument(document, 4);
    redo(input);
    expectDocument(joined, 2);
    expect(backspace(input)).toBe(false);
    expectDocument(documentOf(paragraph()), 1);
  });

  it("removes only the targeted Skill among five mixed references and keeps all metadata through history", () => {
    const nodes = references.flatMap(reference => [mention(reference), schema.text(" ")]);
    const document = documentOf(paragraph(...nodes, schema.text("123")));
    const deleted = documentOf(paragraph(mention(references[0]), schema.text("  "), ...nodes.slice(4), schema.text("123")));
    const input = open(document, 4, plainTextMode);
    expect(backspace(input)).toBe(false);
    expectDocument(deleted, 3);
    expect(inlineComposerReferences(richDocumentState(view.state.doc, plainTextMode))).toEqual(references.filter(reference => reference.kind !== "skill"));
    expect(input.querySelectorAll("[data-composer-reference]")).toHaveLength(4);
    undo(input);
    expectDocument(document, 4);
    expect(inlineComposerReferences(richDocumentState(view.state.doc, plainTextMode))).toEqual(references);
    redo(input);
    expectDocument(deleted, 3);
  });
});
