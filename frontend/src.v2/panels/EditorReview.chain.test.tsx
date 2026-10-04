// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { useAgentEditReview, type AgentEditReviewModel } from "./useAgentEditReview";
import type { TurnDiffState } from "../stores/types";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", {
  configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
afterEach(() => { cleanup(); for (const model of monaco.editor.getModels()) model.dispose(); });
const body = "@@ -1,3 +1,3 @@\n alpha\n-bravo\n+BRAVO\n charlie\n";
const patch = (body: string) => `diff --git a/src/main.ts b/src/main.ts\n--- a/src/main.ts\n+++ b/src/main.ts\n${body}`;
const turn = (body: string, revision = 1, threadId = "A"): TurnDiffState => ({ threadId, turnId: "same-turn", diff: patch(body), revision, updatedAt: revision });
const handle = (model: monaco.editor.ITextModel): NonNullable<AgentEditReviewModel["editor"]> => ({
  getModel: () => model, pushUndoStop: () => model.pushStackElement(), focus: vi.fn(),
  executeEdits: (_source, edits) => { model.pushEditOperations([], edits.map((edit) => ({ ...edit, range: monaco.Range.lift(edit.range as monaco.IRange) })), () => []); },
});
const create = (content = "alpha\nBRAVO\ncharlie\n") => monaco.editor.createModel(content, "plaintext", monaco.Uri.parse("file:///review/src/main.ts"));

