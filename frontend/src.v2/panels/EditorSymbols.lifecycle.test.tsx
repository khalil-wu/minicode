/* @vitest-environment jsdom */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { EditorSymbols } from "./EditorSymbols";
import { useAppStore } from "../stores";

const root = "/symbols";
const registrations: monaco.IDisposable[] = [];
const source = (index: number) => ({ uri: monaco.Uri.parse(`file://${root}/source${index}.ts`), content: `export const value${index} = ${index};` });
const symbol = (model: monaco.editor.ITextModel): monaco.languages.DocumentSymbol => ({
  name: model.getValue(), detail: "", kind: monaco.languages.SymbolKind.Variable, tags: [],
  range: model.getFullModelRange(), selectionRange: model.getFullModelRange(),
});
beforeAll(() => monaco.languages.register({ id: "typescript" }));
beforeEach(() => useAppStore.setState({ workingDirectory: root, editorTabs: [] }));
afterEach(() => { cleanup(); registrations.splice(0).forEach((item) => item.dispose()); monaco.editor.getModels().forEach((model) => model.dispose()); });

describe("project symbol model ownership", () => {
  it("reads all unopened sources with only one temporary model at a time and preserves an existing draft", async () => {
    const files = Array.from({ length: 300 }, (_, index) => source(index));
    const draft = monaco.editor.createModel("actual unsaved draft", "typescript", files[0].uri);
    let maximumModels = 0;
    registrations.push(monaco.languages.registerDocumentSymbolProvider("typescript", {
      provideDocumentSymbols(model) { maximumModels = Math.max(maximumModels, monaco.editor.getModels().length); return [symbol(model)]; },
    }));
    render(<EditorSymbols monaco={monaco} workspaceRoot={root} path={files[0].uri.toString()} project sourceFiles={() => files} onClose={() => {}} />);
    await screen.findByText(files[299].content);
    expect(maximumModels).toBe(2);
    expect(monaco.editor.getModels()).toEqual([draft]);
    expect(draft.getValue()).toBe("actual unsaved draft");
    expect(screen.getByText("actual unsaved draft")).toBeTruthy();
  });

  it("keeps a temporary source that the user edits while its symbol query is pending", async () => {
    const file = source(0);
    let finish!: (value: monaco.languages.DocumentSymbol[]) => void;
    registrations.push(monaco.languages.registerDocumentSymbolProvider("typescript", {
      provideDocumentSymbols() { return new Promise((resolve) => { finish = resolve; }); },
    }));
    render(<EditorSymbols monaco={monaco} workspaceRoot={root} path={file.uri.toString()} project sourceFiles={() => [file]} onClose={() => {}} />);
    await waitFor(() => expect(monaco.editor.getModel(file.uri)).not.toBeNull());
    const draft = monaco.editor.getModel(file.uri)!;
    draft.pushEditOperations([], [{ range: draft.getFullModelRange(), text: "user edit during query" }], () => []);
    await act(async () => finish([symbol(draft)]));
    expect(monaco.editor.getModel(file.uri)).toBe(draft);
    expect(draft.getValue()).toBe("user edit during query");
  });
});
