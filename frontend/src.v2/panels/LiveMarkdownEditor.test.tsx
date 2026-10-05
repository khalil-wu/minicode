/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { defaultUrlTransform } from "react-markdown";
import { useRef, useState } from "react";
import { LiveMarkdownEditor, type MarkdownEditorSession } from "./LiveMarkdownEditor";
import type { EditorTextSurface } from "./editor-text-surface";
import { useAgentEditReview } from "./useAgentEditReview";

beforeAll(() => {
  // JSDOM has no layout engine; CodeMirror's range measurement still needs the
  // browser geometry APIs while these tests exercise its real document state.
  const rect = new DOMRect(0, 0, 800, 24);
  Range.prototype.getBoundingClientRect = () => rect;
  Range.prototype.getClientRects = () => ({ 0: rect, length: 1, item: () => rect, [Symbol.iterator]: function* () { yield rect; } }) as DOMRectList;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => cleanup());

const fixture = (source: string) => {
  let editor: EditorTextSurface;
  let value = source;
  const onChange = vi.fn((next: string) => { value = next; });
  const sessions = new Map<string, MarkdownEditorSession>();
  const props = {
    documentId: "readme", sessions, value: source, readOnly: false, textScale: 1,
    components: {}, urlTransform: defaultUrlTransform, resolveUrl: (url: string) => url.startsWith("#") ? `#doc-${url.slice(1)}` : url,
    scopeId: "doc", onChange, onMount: (handle: EditorTextSurface) => { editor = handle; },
  };
  return { props, onChange, sessions, editor: () => editor, value: () => value };
};

describe("LiveMarkdownEditor", () => {
  it("records unopened Markdown buffer replacements into native Undo when the document first mounts", () => {
    const before = "# foo\r\n\r\n😀 foo\r\n";
    const after = "# bar\r\n\r\n😀 foo\r\n";
    const state = fixture(after);
    const consumed = vi.fn();
    render(<LiveMarkdownEditor {...state.props} bufferTransactions={[{ before, after, label: "project replace", edits: [{ offset: 2, length: 3, text: "bar" }] }]} onConsumeBufferTransactions={consumed} />);
    expect(state.value()).toBe(after);
    expect(consumed).toHaveBeenCalledOnce();
    act(() => state.editor().getAction!("undo")!.run());
    expect(state.value()).toBe(before);
    act(() => state.editor().getAction!("redo")!.run());
    expect(state.value()).toBe(after);
  });
  it("restores serialized reading position into a fresh Markdown editor without its in-memory session", () => {
    const first = fixture("# Title\n\nFirst paragraph\n\nSecond paragraph\n\nLast line");
    const rendered = render(<LiveMarkdownEditor {...first.props} />);
    act(() => first.editor().setSelection!({ startLineNumber: 3, startColumn: 2, endLineNumber: 5, endColumn: 6 }));
    const scroll = rendered.container.querySelector<HTMLElement>(".cm-scroller")!;
    scroll.scrollTop = 170;
    const saved = JSON.parse(JSON.stringify(first.editor().saveViewState!()));
    rendered.unmount();
    const restored = fixture(first.props.value);
    const next = render(<LiveMarkdownEditor {...restored.props} />);
    act(() => restored.editor().restoreViewState!(saved));
    expect(restored.editor().getSelection()).toEqual({ startLineNumber: 3, startColumn: 2, endLineNumber: 5, endColumn: 6 });
    expect(next.container.querySelector<HTMLElement>(".cm-scroller")!.scrollTop).toBe(170);
    expect(restored.value()).toBe(first.props.value);
  });
  it("restores a multi-line source range without losing its end position", () => {
    const state = fixture("first\nsecond line\nthird line\nlast");
    render(<LiveMarkdownEditor {...state.props} />);
    const range = { startLineNumber: 2, startColumn: 3, endLineNumber: 3, endColumn: 6 };
    act(() => state.editor().setSelection!(range));
    expect(state.editor().getSelection()).toEqual(range);
    expect(state.editor().getModel()!.getValueInRange(state.editor().getSelection())).toBe("cond line\nthird");
  });
  it("reviews the restored Markdown cursor's block after switching documents", () => {
    const source = "line1\nADDED\nline2\nline3\nALSO\nline4\n";
    const state = fixture(source);
    const turnDiff = {
      threadId: "thread", turnId: "turn", updatedAt: 1,
      diff: "diff --git a/first.md b/first.md\n--- a/first.md\n+++ b/first.md\n@@ -1,4 +1,6 @@\n line1\n+ADDED\n line2\n line3\n+ALSO\n line4",
    };
    let surface: EditorTextSurface;
    const Harness = ({ path }: { path: string }) => {
      const editorRef = useRef<EditorTextSurface | null>(null);
      const [editorEpoch, setEditorEpoch] = useState(0);
      const review = useAgentEditReview({
        editorRef, path, content: source, turnDiff, readOnly: false,
        workingDirectory: "/repo", editorEpoch,
      });
      return <>
        <LiveMarkdownEditor {...state.props} documentId={path} onMount={(editor) => {
          surface = editor;
          editorRef.current = editor;
          editor.onDidChangeCursorPosition(({ position }) => review.onCursorLine(position.lineNumber));
          setEditorEpoch((epoch) => epoch + 1);
        }} />
        <output aria-label="当前审阅行">{review.currentLine}</output>
        <button onClick={review.undo}>撤销当前改动</button>
      </>;
    };
    const view = render(<Harness path="first.md" />);
    act(() => surface.setPosition!({ lineNumber: 5, column: 3 }));
    expect(screen.getByLabelText("当前审阅行").textContent).toBe("5");

    view.rerender(<Harness path="second.md" />);
    view.rerender(<Harness path="first.md" />);
    expect(surface!.getPosition()).toEqual({ lineNumber: 5, column: 3 });
    expect(screen.getByLabelText("当前审阅行").textContent).toBe("5");
    fireEvent.click(screen.getByRole("button", { name: "撤销当前改动" }));
    expect(state.value()).toBe("line1\nADDED\nline2\nline3\nline4\n");
  });

  it.each([["\n", "\r\n"], ["\r\n", "\n"]])("undoes a source EOL review from %j to %j together with its text", (currentEol, removedEol) => {
    const source = `# NEW${currentEol}Body`;
    const state = fixture(source);
    render(<LiveMarkdownEditor {...state.props} />);
    act(() => state.editor().executeEdits("agent-edit-undo", [{ range: { startLineNumber: 1, startColumn: 1, endLineNumber: 2, endColumn: 5 }, text: `# Original${removedEol}Restored${removedEol}`, eol: removedEol }]));
    expect(state.value()).toBe(`# Original${removedEol}Restored${removedEol}`);
    expect(state.editor().getModel()!.getEOL!()).toBe(removedEol);
    expect(state.editor().getModel()!.getLineMaxColumn(1)).toBe(11);
    act(() => state.editor().getAction!("undo")!.run());
    expect(state.value()).toBe(source);
    expect(state.editor().getModel()!.getEOL!()).toBe(currentEol);
    act(() => state.editor().getAction!("redo")!.run());
    expect(state.value()).toBe(`# Original${removedEol}Restored${removedEol}`);
    expect(state.editor().getModel()!.getEOL!()).toBe(removedEol);
  });

  it("isolates adjacent program edits in the real CodeMirror Undo/Redo history", () => {
    const state = fixture("# Title\n\nOriginal");
    render(<LiveMarkdownEditor {...state.props} />);
    act(() => state.editor().executeEdits("typing", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 9 }, text: "BRAVO" }]));
    act(() => state.editor().executeEdits("agent-edit-undo", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 6 }, text: "Restored" }]));
    expect(state.value()).toBe("# Title\n\nRestored");
    act(() => state.editor().getAction!("undo")!.run());
    expect(state.value()).toBe("# Title\n\nBRAVO");
    act(() => state.editor().getAction!("undo")!.run());
    expect(state.value()).toBe("# Title\n\nOriginal");
    act(() => state.editor().getAction!("redo")!.run());
    expect(state.value()).toBe("# Title\n\nBRAVO");
    act(() => state.editor().getAction!("redo")!.run());
    expect(state.value()).toBe("# Title\n\nRestored");
  });

  it("renders Markdown and edits the original source within the same surface", async () => {
    const state = fixture("# Title\n\nA **bold** paragraph and `code`.");
    const { container } = render(<LiveMarkdownEditor {...state.props} />);
    expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy();
    expect(container.querySelector(".live-md-strong")?.textContent).toBe("bold");
    expect(container.querySelector(".live-md-code")?.textContent).toBe("code");
    expect(state.onChange).not.toHaveBeenCalled();

    act(() => {
      state.editor().focus();
      state.editor().executeEdits("typing", [{ range: { startLineNumber: 1, startColumn: 3, endLineNumber: 1, endColumn: 8 }, text: "Updated" }]);
    });
    expect(state.value()).toBe("# Updated\n\nA **bold** paragraph and `code`.");
    expect(screen.getByRole("heading", { name: "Updated" }).textContent).toBe("# Updated");
    fireEvent.blur(container.querySelector(".cm-content")!);
    expect(screen.getByRole("heading", { name: "Updated" }).textContent).toBe("Updated");
    expect(container.querySelectorAll(".cm-content")).toHaveLength(1);
  });

  it("keeps tables and code blocks editable by returning their source on click", async () => {
    const source = "Intro\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```js\nconst count = 1\n```";
    const state = fixture(source);
    render(<LiveMarkdownEditor {...state.props} />);
    const table = await screen.findByRole("table");
    expect(table.textContent).toContain("A");
    fireEvent.mouseDown(table, { button: 0 });
    await waitFor(() => expect(screen.queryByRole("table")).toBeNull());
    expect(state.editor().getSelection()).toEqual({ startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 1 });
    expect(state.onChange).not.toHaveBeenCalled();
    act(() => state.editor().setPosition!({ lineNumber: 8, column: 15 }));
    expect(state.editor().getPosition()).toEqual({ lineNumber: 8, column: 15 });
    expect(state.editor().getModel()!.getValueInRange({ startLineNumber: 7, startColumn: 1, endLineNumber: 9, endColumn: 4 })).toBe("```js\nconst count = 1\n```");
  });

  it("updates task Markdown when clicking a rendered checkbox", async () => {
    const state = fixture("# Tasks\n\n- [ ] Ship the update");
    render(<LiveMarkdownEditor {...state.props} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "标记为完成" }));
    expect(state.value()).toBe("# Tasks\n\n- [x] Ship the update");
  });

  it("jumps to duplicate headings using source positions without leaving the editor", () => {
    const source = "[Second](#title-2)\n\n# Title\n\nFirst\n\n# Title\n\nSecond";
    const state = fixture(source);
    render(<LiveMarkdownEditor {...state.props} />);
    fireEvent.click(screen.getByRole("link", { name: "Second" }));
    const line = source.slice(0, source.lastIndexOf("# Title")).split("\n").length;
    expect(state.editor().getSelection()).toEqual({ startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: 1 });
    expect(state.onChange).not.toHaveBeenCalled();
  });

  it("uses visible heading text for fragments when a heading contains Markdown formatting", () => {
    const source = "[Jump](#hello-world)\n\n# **Hello** [world](https://example.com)";
    const state = fixture(source);
    render(<LiveMarkdownEditor {...state.props} />);
    expect(screen.getByRole("heading", { name: "Hello world" }).id).toBe("doc-hello-world");
    fireEvent.click(screen.getByRole("link", { name: "Jump" }));
    expect(state.editor().getSelection()).toEqual({ startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 1 });
  });

  it("restores cursor, scroll, source edits and undo history when a file remounts", () => {
    const state = fixture("# Title\n\nOriginal paragraph");
    const first = render(<LiveMarkdownEditor {...state.props} />);
    act(() => {
      state.editor().executeEdits("typing", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 9 }, text: "Changed" }]);
      state.editor().setPosition!({ lineNumber: 3, column: 5 });
    });
    const selection = state.editor().getSelection();
    first.container.querySelector(".cm-scroller")!.scrollTop = 123;
    first.unmount();

    const second = render(<LiveMarkdownEditor {...state.props} value={state.value()} />);
    expect(state.editor().getSelection()).toEqual(selection);
    expect(state.editor().getPosition()).toEqual({ lineNumber: 3, column: 5 });
    expect(second.container.querySelector(".cm-scroller")!.scrollTop).toBe(123);
    const selectionChanged = vi.fn();
    state.editor().onDidChangeCursorSelection(selectionChanged);
    act(() => state.editor().setPosition!({ lineNumber: 1, column: 2 }));
    expect(selectionChanged).toHaveBeenCalledOnce();
    act(() => state.editor().getAction!("undo")!.run());
    expect(state.value()).toBe("# Title\n\nOriginal paragraph");
  });

  it("preserves CRLF source and supports line review decorations and source replacements", () => {
    const state = fixture("# Title\r\n\r\nOriginal");
    const { container } = render(<LiveMarkdownEditor {...state.props} />);
    expect(state.onChange).not.toHaveBeenCalled();
    expect(state.editor().getModel()!.getEOL!()).toBe("\r\n");
    let collection: ReturnType<NonNullable<EditorTextSurface["createDecorationsCollection"]>>;
    act(() => {
      collection = state.editor().createDecorationsCollection!([{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 1 }, options: { className: "agent-edit-line-replace" } }]);
      state.editor().executeEdits("agent-edit-undo", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 9 }, text: "Restored" }]);
    });
    expect(state.value()).toBe("# Title\r\n\r\nRestored");
    expect(container.querySelector(".agent-edit-line-replace")).toBeTruthy();
    act(() => collection.clear());
    expect(container.querySelector(".agent-edit-line-replace")).toBeNull();
  });

  it.each([["\n", "\r\n"], ["\r\n", "\n"]])("refreshes external source from %j to %j without writing a new draft", (beforeEOL, afterEOL) => {
    const state = fixture(["# Title", "", "Original"].join(beforeEOL));
    const editor = render(<LiveMarkdownEditor {...state.props} />);
    const refreshed = ["# Disk", "", "External paragraph"].join(afterEOL);
    editor.rerender(<LiveMarkdownEditor {...state.props} value={refreshed} />);
    const model = state.editor().getModel()!;
    expect(model.getLineCount()).toBe(3);
    expect(model.getEOL!()).toBe(afterEOL);
    expect(model.getValueInRange({ startLineNumber: 1, startColumn: 1, endLineNumber: 3, endColumn: 19 })).toBe(refreshed);
    expect(state.onChange).not.toHaveBeenCalled();
    act(() => state.editor().executeEdits("typing", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 9 }, text: "User" }]));
    expect(state.onChange).toHaveBeenCalledExactlyOnceWith(["# Disk", "", "User paragraph"].join(afterEOL));
  });

  it.each([["\n", "\r\n"], ["\r\n", "\n"]])("restores cached source with external EOL changed from %j to %j", (beforeEOL, afterEOL) => {
    const state = fixture(["# Title", "", "Original"].join(beforeEOL));
    const first = render(<LiveMarkdownEditor {...state.props} />);
    first.unmount();
    const refreshed = ["# Agent edit", "", "Updated on disk"].join(afterEOL);
    render(<LiveMarkdownEditor {...state.props} value={refreshed} />);
    const model = state.editor().getModel()!;
    expect(model.getLineCount()).toBe(3);
    expect(model.getEOL!()).toBe(afterEOL);
    expect(model.getValueInRange({ startLineNumber: 1, startColumn: 1, endLineNumber: 3, endColumn: 16 })).toBe(refreshed);
    expect(state.onChange).not.toHaveBeenCalled();
    act(() => state.editor().executeEdits("typing", [{ range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 8 }, text: "User" }]));
    expect(state.value()).toBe(["# Agent edit", "", "User on disk"].join(afterEOL));
  });

  it("exposes real find and line navigation while keeping read-only documents rendered", () => {
    const state = fixture("# Title\n\nBody");
    const { container } = render(<LiveMarkdownEditor {...state.props} readOnly />);
    expect(container.querySelector(".cm-content")!.getAttribute("contenteditable")).toBe("false");
    act(() => state.editor().getAction!("actions.find")!.run());
    expect(container.querySelector(".cm-search")).toBeTruthy();
    expect(state.editor().getAction!("editor.action.gotoLine")).toBeTruthy();
    expect(state.editor().getAction!("editor.action.formatDocument")).toBeNull();
  });
});
