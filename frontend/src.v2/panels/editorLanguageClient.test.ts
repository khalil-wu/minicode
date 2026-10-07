/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Monaco from "monaco-editor/editor/editor.api.js";
import { registerEditorLanguageServices } from "./editorLanguageClient";
import { useAppStore } from "../stores";
import { editorModelUri } from "./monacoLanguageServices";
import { isCancellationError } from "monaco-editor/base/common/errors.js";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  Object.defineProperty(globalThis, "CSS", { configurable: true, value: { escape: (value: string) => value } });
  Object.defineProperty(window, "matchMedia", { configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
});

const captured = new Map<string, Monaco.languages.CompletionItemProvider>();
const symbols = new Map<string, Monaco.languages.DocumentSymbolProvider>();
let registration: Monaco.IDisposable;
beforeEach(() => {
  useAppStore.setState({ workingDirectory: "/cpp-editor-test" });
  const real = Monaco.languages.registerCompletionItemProvider;
  vi.spyOn(Monaco.languages, "registerCompletionItemProvider").mockImplementation((selector, provider) => {
    if (typeof selector === "string") captured.set(selector, provider);
    return real(selector, provider);
  });
  const realSymbols = Monaco.languages.registerDocumentSymbolProvider;
  vi.spyOn(Monaco.languages, "registerDocumentSymbolProvider").mockImplementation((selector, provider) => {
    if (typeof selector === "string") symbols.set(selector, provider);
    return realSymbols(selector, provider);
  });
  registration = registerEditorLanguageServices(Monaco);
});
afterEach(() => {
  registration.dispose();
  Monaco.editor.getModels().forEach((model) => model.dispose());
  captured.clear();
  symbols.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("native C/C++ Monaco provider bridge", () => {
  it("maps both LSP document-symbol result variants without inventing ranges", async () => {
    const model = Monaco.editor.createModel("class Ledger:\n    def total(self):\n        return 42\n", "python", Monaco.Uri.parse(editorModelUri("ledger.py", "/cpp-editor-test")));
    const range = { start: { line: 0, character: 0 }, end: { line: 2, character: 17 } };
    const selection = { start: { line: 0, character: 6 }, end: { line: 0, character: 12 } };
    const childRange = { start: { line: 1, character: 4 }, end: { line: 2, character: 17 } };
    const provider = symbols.get("python")!;
    const token = new Monaco.CancellationTokenSource();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ result: [{ name: "Ledger", kind: 5, range, selectionRange: selection,
      children: [{ name: "total", kind: 6, range: childRange, selectionRange: childRange }] }] }), { status: 200 })));
    const hierarchical = await provider.provideDocumentSymbols(model, token.token);
    expect(hierarchical![0]).toMatchObject({ name: "Ledger", kind: Monaco.languages.SymbolKind.Class,
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 3, endColumn: 18 },
      selectionRange: { startLineNumber: 1, startColumn: 7, endLineNumber: 1, endColumn: 13 },
      children: [{ name: "total", kind: Monaco.languages.SymbolKind.Method }] });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ result: [{ name: "total", kind: 6, containerName: "Ledger",
      location: { uri: model.uri.toString(), range: childRange } }] }), { status: 200 })));
    const flat = await provider.provideDocumentSymbols(model, token.token);
    expect(flat![0]).toMatchObject({ name: "total", detail: "Ledger", kind: Monaco.languages.SymbolKind.Method,
      range: { startLineNumber: 2, startColumn: 5, endLineNumber: 3, endColumn: 18 },
      selectionRange: { startLineNumber: 2, startColumn: 5, endLineNumber: 3, endColumn: 18 } });
    token.dispose();
  });

  it("does not launch an already-cancelled Monaco request", async () => {
    const model = Monaco.editor.createModel("value = 1", "python", Monaco.Uri.parse(editorModelUri("cancelled.py", "/cpp-editor-test")));
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const token = new Monaco.CancellationTokenSource();
    token.cancel();
    await expect(symbols.get("python")!.provideDocumentSymbols(model, token.token)).rejects.toSatisfy(isCancellationError);
    expect(fetch).not.toHaveBeenCalled();
    token.dispose();
  });

  it("preserves Monaco cancellation through the pending response body", async () => {
    const model = Monaco.editor.createModel("value = 1", "python", Monaco.Uri.parse(editorModelUri("body.py", "/cpp-editor-test")));
    let signal!: AbortSignal;
    let body!: ReadableStreamDefaultController<Uint8Array>;
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
      signal = init.signal;
      const stream = new ReadableStream<Uint8Array>({ start(controller) { body = controller; } });
      signal.addEventListener("abort", () => body.error(new DOMException("fetch body aborted", "AbortError")), { once: true });
      return new Response(stream, { status: 200 });
    }));
    const token = new Monaco.CancellationTokenSource();
    const request = symbols.get("python")!.provideDocumentSymbols(model, token.token);
    const cancelled = expect(request).rejects.toSatisfy(isCancellationError);
    await vi.waitFor(() => expect(signal).toBeDefined());
    token.cancel();
    await cancelled;
    expect(isCancellationError(signal.reason)).toBe(true);
    token.dispose();
  });

  it("propagates server failures instead of treating them as cancelled or empty outlines", async () => {
    const model = Monaco.editor.createModel("value = 1", "python", Monaco.Uri.parse(editorModelUri("failure.py", "/cpp-editor-test")));
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"language server failed"}', { status: 500 })));
    const token = new Monaco.CancellationTokenSource();
    await expect(symbols.get("python")!.provideDocumentSymbols(model, token.token)).rejects.toThrow("language server failed");
    token.dispose();
  });

  it("triggers at # and maps native include edits and snippets while sending unsaved C++ and header buffers", async () => {
    await import("monaco-editor/languages/definitions/cpp/register.js");
    const model = Monaco.editor.createModel("#", "cpp", Monaco.Uri.parse(editorModelUri("main.cpp", "/cpp-editor-test")));
    const header = Monaco.editor.createModel("int unsaved_header_function();", "c", Monaco.Uri.parse(editorModelUri("local.h", "/cpp-editor-test")));
    const fetch = vi.fn(async (_url, init) => {
      expect(JSON.parse(init.body)).toMatchObject({ method: "completion", line: 0, character: 1,
        documents: [{ path: model.uri.fsPath, content: "#", language: "cpp" },
          { path: header.uri.fsPath, content: "int unsaved_header_function();", language: "c" }] });
      return new Response(JSON.stringify({ result: { isIncomplete: false, items: [{
        label: " include <header>", kind: 15, insertTextFormat: 2, filterText: "include",
        textEdit: { range: { start: { line: 0, character: 1 }, end: { line: 0, character: 1 } }, newText: "include <$0>" },
      }] } }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetch);
    const provider = captured.get("cpp")!;
    expect(provider.triggerCharacters).toEqual(expect.arrayContaining(["#", "<", '"', "/", ">", ":"]));
    const token = new Monaco.CancellationTokenSource();
    const result = await provider.provideCompletionItems(model, { lineNumber: 1, column: 2 }, { triggerKind: Monaco.languages.CompletionTriggerKind.TriggerCharacter, triggerCharacter: "#" }, token.token);
    token.dispose();
    expect(result!.suggestions[0]).toMatchObject({ insertText: "include <$0>", insertTextRules: Monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
      range: { startLineNumber: 1, endLineNumber: 1, startColumn: 2, endColumn: 2 } });
    model.pushEditOperations([], [{ range: result!.suggestions[0].range as Monaco.IRange, text: "include <iostream>" }], () => null);
    expect(model.getValue()).toBe("#include <iostream>");
    expect(header.getValue()).toBe("int unsaved_header_function();");
  });
});
