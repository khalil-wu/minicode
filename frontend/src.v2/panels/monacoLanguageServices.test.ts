/* @vitest-environment jsdom */
import { describe, expect, it, vi } from "vitest";
import * as Monaco from "monaco-editor/editor/editor.api.js";
import { URI } from "monaco-editor/base/common/uri.js";
import { TypeScriptWorker as NativeTypeScriptWorker } from "monaco-editor/languages/features/typescript/tsWorker.js";
import { normalizeWorkspacePath } from "../lib/workspace-path";
import {
  configureMiniCodeMonacoWorkers,
  editorModelUri,
  loadMiniCodeLanguageServices,
  registerMiniCodeEditorOpener,
  setWorkspaceTypeScriptFiles,
  syncWorkspaceTypeScriptModels,
} from "./monacoLanguageServices";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
});
const onLanguage = vi.spyOn(Monaco.languages, "onLanguage");
vi.mock("monaco-editor/editor/editor.worker?worker", () => ({ default: class { kind = "editor"; } }));
vi.mock("./workspaceTypeScriptWorker?worker", () => ({ default: class { kind = "typescript"; } }));
vi.mock("monaco-editor/languages/features/css/css.worker?worker", () => ({ default: class { kind = "css"; } }));
vi.mock("./workspaceHtmlWorker?worker", () => ({ default: class { kind = "html"; } }));
vi.mock("monaco-editor/languages/features/json/json.worker?worker", () => ({ default: class { kind = "json"; } }));

// Transform Monaco's native provider graph during suite loading. The tests
// below still activate the real services with no source models present.
await import("monaco-editor/languages/features/typescript/tsMode.js");

