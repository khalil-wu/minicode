/* @vitest-environment jsdom */
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { EditorExtensionsRegistry } from "monaco-editor/editor/browser/editorExtensions.js";
import { CommandsRegistry } from "monaco-editor/platform/commands/common/commands.js";
import { TypeScriptWorker } from "monaco-editor/languages/features/typescript/tsWorker.js";
import { loadMiniCodeLanguageServices, editorModelUri, registerMiniCodeEditorOpener } from "./monacoLanguageServices";
import { miniCodeCodeEditingOptions } from "./monacoEditorFeatures";
import { WorkspaceHTMLWorker } from "./workspaceHtmlService";
import { useAppStore } from "../stores";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { ILanguageFeaturesService } from "monaco-editor/editor/common/services/languageFeatures.js";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
  // JSDOM has text and DOM events, but no font measurement or canvas drawing.
  HTMLCanvasElement.prototype.getContext = (() => ({
    font: "", measureText: (text: string) => ({ width: text.length * 8 }),
    clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
  })) as unknown as typeof HTMLCanvasElement.prototype.getContext;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
});
vi.mock("monaco-editor/editor/editor.worker?worker", () => ({ default: class {} }));
vi.mock("./workspaceTypeScriptWorker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/languages/features/css/css.worker?worker", () => ({ default: class {} }));
vi.mock("./workspaceHtmlWorker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/languages/features/json/json.worker?worker", () => ({ default: class {} }));

const disposables: Array<{ dispose(): void }> = [];
beforeAll(async () => {
  // The production entry loads both language providers and editing features.
  await loadMiniCodeLanguageServices();
});
afterEach(() => {
  for (const entry of disposables.splice(0).reverse()) entry.dispose();
  for (const model of monaco.editor.getModels()) model.dispose();
  document.body.replaceChildren();
});

function textModel(content: string, language: string, path: string) {
  return monaco.editor.createModel(content, language, monaco.Uri.parse(editorModelUri(path, "/completion-test")));
}
function editorFor(model: monaco.editor.ITextModel) {
  const host = document.createElement("div");
  document.body.append(host);
  const editor = monaco.editor.create(host, {
    ...miniCodeCodeEditingOptions,
    model, dimension: { width: 800, height: 420 },
    automaticLayout: false, minimap: { enabled: false },
    wordBasedSuggestions: "off",
  });
  disposables.push(editor);
  editor.focus();
  return editor;
}
function mirrorContext() {
  return { getMirrorModels: () => monaco.editor.getModels().map((model) => ({
    uri: model.uri, version: model.getVersionId(), getValue: () => model.getValue(),
  })) };
}
function typeScriptWorker() {
  const worker = new TypeScriptWorker(mirrorContext(), {
    compilerOptions: { allowJs: true, target: 99 }, extraLibs: {}, inlayHintsOptions: {},
  });
  disposables.push({ dispose: () => worker._languageService.dispose() });
  return worker;
}