describe("actual editor review ownership and native Undo chains", () => {
  it("keeps an accepted block dismissed when the same turn publishes another revision", () => {
    const model = create();
    const args = { editorRef: { current: handle(model) }, path: "src/main.ts", content: model.getValue(), readOnly: false,
      turnDiff: turn(body), workingDirectory: "/review", editorEpoch: 1 };
    const { result, rerender } = renderHook((props) => useAgentEditReview(props), { initialProps: args });
    act(() => result.current.keep());
    expect(result.current.total).toBe(0);
    rerender({ ...args, turnDiff: turn(body, 2) });
    expect(result.current.total).toBe(0);
  });
  it("reviews changed block content in a later revision", () => {
    const model = create();
    const args = { editorRef: { current: handle(model) }, path: "src/main.ts", content: model.getValue(), readOnly: false,
      turnDiff: turn(body), workingDirectory: "/review", editorEpoch: 1 };
    const { result, rerender } = renderHook((props) => useAgentEditReview(props), { initialProps: args });
    act(() => result.current.keep());
    const nextBody = "@@ -1,3 +1,3 @@\n-alpha\n+ALPHA\n-bravo\n+BRAVO\n charlie\n";
    rerender({ ...args, content: "ALPHA\nBRAVO\ncharlie\n", turnDiff: turn(nextBody, 2) });
    expect(result.current.total).toBe(1);
  });
  it("does not transfer a kept repeated block to another occurrence after the first hunk stops matching", () => {
    const repeated = "@@ -1,3 +1,3 @@\n first\n-old\n+NEW\n tail1\n@@ -5,3 +5,3 @@\n second\n-old\n+NEW\n tail2\n";
    const model = create("first\nNEW\ntail1\ngap\nsecond\nNEW\ntail2\n");
    const args = { editorRef: { current: handle(model) }, path: "src/main.ts", content: model.getValue(), readOnly: false,
      turnDiff: turn(repeated), workingDirectory: "/review", editorEpoch: 1 };
    const { result, rerender } = renderHook((props) => useAgentEditReview(props), { initialProps: args });
    act(() => result.current.keep());
    expect(result.current.total).toBe(1);
    rerender({ ...args, content: args.content.replace("first\nNEW", "first\nUSER") });
    expect(result.current.total).toBe(1);
    act(() => result.current.undo());
    expect(model.getValue()).toBe("first\nNEW\ntail1\ngap\nsecond\nold\ntail2\n");
  });

  it.each(["\n", "\r\n"])("restores deleted %j EOL and the source text in one native Undo/Redo event", async (eol) => {
    const model = create("NEW");
    const startingEol = model.getEOL();
    const source = `old${eol}tail${eol}`;
    const sourcePatch = `@@ -1,2 +1 @@\n-old${eol}-tail${eol}+NEW\n\\ No newline at end of file\n`;
    const { result } = renderHook(() => useAgentEditReview({ editorRef: { current: handle(model) }, path: "src/main.ts",
      content: "NEW", readOnly: false, turnDiff: turn(sourcePatch), workingDirectory: "/review", editorEpoch: 1 }));
    act(() => result.current.undo());
    expect(model.getValue()).toBe(source);
    expect(model.getEOL()).toBe(eol);
    await model.undo();
    expect(model.getValue()).toBe("NEW");
    expect(model.getEOL()).toBe(startingEol);
    await model.redo();
    expect(model.getValue()).toBe(source);
    expect(model.getEOL()).toBe(eol);
  });
  it.each([["\n", "\r\n"], ["\r\n", "\n"]])("restores a whole multiline %j file from a %j model through one native Undo/Redo", async (beforeEol, afterEol) => {
    const before = `alpha${beforeEol}bravo${beforeEol}`;
    const after = `ALPHA${afterEol}BRAVO${afterEol}`;
    const model = create(after);
    const patch = `@@ -1,2 +1,2 @@\n-alpha${beforeEol}-bravo${beforeEol}+ALPHA${afterEol}+BRAVO${afterEol}`;
    const { result } = renderHook(() => useAgentEditReview({ editorRef: { current: handle(model) }, path: "src/main.ts",
      content: after, readOnly: false, turnDiff: turn(patch), workingDirectory: "/review", editorEpoch: 1 }));
    act(() => result.current.undo());
    expect(model.getValue()).toBe(before);
    await model.undo();
    expect(model.getValue()).toBe(after);
    await model.redo();
    expect(model.getValue()).toBe(before);
  });
  it.each(["workspace", "thread"])("scopes accepted blocks to their %s owner", (owner) => {
    const model = create();
    const args = { editorRef: { current: handle(model) }, path: "src/main.ts", content: model.getValue(), readOnly: false,
      turnDiff: turn(body), workingDirectory: "/review", editorEpoch: 1 };
    const { result, rerender } = renderHook((props) => useAgentEditReview(props), { initialProps: args });
    act(() => result.current.keep());
    rerender({ ...args, ...(owner === "workspace" ? { workingDirectory: "/other" } : { turnDiff: turn(body, 1, "B") }) });
    expect(result.current.total).toBe(1);
  });
  it("undoes only the reviewed block, with prior typing and Redo intact", async () => {
    const model = create();
    const initial = model.getValue();
    model.pushEditOperations([], [{ range: new monaco.Range(4, 1, 4, 1), text: "USER\n" }], () => []);
    const typed = model.getValue();
    const { result } = renderHook(() => useAgentEditReview({ editorRef: { current: handle(model) }, path: "src/main.ts",
      content: typed, readOnly: false, turnDiff: turn(body), workingDirectory: "/review", editorEpoch: 1 }));
    act(() => result.current.undo());
    expect(model.getValue()).toBe("alpha\nbravo\ncharlie\nUSER\n");
    await model.undo();
    expect(model.getValue()).toBe(typed);
    await model.undo();
    expect(model.getValue()).toBe(initial);
    await model.redo();
    expect(model.getValue()).toBe(typed);
    await model.redo();
    expect(model.getValue()).toBe("alpha\nbravo\ncharlie\nUSER\n");
  });
  it("restores a completely removed unterminated file using a real model", async () => {
    const model = create("");
    const { result } = renderHook(() => useAgentEditReview({ editorRef: { current: handle(model) }, path: "src/main.ts",
      content: "", readOnly: false, turnDiff: turn("@@ -1 +0,0 @@\n-old\n\\ No newline at end of file\n"), workingDirectory: "/review", editorEpoch: 1 }));
    act(() => result.current.undo());
    expect(model.getValue()).toBe("old");
    await model.undo();
    expect(model.getValue()).toBe("");
  });
});