describe("MiniCode Monaco language services", () => {
  it("awaits both workers' current source snapshot before exposing project queries", async () => {
    await loadMiniCodeLanguageServices();
    const typescript = await import("monaco-editor/languages/features/typescript/register.js");
    const resource = Monaco.Uri.parse(editorModelUri("src/main.ts", "/current"));
    setWorkspaceTypeScriptFiles({ workspaceRoot: editorModelUri(".", "/current"), sourceFileNames: [resource.toString()], readOnlyFileNames: [], caseSensitive: true },
      [{ filePath: resource.toString(), content: "export const current = 1;" }]);
    let finish!: () => void;
    const workers = [
      { updateExtraLibs: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })), getConfigurationFileRequests: vi.fn(async () => []) },
      { updateExtraLibs: vi.fn(async () => {}), getConfigurationFileRequests: vi.fn(async () => []) },
    ];
    const getters = [
      vi.spyOn(typescript, "getTypeScriptWorker").mockResolvedValue(async () => workers[0] as never),
      vi.spyOn(typescript, "getJavaScriptWorker").mockResolvedValue(async () => workers[1] as never),
    ];
    const pending = syncWorkspaceTypeScriptModels([]);
    let completed = false;
    void pending.then(() => { completed = true; });
    await vi.waitFor(() => expect(workers[0].updateExtraLibs).toHaveBeenCalledOnce());
    expect(workers[1].updateExtraLibs).toHaveBeenCalledWith(expect.objectContaining({ [resource.toString()]: expect.objectContaining({ content: "export const current = 1;" }) }));
    expect(completed).toBe(false);
    finish();
    await (await pending).configurationRequests();
    expect(workers.every((worker) => worker.getConfigurationFileRequests.mock.calls.length === 1)).toBe(true);
    getters.forEach((getter) => getter.mockRestore());
  });

  it("loads real feature registrations once and keeps their standard providers and libraries", async () => {
    const firstLoad = loadMiniCodeLanguageServices();
    expect(loadMiniCodeLanguageServices()).toBe(firstLoad);
    await firstLoad;
    const typescript = await import("monaco-editor/languages/features/typescript/register.js");
    const css = await import("monaco-editor/languages/features/css/register.js");
    const html = await import("monaco-editor/languages/features/html/register.js");
    const json = await import("monaco-editor/languages/features/json/register.js");
    const enabledLanguages = onLanguage.mock.calls.map(([language]) => language);
    expect(enabledLanguages).toEqual(expect.arrayContaining(["typescript", "javascript", "css", "html", "json"]));
    expect(enabledLanguages).not.toContain("python");
    for (const defaults of [typescript.typescriptDefaults, typescript.javascriptDefaults]) {
      expect(defaults.getEagerModelSync()).toBe(true);
      expect(defaults.getCompilerOptions()).toMatchObject({
        target: typescript.ScriptTarget.ESNext, module: typescript.ModuleKind.ESNext,
        moduleResolution: typescript.ModuleResolutionKind.NodeJs, jsx: typescript.JsxEmit.Preserve,
      });
      expect(defaults.getCompilerOptions().noLib).not.toBe(true);
      expect(defaults.modeConfiguration).toMatchObject({ completionItems: true, hovers: true, definitions: true, rename: true, diagnostics: true });
    }
    expect(css.cssDefaults.modeConfiguration).toMatchObject({ completionItems: true, hovers: true, diagnostics: true });
    expect(html.htmlDefaults.modeConfiguration).toMatchObject({ completionItems: true, hovers: true, diagnostics: true });
    expect(json.jsonDefaults.modeConfiguration).toMatchObject({ completionItems: true, hovers: true, diagnostics: true });
  });

  it("activates both native TS and JS workers before any source file is opened", async () => {
    await loadMiniCodeLanguageServices();
    expect(Monaco.editor.getModels()).toHaveLength(0);
    const { getTypeScriptWorker, getJavaScriptWorker } = await import("monaco-editor/languages/features/typescript/register.js");
    const workers = await Promise.all([getTypeScriptWorker(), getJavaScriptWorker()]);
    expect(workers.every((worker) => typeof worker === "function")).toBe(true);
  });

  it("routes each service to its own worker and keeps the default editor worker", () => {
    const previousEnvironment = globalThis.MonacoEnvironment;
    try {
      configureMiniCodeMonacoWorkers();
      const getWorker = globalThis.MonacoEnvironment!.getWorker!;
      for (const [label, kind] of [["typescript", "typescript"], ["javascript", "typescript"], ["css", "css"], ["scss", "css"], ["html", "html"], ["json", "json"], ["editorWorkerService", "editor"], ["python", "editor"]]) {
        expect(getWorker("workerMain.js", label)).toMatchObject({ kind });
      }
    } finally {
      globalThis.MonacoEnvironment = previousEnvironment;
    }
  });

  it("preserves real directories, extensions, and special file characters in model URIs", () => {
    const relative = editorModelUri("src/../lib/工具 #1?.tsx", "C:\\work\\Demo");
    const absolute = editorModelUri("C:/work/Demo/lib/工具 #1?.tsx", "C:/another-project");
    expect(relative).toBe(absolute);
    expect(URI.parse(relative).path).toBe("/C:/work/Demo/lib/工具 #1?.tsx");
    expect(URI.parse(editorModelUri("src/a.ts", "/repo")).path).toBe("/repo/src/a.ts");
    expect(URI.parse(editorModelUri("src/a.ts", "//server/share")).authority).toBe("server");
    expect(URI.parse(editorModelUri("src/a.ts", "//server/share")).path).toBe("/share/src/a.ts");
    expect(URI.parse(editorModelUri("100%.json", "/repo")).path).toBe("/repo/100%.json");
  });

  it("opens definition resources at their exact source position and disposes through Monaco", () => {
    const dispose = vi.fn();
    let opener!: Monaco.editor.ICodeEditorOpener;
    const monaco = { editor: { registerEditorOpener: vi.fn((handler) => { opener = handler; return { dispose }; }) } } as unknown as typeof Monaco;
    const openFile = vi.fn();
    const registration = registerMiniCodeEditorOpener(monaco, openFile);
    const resource = URI.parse(editorModelUri("src/helpers.ts", "C:/project"));
    expect(opener.openCodeEditor({} as Monaco.editor.ICodeEditor, resource, { startLineNumber: 8, startColumn: 4, endLineNumber: 8, endColumn: 10 })).toBe(true);
    expect(openFile).toHaveBeenCalledWith(normalizeWorkspacePath(resource.fsPath), "helpers.ts", { line: 8, column: 4, exact: true });
    opener.openCodeEditor({} as Monaco.editor.ICodeEditor, resource, { lineNumber: 12, column: 2 });
    expect(openFile).toHaveBeenLastCalledWith(normalizeWorkspacePath(resource.fsPath), "helpers.ts", { line: 12, column: 2, exact: true });
    expect(opener.openCodeEditor({} as Monaco.editor.ICodeEditor, URI.parse("inmemory://model/1"))).toBe(false);
    expect(openFile).toHaveBeenCalledTimes(2);
    registration.dispose();
    expect(dispose).toHaveBeenCalledOnce();
  });

  it.each(["/project", "C:/project"])("uses Monaco's actual TypeScript service for imports, definition, rename, hover, formatting, and standard-library completion in %s", async (workspaceRoot) => {
    await loadMiniCodeLanguageServices();
    const { typescriptDefaults } = await import("monaco-editor/languages/features/typescript/register.js");
    const helperUri = URI.parse(editorModelUri("src/greet.ts", workspaceRoot));
    const mainUri = URI.parse(editorModelUri("src/main.ts", workspaceRoot));
    const helper = 'export function greet(name: string) { return "Hello " + name; }';
    const main = 'import { greet } from "./greet";\nfunction makeMessage(){\nreturn greet("MiniCode");\n}\nconst message=makeMessage();\nmessage.toUpperCase();';
    const mirrorModels = [
      { uri: helperUri, version: 1, getValue: () => helper },
      { uri: mainUri, version: 1, getValue: () => main },
    ];
    const worker = new NativeTypeScriptWorker({ getMirrorModels: () => mirrorModels }, {
      compilerOptions: typescriptDefaults.getCompilerOptions(), extraLibs: {}, inlayHintsOptions: {},
    });
    expect(await worker.getSemanticDiagnostics(mainUri.toString())).toEqual([]);
    const definitions = await worker.getDefinitionAtPosition(mainUri.toString(), main.indexOf('greet("') + 2);
    expect(definitions).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: helperUri.toString() })]));
    const hover = await worker.getQuickInfoAtPosition(mainUri.toString(), main.indexOf('greet("') + 2);
    expect(hover.displayParts.map((part) => part.text).join("")).toContain("greet(name: string): string");
    const completions = await worker.getCompletionsAtPosition(mainUri.toString(), main.indexOf("toUpperCase"));
    expect(completions.entries.some((entry) => entry.name === "toUpperCase")).toBe(true);
    const renameLocations = await worker.findRenameLocations(helperUri.toString(), helper.indexOf("greet") + 2, false, false, true);
    expect(renameLocations.map((location) => location.fileName)).toEqual(expect.arrayContaining([helperUri.toString(), mainUri.toString()]));
    const formattingEdits = await worker.getFormattingEditsForDocument(mainUri.toString(), {
      ConvertTabsToSpaces: true, TabSize: 2, IndentSize: 2, IndentStyle: 2, NewLineCharacter: "\n",
      InsertSpaceBeforeAndAfterBinaryOperators: true,
      PlaceOpenBraceOnNewLineForFunctions: false,
    });
    const formatted = [...formattingEdits].sort((left, right) => right.span.start - left.span.start).reduce(
      (text, edit) => text.slice(0, edit.span.start) + edit.newText + text.slice(edit.span.start + edit.span.length),
      main,
    );
    expect(formatted).toBe('import { greet } from "./greet";\nfunction makeMessage() {\n  return greet("MiniCode");\n}\nconst message = makeMessage();\nmessage.toUpperCase();');
  });

  it("reports an unresolved dependency instead of pretending unopened workspace files were loaded", async () => {
    await loadMiniCodeLanguageServices();
    const { typescriptDefaults } = await import("monaco-editor/languages/features/typescript/register.js");
    const uri = URI.parse(editorModelUri("src/main.ts", "/project"));
    const worker = new NativeTypeScriptWorker({ getMirrorModels: () => [{ uri, version: 1, getValue: () => 'import { unseen } from "./not-opened";\nunseen();' }] }, {
      compilerOptions: typescriptDefaults.getCompilerOptions(), extraLibs: {}, inlayHintsOptions: {},
    });
    const diagnostics = await worker.getSemanticDiagnostics(uri.toString());
    expect(diagnostics.some((diagnostic) => diagnostic.code === 2307)).toBe(true);
  });
});