describe("production Monaco interaction and language service integration", () => {
  it("registers the actual controllers and every advertised editor command", () => {
    const contributions = EditorExtensionsRegistry.getEditorContributions().map((entry) => entry.id);
    expect(contributions).toEqual(expect.arrayContaining([
      "editor.contrib.suggestController", "snippetController2", "editor.controller.parameterHints",
      "editor.contrib.findController", "editor.contrib.folding", "editor.contrib.contentHover", "editor.contrib.renameController",
    ]));
    for (const id of [
      "editor.action.triggerSuggest", "actions.find", "editor.action.startFindReplaceAction", "editor.action.gotoLine",
      "editor.action.inlineSuggest.trigger", "editor.action.inlineSuggest.acceptNextWord", "editor.action.inlineSuggest.acceptNextLine",
      "editor.foldAll", "editor.unfoldAll", "editor.action.formatDocument", "editor.action.revealDefinition",
      "editor.action.referenceSearch.trigger", "editor.action.rename", "editor.action.triggerParameterHints",
    ]) expect(CommandsRegistry.getCommand(id)).toBeDefined();
    for (const id of ["acceptSelectedSuggestion", "insertBestCompletion", "jumpToNextSnippetPlaceholder"]) {
      expect(EditorExtensionsRegistry.getEditorCommand(id)).toBeDefined();
    }
  });

  it.each(["typescript", "javascript"])("automatically suggests a real %s member, accepts it and preserves native undo", async (language) => {
    const testLanguage = `integration-${language}`;
    monaco.languages.register({ id: testLanguage });
    const model = textModel('const greeting = "MiniCode";\ngreeting', testLanguage, language === "typescript" ? "main.ts" : "main.js");
    const { SuggestAdapter } = await import("monaco-editor/languages/features/typescript/tsMode.js");
    const worker = typeScriptWorker();
    // The production adapter receives the real compiler without RPC in JSDOM.
    // Completion results and native editor editing are not mocked.
    disposables.push(monaco.languages.registerCompletionItemProvider(testLanguage, new SuggestAdapter(async () => worker)));
    const editor = editorFor(model);
    editor.setPosition(model.getPositionAt(model.getValueLength()));
    editor.trigger("keyboard", "type", { text: ".toU" });
    await waitFor(() => expect(document.querySelector(".suggest-widget .monaco-list-row")?.textContent).toContain("toUpperCase"));
    editor.trigger("keyboard", "acceptSelectedSuggestion", {});
    expect(model.getValue()).toBe('const greeting = "MiniCode";\ngreeting.toUpperCase');
    await model.undo();
    expect(model.getValue()).toBe('const greeting = "MiniCode";\ngreeting.toU');
    editor.trigger("keyboard", "editor.action.triggerSuggest", {});
    await waitFor(() => expect(document.querySelector(".suggest-widget .monaco-list-row")?.textContent).toContain("toUpperCase"));
  });

  it("runs modern definition commands through the native editor command route", async () => {
    const testLanguage = "integration-definition";
    monaco.languages.register({ id: testLanguage });
    const model = textModel('function greet(name: string) { return name; }\ngreet("MiniCode");', testLanguage, "definition.ts");
    const { DefinitionAdapter, LibFiles } = await import("monaco-editor/languages/features/typescript/tsMode.js");
    const worker = typeScriptWorker();
    const workerFactory = async () => worker;
    disposables.push(monaco.languages.registerDefinitionProvider(testLanguage, new DefinitionAdapter(new LibFiles(workerFactory), workerFactory)));
    const editor = editorFor(model);
    editor.setPosition(model.getPositionAt(model.getValue().lastIndexOf("greet") + 2));
    expect(editor.getAction("editor.action.revealDefinition")).toBeNull();
    editor.trigger("minicode.editor-action", "editor.action.revealDefinition", {});
    await waitFor(() => expect(editor.getPosition()).toEqual({ lineNumber: 1, column: 10 }));
  });

  it("moves native snippet placeholders through the command that Tab executes", () => {
    const model = textModel("", "plaintext", "snippet.txt");
    const editor = editorFor(model);
    const snippets = editor.getContribution<{ dispose(): void; insert(template: string): void }>("snippetController2")!;
    snippets.insert("function ${1:name}(${2:value}) {\n\t$0\n}");
    expect(model.getValueInRange(editor.getSelection()!)).toBe("name");
    editor.trigger("keyboard", "jumpToNextSnippetPlaceholder", {});
    expect(model.getValueInRange(editor.getSelection()!)).toBe("value");
    editor.trigger("keyboard", "type", { text: "message" });
    expect(model.getValue()).toContain("function name(message)");
  });

  it("returns type hover and the actual parameter signature from the unsaved model", async () => {
    const model = textModel('function greet(name: string, times: number) { return name.repeat(times); }\ngreet("MiniCode", ', "plaintext", "signature.ts");
    const worker = typeScriptWorker();
    const { QuickInfoAdapter, SignatureHelpAdapter } = await import("monaco-editor/languages/features/typescript/tsMode.js");
    const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };
    const hover = await new QuickInfoAdapter(async () => worker).provideHover(model, model.getPositionAt(model.getValue().lastIndexOf("greet") + 2), token);
    expect(hover.contents.map((entry: { value: string }) => entry.value).join("\n")).toContain("greet(name: string, times: number): string");
    const hints = await new SignatureHelpAdapter(async () => worker).provideSignatureHelp(model, model.getPositionAt(model.getValueLength()), token, { triggerKind: monaco.languages.SignatureHelpTriggerKind.Invoke, isRetrigger: false });
    expect(hints.value.activeParameter).toBe(1);
    expect(hints.value.signatures[0].parameters.map((entry: { label: string }) => entry.label)).toEqual(["name: string", "times: number"]);
    hints.dispose();
  });

  it.each([
    { language: "html", source: "<di", expected: "div" },
    { language: "css", source: ".card { dis", expected: "display" },
    { language: "scss", source: ".card { dis", expected: "display" },
    { language: "less", source: ".card { dis", expected: "display" },
    { language: "json", source: '{"mode": "', expected: '"code"' },
  ])("uses the actual $language worker and completion adapter", async ({ language, source, expected }) => {
    const model = textModel(source, "plaintext", `language.${language}`);
    let worker;
    let Adapter;
    if (language === "html") {
      const { HTMLWorker } = await import("monaco-editor/languages/features/html/htmlWorker.js");
      ({ CompletionAdapter: Adapter } = await import("monaco-editor/languages/features/html/htmlMode.js"));
      worker = new HTMLWorker(mirrorContext(), { languageId: language, languageSettings: {} });
    } else if (language === "json") {
      const { JSONWorker } = await import("monaco-editor/languages/features/json/jsonWorker.js");
      ({ CompletionAdapter: Adapter } = await import("monaco-editor/languages/features/json/jsonMode.js"));
      worker = new JSONWorker(mirrorContext(), { languageId: language, enableSchemaRequest: false, languageSettings: {
        schemas: [{ uri: "inmemory://integration.schema.json", fileMatch: [model.uri.toString()], schema: { type: "object", properties: { mode: { enum: ["code", "chat"] } } } }],
      } });
    } else {
      const { CSSWorker } = await import("monaco-editor/languages/features/css/cssWorker.js");
      ({ CompletionAdapter: Adapter } = await import("monaco-editor/languages/features/css/cssMode.js"));
      worker = new CSSWorker(mirrorContext(), { languageId: language, options: {} });
    }
    const result = await new Adapter(async () => worker).provideCompletionItems(model, model.getPositionAt(model.getValueLength()), { triggerKind: monaco.languages.CompletionTriggerKind.Invoke }, { isCancellationRequested: false });
    expect(result.suggestions.map((item: monaco.languages.CompletionItem) => item.label)).toContain(expected);
  });

  it.each(["C:/project", "/project", "//server/share/project"])("resolves HTML asset links from their owning document in %s", async (workspaceRoot) => {
    const model = monaco.editor.createModel([
      '<script src="js/game.js"></script>',
      '<script src="./js/game.js"></script>',
      '<script src="../shared/game.js"></script>',
      '<a href="https://example.com/js/game.js?mode=play#ready">Remote</a>',
      '<a href="file:///outside/game.js">Absolute file</a>',
    ].join("\n"), "plaintext", monaco.Uri.parse(editorModelUri("site/index.html", workspaceRoot)));
    const worker = new WorkspaceHTMLWorker(mirrorContext(), { languageId: "html", languageSettings: {} });
    const { DocumentLinkAdapter } = await import("monaco-editor/languages/features/html/htmlMode.js");
    const result = await new DocumentLinkAdapter(async () => worker).provideLinks(model, { isCancellationRequested: false });
    const targetUri = (path: string) => monaco.Uri.parse(editorModelUri(path, workspaceRoot)).toString();

    expect(result.links.map((link: monaco.languages.ILink) => link.url)).toEqual([
      targetUri("site/js/game.js"),
      targetUri("site/js/game.js"),
      targetUri("shared/game.js"),
      "https://example.com/js/game.js?mode=play#ready",
      "file:///outside/game.js",
    ]);
  });

  it.each(["C:/project", "/project", "//server/share/project"])("honors an HTML base href while resolving links in %s", async (workspaceRoot) => {
    const model = monaco.editor.createModel([
      '<base href="../assets/">',
      '<script src="js/game.js"></script>',
      '<a href="./info.html">Info</a>',
    ].join("\n"), "plaintext", monaco.Uri.parse(editorModelUri("site/index.html", workspaceRoot)));
    const worker = new WorkspaceHTMLWorker(mirrorContext(), { languageId: "html", languageSettings: {} });
    const { DocumentLinkAdapter } = await import("monaco-editor/languages/features/html/htmlMode.js");
    const result = await new DocumentLinkAdapter(async () => worker).provideLinks(model, { isCancellationRequested: false });
    const targetUri = (path: string) => monaco.Uri.parse(editorModelUri(path, workspaceRoot)).toString();

    expect(result.links.map((link: monaco.languages.ILink) => link.url)).toEqual([
      targetUri("assets/js/game.js"),
      targetUri("assets/info.html"),
    ]);
  });

  it("opens a real HTML worker link through Monaco as the correct workspace-relative file", async () => {
    const previousState = useAppStore.getState();
    disposables.push({ dispose: () => useAppStore.setState(previousState, true) });
    useAppStore.setState({ workingDirectory: "C:/project", editorOpenRequests: [], activeEditorOpenRequestId: null, activeEditorPath: null });
    const model = monaco.editor.createModel('<script src="js/game.js"></script>', "plaintext", monaco.Uri.parse(editorModelUri("site/index.html", "C:/project")));
    const editor = editorFor(model);
    const worker = new WorkspaceHTMLWorker(mirrorContext(), { languageId: "html", languageSettings: {} });
    const { DocumentLinkAdapter } = await import("monaco-editor/languages/features/html/htmlMode.js");
    const result = await new DocumentLinkAdapter(async () => worker).provideLinks(model, { isCancellationRequested: false });
    disposables.push(registerMiniCodeEditorOpener(monaco, (path, label, target) => useAppStore.getState().openEditorFile(path, label, target)));
    const { StandaloneServices } = await import("monaco-editor/editor/standalone/browser/standaloneServices.js");
    const { ICodeEditorService } = await import("monaco-editor/editor/browser/services/codeEditorService.js");
    const service = StandaloneServices.get<{
      openCodeEditor(input: { resource: monaco.Uri }, source: monaco.editor.ICodeEditor): Promise<unknown>;
    }>(ICodeEditorService);

    await service.openCodeEditor({ resource: monaco.Uri.parse(result.links[0].url) }, editor);

    expect(useAppStore.getState().editorOpenRequests).toEqual([
      expect.objectContaining({ path: "site/js/game.js", exact: true }),
    ]);
    expect(useAppStore.getState().activeEditorPath).toBe("site/js/game.js");
  });
});

