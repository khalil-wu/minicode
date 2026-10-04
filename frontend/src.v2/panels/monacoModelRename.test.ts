/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { renameMonacoModel } from "./monacoModelRename";

vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
});
afterEach(() => { for (const model of monaco.editor.getModels()) model.dispose(); });

const replace = (model: monaco.editor.ITextModel, value: string) => {
  model.pushStackElement();
  model.pushEditOperations([new monaco.Selection(1, 1, 1, 1)], [{ range: model.getFullModelRange(), text: value }], () => [new monaco.Selection(1, 2, 1, 2)]);
  model.pushStackElement();
};

describe("Monaco file-model rename", () => {
  it("moves real undo groups, redo and saved-version identity to the new file URI", async () => {
    const original = monaco.editor.createModel("first", "plaintext", monaco.Uri.parse("file:///workspace/old.txt"));
    const savedVersion = original.getAlternativeVersionId();
    replace(original, "second");
    const secondVersion = original.getAlternativeVersionId();
    replace(original, "third");
    const thirdVersion = original.getAlternativeVersionId();
    replace(original, "fourth");
    const fourthVersion = original.getAlternativeVersionId();
    await original.undo();
    expect(original.getValue()).toBe("third");
    const versionBeforeRename = original.getVersionId();

    const renamed = renameMonacoModel(monaco, original, monaco.Uri.parse("file:///workspace/new.txt"));
    original.dispose();
    expect(renamed.uri.path).toBe("/workspace/new.txt");
    expect(renamed.getValue()).toBe("third");
    expect(renamed.getVersionId()).toBe(versionBeforeRename);
    expect(renamed.getAlternativeVersionId()).toBe(thirdVersion);
    expect(renamed.getAlternativeVersionId()).not.toBe(savedVersion);
    expect(renamed.canRedo()).toBe(true);

    await renamed.undo();
    expect(renamed.getValue()).toBe("second");
    expect(renamed.getAlternativeVersionId()).toBe(secondVersion);
    await renamed.undo();
    expect(renamed.getValue()).toBe("first");
    expect(renamed.getAlternativeVersionId()).toBe(savedVersion);
    expect(renamed.canUndo()).toBe(false);
    await renamed.redo();
    expect(renamed.getValue()).toBe("second");
    await renamed.redo();
    expect(renamed.getAlternativeVersionId()).toBe(thirdVersion);
    await renamed.redo();
    expect(renamed.getValue()).toBe("fourth");
    expect(renamed.getAlternativeVersionId()).toBe(fourthVersion);
    expect(renamed.canRedo()).toBe(false);
  });

  it("continues editing after rename with the previous undo history and CRLF intact", async () => {
    const original = monaco.editor.createModel("first\r\nline", "plaintext", monaco.Uri.parse("file:///workspace/crlf-old.txt"));
    original.updateOptions({ tabSize: 2, insertSpaces: false });
    replace(original, "second\r\nline");
    const renamed = renameMonacoModel(monaco, original, monaco.Uri.parse("file:///workspace/crlf-new.txt"));
    original.dispose();
    expect(renamed.getEOL()).toBe("\r\n");
    expect(renamed.getOptions()).toMatchObject({ tabSize: 2, insertSpaces: false });
    replace(renamed, "third\r\nline");
    await renamed.undo();
    expect(renamed.getValue()).toBe("second\r\nline");
    await renamed.undo();
    expect(renamed.getValue()).toBe("first\r\nline");
  });

  it("renames an unedited model without inventing an undo step", () => {
    const original = monaco.editor.createModel("original", "plaintext", monaco.Uri.parse("file:///workspace/clean-old.txt"));
    const savedVersion = original.getAlternativeVersionId();
    const renamed = renameMonacoModel(monaco, original, monaco.Uri.parse("file:///workspace/clean-new.txt"));
    original.dispose();
    expect(renamed.getValue()).toBe("original");
    expect(renamed.getAlternativeVersionId()).toBe(savedVersion);
    expect(renamed.canUndo()).toBe(false);
  });

  it("reuses an already indexed destination while preserving the source's dirty state and redo history", async () => {
    const original = monaco.editor.createModel("original\r\nline", "plaintext", monaco.Uri.parse("file:///workspace/source.txt"));
    original.updateOptions({ tabSize: 2, insertSpaces: false });
    const cleanVersion = original.getAlternativeVersionId();
    replace(original, "first edit\r\nline");
    const dirtyVersion = original.getAlternativeVersionId();
    replace(original, "second edit\r\nline");
    await original.undo();
    const targetUri = monaco.Uri.parse("file:///workspace/indexed-target.txt");
    const indexedTarget = monaco.editor.createModel("disk snapshot\nold line", "plaintext", targetUri);
    indexedTarget.updateOptions({ tabSize: 4, insertSpaces: true });
    replace(indexedTarget, "target history to replace");

    const renamed = renameMonacoModel(monaco, original, targetUri);
    original.dispose();
    expect(renamed).toBe(indexedTarget);
    expect(renamed.getValue()).toBe("first edit\r\nline");
    expect(renamed.getEOL()).toBe("\r\n");
    expect(renamed.getOptions()).toMatchObject({ tabSize: 2, insertSpaces: false });
    expect(renamed.getAlternativeVersionId()).toBe(dirtyVersion);
    expect(renamed.getAlternativeVersionId()).not.toBe(cleanVersion);
    expect(renamed.canRedo()).toBe(true);
    await renamed.redo();
    expect(renamed.getValue()).toBe("second edit\r\nline");
    await renamed.undo();
    expect(renamed.getValue()).toBe("first edit\r\nline");
    await renamed.undo();
    expect(renamed.getValue()).toBe("original\r\nline");
    expect(renamed.getAlternativeVersionId()).toBe(cleanVersion);
    expect(renamed.canUndo()).toBe(false);
  });
});
