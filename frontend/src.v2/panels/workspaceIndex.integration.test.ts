/* @vitest-environment jsdom */
import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { IBulkEditService, type BulkEditService } from "monaco-editor/editor/browser/services/bulkEditService.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { typescript as ts } from "monaco-editor/languages/features/typescript/lib/typescriptServices.js";
import type { IExtraLibs } from "monaco-editor/languages/features/typescript/register.js";
import type { WorkspaceProjectIndex, WorkspaceProjectIndexFile } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { editorModelUri } from "./monacoLanguageServices";
import { WorkspaceModelIndex } from "./workspaceModelIndex";
import { WorkspaceTypeScriptService } from "./workspaceTypeScriptService";
import { WORKSPACE_TYPESCRIPT_METADATA_URI, type WorkspaceTypeScriptMetadata } from "./workspaceTypeScriptContract";

const publication = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
  return { version: 0, extraLibs: {} as IExtraLibs, publish: vi.fn() };
});

// Keep the index, compiler, models, native bulk-edit service and store real.
// Capture only the transport of published index files into worker extra libs.
vi.mock("./monacoLanguageServices", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./monacoLanguageServices")>();
  const { WORKSPACE_TYPESCRIPT_METADATA_URI: metadataUri } = await import("./workspaceTypeScriptContract");
  publication.publish.mockImplementation((metadata: WorkspaceTypeScriptMetadata, files: Array<{ filePath: string; content: string }>) => {
    const version = ++publication.version;
    publication.extraLibs = Object.fromEntries(files.map((file) => [file.filePath, { content: file.content, version }]));
    publication.extraLibs[metadataUri] = { content: JSON.stringify(metadata), version };
  });
  return { ...actual, setWorkspaceTypeScriptFiles: publication.publish };
});

const ROOT = "/workspace/integration";
const owners: WorkspaceModelIndex[] = [];
const services: WorkspaceTypeScriptService[] = [];
const subscriptions: Array<() => void> = [];
const actualAdopt = useAppStore.getState().adoptEditorModelChanges;
const uri = (path: string) => monaco.Uri.parse(editorModelUri(path, ROOT));
const model = (path: string) => monaco.editor.getModel(uri(path))!;
const tab = (path: string) => useAppStore.getState().editorTabs.find((entry) => entry.path === path)!;
const metadata = () => JSON.parse(publication.extraLibs[WORKSPACE_TYPESCRIPT_METADATA_URI].content) as WorkspaceTypeScriptMetadata;

beforeAll(() => {
  monaco.languages.register({ id: "typescript", extensions: [".ts", ".tsx", ".mts", ".cts"] });
  monaco.languages.register({ id: "javascript", extensions: [".js", ".jsx", ".mjs", ".cjs"] });
});
beforeEach(() => {
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  publication.version = 0;
  publication.extraLibs = {};
  publication.publish.mockClear();
  useAppStore.setState({
    workingDirectory: ROOT, appMode: "code", conversationId: null,
    editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [], activeEditorOpenRequestId: null,
    panelSlots: [{ id: "main-editor", kind: "editor", focused: true }],
    adoptEditorModelChanges: actualAdopt,
  });
});
afterEach(async () => {
  for (const unsubscribe of subscriptions.splice(0)) unsubscribe();
  for (const owner of owners.splice(0).reverse()) owner.dispose();
  for (const service of services.splice(0)) await service.updateExtraLibs(publication.extraLibs);
  await Promise.resolve();
  for (const current of monaco.editor.getModels()) current.dispose();
  useAppStore.setState({ adoptEditorModelChanges: actualAdopt });
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  vi.restoreAllMocks();
});

function file(path: string, content: string, kind: WorkspaceProjectIndexFile["kind"] = "source"): WorkspaceProjectIndexFile {
  return { path, content, kind, content_hash: createHash("sha256").update(content).digest("hex"), size_bytes: Buffer.byteLength(content, "utf8") };
}
function snapshot(files: WorkspaceProjectIndexFile[]): WorkspaceProjectIndex {
  return { workspace_root: ROOT, files, complete: true, issues: [] };
}
const apply = (owner: WorkspaceModelIndex, files: WorkspaceProjectIndexFile[]) => owner.applySnapshot(snapshot(files), true, new AbortController().signal);
function workerService(): WorkspaceTypeScriptService {
  const service = new WorkspaceTypeScriptService({
    getMirrorModels: () => monaco.editor.getModels().map((current) => ({
      uri: current.uri, version: current.getVersionId(), getValue: () => current.getValue(undefined, true),
    })),
  }, {
    compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.NodeJs, allowJs: true, jsx: ts.JsxEmit.Preserve },
    extraLibs: publication.extraLibs,
  });
  services.push(service);
  return service;
}