it("uses the real inline provider with cancellation and keeps stale responses out of the model", async () => {
  const previousFetch = globalThis.fetch;
  const previous = useAppStore.getState().workbenchPreferences;
  useAppStore.setState({ workingDirectory: "/completion-test", workbenchPreferences: { ...previous, aiEnabled: true } });
  const model = textModel("const value = ", "typescript", "prediction.ts");
  const providers = StandaloneServices.get<{ inlineCompletionsProvider: { ordered(model: monaco.editor.ITextModel): monaco.languages.InlineCompletionsProvider[] } }>(ILanguageFeaturesService).inlineCompletionsProvider.ordered(model);
  const provider = providers.find((entry) => entry.displayName === "MiniCode")!;
  const token = new monaco.CancellationTokenSource();
  let resolve!: (response: Response) => void;
  globalThis.fetch = vi.fn(() => new Promise<Response>((done) => { resolve = done; }));
  try {
    const pending = provider.provideInlineCompletions(model, new monaco.Position(1, 15), { triggerKind: 0 } as monaco.languages.InlineCompletionContext, token.token);
    model.setValue("const value = 2");
    resolve(new Response(JSON.stringify({ text: "1;", usage: { input_tokens: 5, output_tokens: 2 }, model: "test" }), { status: 200 }));
    expect((await pending)?.items).toEqual([]);
    expect(model.getValue()).toBe("const value = 2");
  } finally { token.dispose(); globalThis.fetch = previousFetch; useAppStore.setState({ workbenchPreferences: previous }); }
});

it("keeps cancellation attached after response headers while the prediction body is pending", async () => {
  const { generateEditorCode } = await import("./editorInlineCompletion");
  const previousFetch = globalThis.fetch;
  const controller = new AbortController();
  let bodyStarted!: () => void;
  const headers = new Promise<void>((resolve) => { bodyStarted = resolve; });
  globalThis.fetch = vi.fn(async (_url, init) => ({ ok: true, json: () => new Promise((_resolve, reject) => {
    init!.signal!.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true });
    bodyStarted();
  }) } as Response));
  try {
    const request = generateEditorCode("/completion-test", { path: "main.ts", prefix: "const a = ", suffix: "" }, controller.signal);
    const cancelled = expect(request).rejects.toMatchObject({ name: "AbortError" });
    await headers;
    controller.abort();
    await cancelled;
  } finally { globalThis.fetch = previousFetch; }
});
