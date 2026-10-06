/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as Monaco from "monaco-editor/editor/editor.api.js";
import { registerEditorLanguageServices } from "./editorLanguageClient";
import { useAppStore } from "../stores";
import { editorModelUri } from "./monacoLanguageServices";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", { configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
});

const captured = new Map<string, Monaco.languages.CompletionItemProvider>();
let registration: Monaco.IDisposable;
beforeEach(() => {
  useAppStore.setState({ workingDirectory: "/cpp-editor-test" });
  const real = Monaco.languages.registerCompletionItemProvider;
  vi.spyOn(Monaco.languages, "registerCompletionItemProvider").mockImplementation((selector, provider) => {
    if (typeof selector === "string") captured.set(selector, provider);
    return real(selector, provider);
  });
  registration = registerEditorLanguageServices(Monaco);
});
afterEach(() => {
  registration.dispose();
  Monaco.editor.getModels().forEach((model) => model.dispose());
  captured.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("native C/C++ Monaco provider bridge", () => {
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