describe("workspace index, compiler and native editor integration", () => {
  it("navigates and renames hidden sources, preserves multi-file undo, and observes subsequent disk refresh and deletion", async () => {
    const greet = file("src/greet.ts", 'export function greet(name: string) { return "Hello " + name; }\n');
    const main = file("src/main.ts", [
      'import { greet as welcome } from "@/greet";',
      'import { decorate } from "tiny";',
      'export const result: string = decorate(welcome("MiniCode"));',
    ].join("\n"));
    const supporting = [
      file("tsconfig.json", '{"extends":"./configs/base.json","include":["src/**/*"]}', "config"),
      file("configs/base.json", '{"compilerOptions":{"strict":true,"baseUrl":"..","paths":{"@/*":["src/*"]}}}', "config"),
      file("node_modules/tiny/package.json", '{"name":"tiny","types":"index.d.ts"}', "package"),
      file("node_modules/tiny/index.d.ts", "export declare function decorate(value: string): string;", "declaration"),
    ];
    useAppStore.getState().openEditorTab(main.path);
    useAppStore.getState().markTabLoaded(main.path, main.content, null, main.content_hash, { sizeBytes: main.size_bytes });
    const activeId = tab(main.path).id;
    const owner = new WorkspaceModelIndex(monaco, ROOT);
    owners.push(owner);
    await apply(owner, [greet, main, ...supporting]);
    const greetModel = model(greet.path);
    const mainModel = model(main.path);
    const cleanVersions = [greetModel, mainModel].map((current) => current.getAlternativeVersionId());
    expect(useAppStore.getState().editorTabs.map((entry) => entry.path)).toEqual([main.path]);
    expect(monaco.editor.getModel(uri("node_modules/tiny/index.d.ts"))).toBeNull();
    expect(new Set(metadata().sourceFileNames)).toEqual(new Set([uri(greet.path).toString(), uri(main.path).toString()]));

    const service = workerService();
    const mainCall = main.content.indexOf('welcome("') + 2;
    expect(await service.getCompilerOptionsDiagnostics(uri(main.path).toString())).toEqual([]);
    expect(await service.getSemanticDiagnostics(uri(main.path).toString())).toEqual([]);
    const definitions = await service.getDefinitionAtPosition(uri(main.path).toString(), mainCall);
    expect(definitions).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri(greet.path).toString() })]));
    const dependencyDefinitions = await service.getDefinitionAtPosition(uri(main.path).toString(), main.content.indexOf("decorate(welcome") + 2);
    expect(dependencyDefinitions).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri("node_modules/tiny/index.d.ts").toString() })]));
    const greetPosition = greet.content.indexOf("greet") + 2;
    const references = await service.getReferencesAtPosition(uri(greet.path).toString(), greetPosition);
    expect(new Set(references.map((entry) => entry.fileName))).toEqual(new Set([uri(greet.path).toString(), uri(main.path).toString()]));

    const renameLocations = await service.findRenameLocations(uri(greet.path).toString(), greetPosition, false, false, true);
    expect(new Set(renameLocations.map((entry) => entry.fileName))).toEqual(new Set([uri(greet.path).toString(), uri(main.path).toString()]));
    const edits: monaco.languages.IWorkspaceTextEdit[] = renameLocations.map((location) => {
      const target = monaco.editor.getModel(monaco.Uri.parse(location.fileName))!;
      const start = target.getPositionAt(location.textSpan.start);
      const end = target.getPositionAt(location.textSpan.start + location.textSpan.length);
      return {
        resource: target.uri, versionId: target.getVersionId(),
        textEdit: { range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column), text: `${location.prefixText ?? ""}hello${location.suffixText ?? ""}` },
      };
    });
    const adoptedStates: Array<Array<{ path: string; content: string; original: string }>> = [];
    subscriptions.push(useAppStore.subscribe((state, previous) => {
      if (state.editorTabs !== previous.editorTabs) adoptedStates.push(state.editorTabs.map(({ path, content, original }) => ({ path, content, original })));
    }));
    const network = vi.spyOn(globalThis, "fetch");
    const nativeBulkEdit = StandaloneServices.get<BulkEditService>(IBulkEditService);
    expect(await nativeBulkEdit.apply({ edits }, { label: "Rename greet to hello" })).toMatchObject({ isApplied: true });
    await Promise.resolve();
    expect(adoptedStates).toHaveLength(1);
    expect(new Set(adoptedStates[0].map((change) => change.path))).toEqual(new Set([greet.path, main.path]));
    expect(greetModel.getValue()).toContain("function hello(");
    expect(mainModel.getValue()).toContain("hello as welcome");
    expect(mainModel.getValue()).toContain('welcome("MiniCode")');
    expect(tab(main.path)).toMatchObject({ id: activeId, original: main.content, contentHash: main.content_hash, content: mainModel.getValue() });
    expect(tab(greet.path)).toMatchObject({ original: greet.content, contentHash: greet.content_hash, content: greetModel.getValue(), loading: false });
    expect(useAppStore.getState().activeTabPath).toBe(main.path);
    expect(useAppStore.getState().activeEditorPath).toBe(main.path);
    expect(network).not.toHaveBeenCalled();
    expect(publication.extraLibs[uri(greet.path).toString()].content).toBe(greet.content);
    expect(publication.extraLibs[uri(main.path).toString()].content).toBe(main.content);

    const renamed = [greetModel.getValue(), mainModel.getValue()];
    await greetModel.undo();
    await Promise.resolve();
    expect([greetModel.getValue(), mainModel.getValue()]).toEqual([greet.content, main.content]);
    expect([greetModel, mainModel].map((current) => current.getAlternativeVersionId())).toEqual(cleanVersions);
    expect(useAppStore.getState().editorTabs.every((entry) => entry.content === entry.original)).toBe(true);
    await mainModel.redo();
    await Promise.resolve();
    expect([greetModel.getValue(), mainModel.getValue()]).toEqual(renamed);
    expect(tab(main.path)).toMatchObject({ id: activeId, original: main.content, contentHash: main.content_hash });
    expect(tab(greet.path)).toMatchObject({ original: greet.content, contentHash: greet.content_hash });
    const liveDefinitions = await service.getDefinitionAtPosition(uri(main.path).toString(), mainModel.getValue().indexOf('welcome("') + 2);
    const renamedDefinition = liveDefinitions!.find((entry) => entry.fileName === uri(greet.path).toString())!;
    expect(greetModel.getValue().slice(renamedDefinition.textSpan.start, renamedDefinition.textSpan.start + renamedDefinition.textSpan.length)).toBe("hello");

    const savedGreet = file(greet.path, greetModel.getValue());
    const savedMain = file(main.path, mainModel.getValue());
    const versionsBeforeAck = [greetModel, mainModel].map((current) => current.getAlternativeVersionId());
    for (const saved of [savedGreet, savedMain]) useAppStore.getState().markTabSaved(saved.path, saved.content, saved.content_hash, saved.size_bytes, ROOT);
    await apply(owner, [savedGreet, savedMain, ...supporting]);
    await service.updateExtraLibs(publication.extraLibs);
    expect([greetModel, mainModel].map((current) => current.getAlternativeVersionId())).toEqual(versionsBeforeAck);
    expect(tab(greet.path)).toMatchObject({ original: savedGreet.content, contentHash: savedGreet.content_hash });
    expect(publication.extraLibs[uri(greet.path).toString()].content).toBe(savedGreet.content);
    useAppStore.getState().closeEditorTab(greet.path);
    expect(model(greet.path)).toBe(greetModel);
    expect(useAppStore.getState().editorTabs.map((entry) => entry.path)).toEqual([main.path]);

    const changedGreet = file(greet.path, "export function hello(name: string) { return name.length; }\n");
    await apply(owner, [changedGreet, savedMain, ...supporting]);
    await service.updateExtraLibs(publication.extraLibs);
    expect(greetModel.getValue()).toBe(changedGreet.content);
    expect(useAppStore.getState().editorTabs.map((entry) => entry.path)).toEqual([main.path]);
    expect((await service.getSemanticDiagnostics(uri(main.path).toString())).some((entry) => entry.code === 2345)).toBe(true);
    expect((await service.getReferencesAtPosition(uri(greet.path).toString(), changedGreet.content.indexOf("hello") + 2)).some((entry) => entry.fileName === uri(main.path).toString())).toBe(true);

    owner.noteFileChanges([{ path: greet.path, event: "delete" }]);
    await apply(owner, [savedMain, ...supporting]);
    await service.updateExtraLibs(publication.extraLibs);
    expect(greetModel.isDisposed()).toBe(true);
    expect(metadata().sourceFileNames).toEqual([uri(main.path).toString()]);
    expect(publication.extraLibs[uri(greet.path).toString()]).toBeUndefined();
    expect((await service.getSemanticDiagnostics(uri(main.path).toString())).some((entry) => entry.code === 2307)).toBe(true);
    expect((await service.getDefinitionAtPosition(uri(main.path).toString(), mainModel.getValue().indexOf('welcome("') + 2))?.some((entry) => entry.fileName === uri(greet.path).toString())).not.toBe(true);
    const currentReferences = await service.getReferencesAtPosition(uri(main.path).toString(), mainModel.getValue().indexOf("welcome") + 2);
    expect(currentReferences.some((entry) => entry.fileName === uri(main.path).toString())).toBe(true);
    expect(currentReferences.some((entry) => entry.fileName === uri(greet.path).toString())).toBe(false);
    expect(useAppStore.getState().activeTabPath).toBe(main.path);
    expect(network).not.toHaveBeenCalled();
  });
});
