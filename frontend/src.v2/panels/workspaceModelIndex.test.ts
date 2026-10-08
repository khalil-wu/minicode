/* @vitest-environment jsdom */
import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { IBulkEditService, type BulkEditService } from "monaco-editor/editor/browser/services/bulkEditService.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import type { WorkspaceProjectIndex, WorkspaceProjectIndexFile } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests, editorStateForWorkspace } from "../stores/shared-helpers";
import { editorModelUri } from "./monacoLanguageServices";
import { renameMonacoModel } from "./monacoModelRename";
import { WorkspaceModelIndex } from "./workspaceModelIndex";

const { publish } = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
  return { publish: vi.fn() };
});
vi.mock("./monacoLanguageServices", async (importOriginal) => ({
  ...await importOriginal<typeof import("./monacoLanguageServices")>(),
  setWorkspaceTypeScriptFiles: publish,
}));

const ROOT = "/workspace";
const owners: WorkspaceModelIndex[] = [];
const subscriptions: monaco.IDisposable[] = [];
const actualAdopt = useAppStore.getState().adoptEditorModelChanges;

beforeAll(() => {
  // Model-only tests register the real language IDs without starting workers.
  monaco.languages.register({ id: "typescript", extensions: [".ts", ".tsx", ".mts", ".cts"] });
  monaco.languages.register({ id: "javascript", extensions: [".js", ".jsx", ".mjs", ".cjs"] });
});
beforeEach(() => {
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  publish.mockClear();
  useAppStore.setState({
    workingDirectory: ROOT, appMode: "code", conversationId: null,
    editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [], activeEditorOpenRequestId: null,
    panelSlots: [{ id: "main-chat", kind: "chat", focused: true }, { id: "main-editor", kind: "editor" }],
    adoptEditorModelChanges: actualAdopt,
  });
});
afterEach(async () => {
  for (const subscription of subscriptions.splice(0)) subscription.dispose();
  for (const owner of owners.splice(0).reverse()) owner.dispose();
  await Promise.resolve();
  for (const model of monaco.editor.getModels()) model.dispose();
  useAppStore.setState({ adoptEditorModelChanges: actualAdopt });
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  vi.restoreAllMocks();
});

function file(path: string, content: string, kind: WorkspaceProjectIndexFile["kind"] = "source"): WorkspaceProjectIndexFile {
  return { path, content, kind, content_hash: createHash("sha256").update(content).digest("hex"), size_bytes: Buffer.byteLength(content, "utf8") };
}
function snapshot(files: WorkspaceProjectIndexFile[], workspaceRoot = ROOT): WorkspaceProjectIndex {
  return { workspace_root: workspaceRoot, files, complete: true, issues: [] };
}
function createOwner(workspaceRoot = ROOT) {
  const owner = new WorkspaceModelIndex(monaco, workspaceRoot);
  owners.push(owner);
  return owner;
}
function disposeOwner(owner: WorkspaceModelIndex) {
  owner.dispose();
  owners.splice(owners.indexOf(owner), 1);
}
const uri = (path: string, workspaceRoot = ROOT) => monaco.Uri.parse(editorModelUri(path, workspaceRoot));
// Native LibFiles creates only the target model requested by an editor action.
const model = (path: string, workspaceRoot = ROOT) => {
  const resource = uri(path, workspaceRoot);
  const existing = monaco.editor.getModel(resource);
  if (existing) return existing;
  const content = latestPublication()[1].find((file) => file.filePath === resource.toString())!.content;
  return monaco.editor.createModel(content, /\.[cm]?tsx?$/i.test(path) ? "typescript" : "javascript", resource);
};
const tab = (path: string) => useAppStore.getState().editorTabs.find((entry) => entry.path === path)!;
const latestPublication = () => publish.mock.calls.at(-1)! as [
  { workspaceRoot: string; sourceFileNames: string[]; caseSensitive: boolean }, Array<{ filePath: string; content: string }>,
];
const apply = (owner: WorkspaceModelIndex, files: WorkspaceProjectIndexFile[], includeDependencies = true) =>
  owner.applySnapshot(snapshot(files, owner.workspaceRoot), includeDependencies, new AbortController().signal);
function open(source: WorkspaceProjectIndexFile, activate = true) {
  useAppStore.getState().openEditorTab(source.path, { activate });
  useAppStore.getState().markTabLoaded(source.path, source.content, null, source.content_hash, { sizeBytes: source.size_bytes });
}
function type(target: monaco.editor.ITextModel, content: string) {
  target.pushStackElement();
  target.pushEditOperations([], [{ range: target.getFullModelRange(), text: content }], () => []);
  target.pushStackElement();
}
function renameEdits(target: monaco.editor.ITextModel): monaco.languages.IWorkspaceTextEdit[] {
  return [...target.getValue().matchAll(/\bvalue\b/g)].map((match) => {
    const start = target.getPositionAt(match.index!);
    const end = target.getPositionAt(match.index! + match[0].length);
    return { resource: target.uri, versionId: target.getVersionId(),
      textEdit: { range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column), text: "renamed" } };
  });
}

describe("workspace model indexing and real editor-buffer adoption", () => {
  it.each(["lines", "characters", "bytes"])("shares the editor %s limit with unopened sources, producing no editable model or language roots", async (limit) => {
    const owner = createOwner();
    const source = file("src/generated.ts", limit === "lines" ? "\n".repeat(20_000) : limit === "characters" ? "x".repeat(1_000_001) : "export const small = 1;");
    if (limit === "bytes") source.size_bytes = 2 * 1024 * 1024 + 1;
    const issues = await apply(owner, [source]);
    expect(issues).toEqual([{ path: source.path, message: expect.stringContaining("超过编辑器") }]);
    expect(owner.sourceCount()).toBe(0);
    expect(owner.ownsModel(source.path)).toBe(false);
    expect(monaco.editor.getModel(uri(source.path))).toBeNull();
    expect(latestPublication()[0].sourceFileNames).toEqual([]);
    expect(latestPublication()[1]).toEqual([]);
    expect(useAppStore.getState().editorTabs).toEqual([]);
  });

  it("keeps rejected tabs rejected and admits them again only after their disk source fits the same policy", async () => {
    const owner = createOwner();
    const large = file("src/generated.ts", "\n".repeat(20_000));
    useAppStore.getState().openEditorTab(large.path);
    useAppStore.getState().markTabLoaded(large.path, "", undefined, large.content_hash, { largeFile: true, loadWarning: "too many lines" });
    await apply(owner, [large]);
    expect(tab(large.path)).toMatchObject({ content: "", original: "", largeFile: true, loading: false, loadWarning: expect.stringContaining("20,001") });
    expect(monaco.editor.getModel(uri(large.path))).toBeNull();
    const small = file(large.path, "export const small = 1;");
    expect(await apply(owner, [small])).toEqual([]);
    expect(tab(large.path)).toMatchObject({ content: small.content, original: small.content, largeFile: false, loadWarning: null });
    expect(model(large.path).getValue()).toBe(small.content);
  });

  it("removes a formerly indexed clean model when the disk file grows beyond the editor limit", async () => {
    const owner = createOwner();
    const source = file("src/growing.ts", "export const value = 1;");
    await apply(owner, [source]);
    open(source);
    const previous = model(source.path);
    await apply(owner, [file(source.path, "\n".repeat(20_000))]);
    expect(previous.isDisposed()).toBe(true);
    expect(tab(source.path)).toMatchObject({ content: "", original: "", largeFile: true });
    expect(owner.resources()).toEqual([]);
    expect(latestPublication()[0].sourceFileNames).toEqual([]);
  });

  it("preserves a real draft and its baseline when the disk source grows beyond the editor limit", async () => {
    const owner = createOwner();
    const source = file("src/draft.ts", "export const value = 1;");
    await apply(owner, [source]);
    type(model(source.path), "export const user = 2;");
    await Promise.resolve();
    const draftModel = model(source.path);
    await apply(owner, [file(source.path, "\n".repeat(20_000))]);
    expect(model(source.path)).toBe(draftModel);
    expect(tab(source.path)).toMatchObject({ content: "export const user = 2;", original: source.content, largeFile: false, externalChanged: true });
    expect(owner.sourceCount()).toBe(0);
    expect(latestPublication()[1]).toEqual([{ filePath: uri(source.path).toString(), content: source.content }]);
  });

  it("preserves an existing read-only tab when a new index snapshot refreshes its baseline", async () => {
    const owner = createOwner();
    const source = file("src/generated.ts", "export const value = 1;");
    useAppStore.getState().openEditorTab(source.path);
    useAppStore.getState().markTabLoaded(source.path, source.content, undefined, source.content_hash, { readOnly: true });
    const updated = file(source.path, "export const value = 2;");
    await apply(owner, [updated]);
    expect(tab(source.path)).toMatchObject({ content: updated.content, original: updated.content, readOnly: true });
    expect(latestPublication()[0]).toMatchObject({ readOnlyFileNames: [uri(source.path).toString()] });
    useAppStore.getState().markTabLoaded(source.path, updated.content, undefined, updated.content_hash, { readOnly: false });
    expect(latestPublication()[0]).toMatchObject({ readOnlyFileNames: [] });
  });

  it("owns a hidden project source only when a native action creates its model", async () => {
    const owner = createOwner();
    const source = file(".storybook/preview.ts", "export const preview = 1;");
    await apply(owner, [source]);
    expect(owner.ownsModel(source.path)).toBe(false);
    expect(monaco.editor.getModels()).toEqual([]);
    expect(owner.sourceCount()).toBe(1);
    type(model(source.path), "export const preview = 2;");
    await Promise.resolve();
    expect(tab(source.path)).toMatchObject({ content: "export const preview = 2;", original: source.content, contentHash: source.content_hash });
  });

  it.each([ROOT, "C:/项目"])("indexes TS, JS and project declarations in %s without creating unopened models", async (workspaceRoot) => {
    useAppStore.setState({ workingDirectory: workspaceRoot });
    const files = [
      file("src/中文.tsx", "export const 标题 = <div />;\r\n"),
      file("shared/helper.mjs", "export const helper = 1;\n"),
      file("types/project.d.ts", "declare const local: string;", "declaration"),
      file("node_modules/library/dist/index.d.ts", "export interface External {}", "declaration"),
      file("node_modules/library/package.json", '{"types":"dist/index.d.ts"}', "package"),
      file("tsconfig.json", '{"compilerOptions":{"strict":true}}', "config"),
    ];
    const owner = createOwner(workspaceRoot);
    await apply(owner, files);
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().activeTabPath).toBeNull();
    expect(owner.sourceCount()).toBe(3);
    expect(owner.resources()).toEqual([]);
    expect(monaco.editor.getModels()).toEqual([]);
    expect(owner.sourceFiles().map((source) => source.uri.toString())).toEqual(files.slice(0, 3).map((source) => uri(source.path, workspaceRoot).toString()));
    expect(model("src/中文.tsx", workspaceRoot).getValue(undefined, true)).toBe(files[0].content);
    expect(model("src/中文.tsx", workspaceRoot).getLanguageId()).toBe("typescript");
    expect(model("shared/helper.mjs", workspaceRoot).getLanguageId()).toBe("javascript");
    expect(model("types/project.d.ts", workspaceRoot).getLanguageId()).toBe("typescript");
    expect(monaco.editor.getModel(uri("node_modules/library/dist/index.d.ts", workspaceRoot))).toBeNull();
    expect(monaco.editor.getModel(uri("tsconfig.json", workspaceRoot))).toBeNull();
    const [metadata, extras] = latestPublication();
    expect(new Set(metadata.sourceFileNames)).toEqual(new Set(owner.resources().map((resource) => resource.toString())));
    expect(metadata.workspaceRoot).toBe(uri(".", workspaceRoot).toString());
    expect(extras).toEqual(expect.arrayContaining(files.map((source) => ({ filePath: uri(source.path, workspaceRoot).toString(), content: source.content }))));
  });

  it("keeps a large index as worker sources while retaining a real user's draft model and Undo", async () => {
    const owner = createOwner();
    const sources = Array.from({ length: 700 }, (_, index) => file(`src/file${index}.ts`, `export const value${index} = ${index};`));
    await apply(owner, sources);
    expect(owner.sourceCount()).toBe(700);
    expect(monaco.editor.getModels()).toEqual([]);
    open(sources[0]);
    const draft = model(sources[0].path);
    type(draft, "export const unsaved = 1;");
    await Promise.resolve();
    await apply(owner, sources, false);
    expect(monaco.editor.getModels()).toEqual([draft]);
    expect(model(sources[0].path)).toBe(draft);
    expect(draft.getValue()).toBe("export const unsaved = 1;");
    await draft.undo();
    expect(draft.getValue()).toBe(sources[0].content);
  });

  it("refreshes the complete source set without removing already loaded dependency types", async () => {
    const owner = createOwner();
    const a = file("src/a.ts", "export const a = 1;");
    const dependency = file("node_modules/library/index.d.ts", "export interface External {}", "declaration");
    await apply(owner, [a, dependency]);
    await apply(owner, [file("src/b.js", "export const b = 2;")], false);
    expect(monaco.editor.getModel(uri(a.path))).toBeNull();
    expect(owner.sourceCount()).toBe(1);
    expect(model("src/b.js").getValue()).toBe("export const b = 2;");
    expect(useAppStore.getState().editorTabs).toEqual([]);
    const [metadata, extras] = latestPublication();
    expect(metadata.sourceFileNames).toEqual([uri("src/b.js").toString()]);
    expect(extras).toEqual(expect.arrayContaining([{ filePath: uri(dependency.path).toString(), content: dependency.content }]));
  });

  it("adopts both F2-edited files atomically when the hidden B edit precedes active A and the old UI updates A immediately", async () => {
    const a = file("src/a.ts", "export const value = 1;\n");
    const b = file("src/b.ts", 'import { value } from "./a";\nvalue;\n');
    const owner = createOwner();
    await apply(owner, [a, b]);
    open(a);
    const aModel = model(a.path);
    const bModel = model(b.path);
    const beforeA = `${a.content}// prior unsaved input\n`;
    type(aModel, beforeA);
    await Promise.resolve();
    const activeId = tab(a.path).id;
    const adopt = vi.fn(actualAdopt);
    useAppStore.setState({ adoptEditorModelChanges: adopt });
    subscriptions.push(aModel.onDidChangeContent(() => useAppStore.getState().updateTabContent(a.path, aModel.getValue(undefined, true))));
    const network = vi.spyOn(globalThis, "fetch");
    const service = StandaloneServices.get<BulkEditService>(IBulkEditService);

    await service.apply({ edits: [...renameEdits(bModel), ...renameEdits(aModel)] }, { label: "Rename value" });
    expect(adopt).toHaveBeenCalledOnce();
    const [changes, targetWorkspace] = adopt.mock.calls[0];
    expect(targetWorkspace).toBe(ROOT);
    expect(new Set(changes.map((change) => change.path))).toEqual(new Set([a.path, b.path]));
    expect(tab(a.path)).toMatchObject({ id: activeId, content: beforeA.replace(/\bvalue\b/g, "renamed"), original: a.content, contentHash: a.content_hash, loading: false });
    expect(tab(b.path)).toMatchObject({ content: b.content.replace(/\bvalue\b/g, "renamed"), original: b.content, contentHash: b.content_hash, loading: false });
    expect(useAppStore.getState().activeTabPath).toBe(a.path);
    expect(useAppStore.getState().activeEditorPath).toBe(a.path);
    expect(network).not.toHaveBeenCalled();

    await bModel.undo();
    await Promise.resolve();
    expect([aModel.getValue(), bModel.getValue()]).toEqual([beforeA, b.content]);
    expect([tab(a.path).content, tab(b.path).content]).toEqual([beforeA, b.content]);
    await aModel.redo();
    await Promise.resolve();
    expect([tab(a.path).content, tab(b.path).content]).toEqual([beforeA.replace(/\bvalue\b/g, "renamed"), b.content.replace(/\bvalue\b/g, "renamed")]);
  });

  it.each(["modified", "deleted"])("flushes a just-typed draft before applying a %s disk snapshot", async (event) => {
    const source = file("src/draft.ts", "export const original = 1;");
    const owner = createOwner();
    await apply(owner, [source]);
    const draft = "export const unsaved = 2;";
    type(model(source.path), draft);
    if (event === "deleted") owner.noteFileChanges([{ path: source.path, event }]);
    await apply(owner, event === "deleted" ? [] : [file(source.path, "export const disk = 3;")], false);
    expect(tab(source.path)).toMatchObject({ content: draft, original: source.content, contentHash: source.content_hash, loading: false, externalChanged: true });
    expect(model(source.path).getValue()).toBe(draft);
    expect(owner.ownsModel(source.path)).toBe(true);
    expect(owner.sourceCount()).toBe(event === "deleted" ? 0 : 1);
  });

  it("uses a newly saved baseline and hash for edits made after closing an indexed file", async () => {
    const original = file("src/saved.ts", "export const count = 1;");
    const owner = createOwner();
    await apply(owner, [original]);
    const firstDraft = "export const count = 2;";
    const target = model(original.path);
    type(target, firstDraft);
    await Promise.resolve();
    const saved = file(original.path, firstDraft);
    useAppStore.getState().markTabSaved(saved.path, saved.content, saved.content_hash, saved.size_bytes, ROOT);
    useAppStore.getState().closeEditorTab(saved.path);
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(model(saved.path)).toBe(target);
    expect(target.getValue()).toBe(saved.content);

    type(target, "export const count = 3;");
    await Promise.resolve();
    expect(tab(saved.path)).toMatchObject({ content: "export const count = 3;", original: saved.content, contentHash: saved.content_hash, loading: false });
    expect(useAppStore.getState().activeTabPath).toBeNull();
  });

  it("discards a closed draft back to its disk baseline without losing the hidden index", async () => {
    const original = file("src/discard.ts", "export const count = 1;");
    const owner = createOwner();
    await apply(owner, [original]);
    const target = model(original.path);
    type(target, "export const count = 2;");
    await Promise.resolve();
    useAppStore.getState().closeEditorTab(original.path);
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(target.getValue()).toBe(original.content);
    expect(owner.ownsModel(original.path)).toBe(true);
    expect(owner.sourceCount()).toBe(1);
    type(target, "export const count = 3;");
    await Promise.resolve();
    expect(tab(original.path)).toMatchObject({ content: "export const count = 3;", original: original.content, contentHash: original.content_hash });
  });

  it("refreshes clean files while retaining dirty file content, identity and original baseline", async () => {
    const a = file("src/dirty.ts", "export const original = 1;");
    const b = file("src/clean.js", "export const original = 2;");
    const owner = createOwner();
    await apply(owner, [a, b]);
    open(a);
    open(b, false);
    const dirtyId = tab(a.path).id;
    type(model(a.path), "export const user = 3;");
    await Promise.resolve();
    const nextA = file(a.path, "export const disk = 4;");
    const nextB = file(b.path, "export const disk = 5;");
    await apply(owner, [nextA, nextB], false);
    expect(tab(a.path)).toMatchObject({ id: dirtyId, content: "export const user = 3;", original: a.content, contentHash: a.content_hash, externalChanged: true });
    expect(model(a.path).getValue()).toBe("export const user = 3;");
    expect(tab(b.path)).toMatchObject({ content: nextB.content, original: nextB.content, contentHash: nextB.content_hash, externalChanged: false });
    expect(model(b.path).getValue()).toBe(nextB.content);
    expect(useAppStore.getState().activeTabPath).toBe(a.path);
    expect(latestPublication()[1]).toEqual(expect.arrayContaining([{ filePath: uri(a.path).toString(), content: nextA.content }]));
  });

  it("removes a deleted closed model but marks an open deleted file as externally changed", async () => {
    const a = file("src/closed.ts", "export const closed = 1;");
    const b = file("src/open.ts", "export const open = 2;");
    const owner = createOwner();
    await apply(owner, [a, b]);
    open(b);
    const openId = tab(b.path).id;
    const closedModel = model(a.path);
    owner.noteFileChanges([{ path: a.path, event: "delete" }, { path: b.path, event: "deleted" }]);
    await apply(owner, [], false);
    expect(closedModel.isDisposed()).toBe(true);
    expect(owner.ownsModel(a.path)).toBe(false);
    expect(monaco.editor.getModel(uri(a.path))).toBeNull();
    expect(tab(b.path)).toMatchObject({ id: openId, content: b.content, original: b.content, externalChanged: true });
    expect(model(b.path).getValue()).toBe(b.content);
    expect(owner.sourceCount()).toBe(0);
    expect(latestPublication()[0].sourceFileNames).toEqual([uri(b.path).toString()]);
  });

  it.each([false, true])("detects a missing open source from the snapshot alone, preserves its draft=%s and replacement conflict until reload", async (dirty) => {
    const source = file("src/missing.ts", "export const original = 1;");
    const owner = createOwner();
    await apply(owner, [source]);
    open(source);
    const opened = model(source.path);
    if (dirty) { type(opened, "export const unsaved = 2;"); await Promise.resolve(); }
    const version = opened.getAlternativeVersionId();
    await apply(owner, [], false);
    expect(tab(source.path).externalChanged).toBe(true);
    expect(model(source.path)).toBe(opened);
    expect(opened.getAlternativeVersionId()).toBe(version);
    await apply(owner, [source], false);
    expect(tab(source.path).externalChanged).toBe(true);
    expect(opened.getValue()).toBe(dirty ? "export const unsaved = 2;" : source.content);
    useAppStore.getState().markTabLoaded(source.path, source.content, undefined, source.content_hash);
    await apply(owner, [source], false);
    expect(tab(source.path).externalChanged).toBe(false);
  });

  it("keeps deletion history with a renamed draft and does not give a fresh buffer at the old path that conflict", async () => {
    const source = file("src/old.ts", "export const original = 1;");
    const destination = file("src/new.ts", source.content);
    const owner = createOwner();
    await apply(owner, [source]);
    open(source);
    const originalModel = model(source.path);
    type(originalModel, "export const unsaved = 2;");
    await Promise.resolve();
    const bufferId = tab(source.path).id;
    await apply(owner, [], false);
    useAppStore.getState().renameEditorPath(source.path, destination.path, ROOT);
    const renamed = renameMonacoModel(monaco, originalModel, uri(destination.path));
    originalModel.dispose();
    open(source, false);
    await apply(owner, [source, destination], false);
    expect(tab(destination.path)).toMatchObject({ id: bufferId, content: "export const unsaved = 2;", externalChanged: true });
    expect(tab(source.path).id).not.toBe(bufferId);
    expect(tab(source.path).externalChanged).toBe(false);
    expect(model(destination.path)).toBe(renamed);
    await renamed.undo();
    expect(renamed.getValue()).toBe(source.content);
    useAppStore.getState().closeEditorTab(destination.path);
    open(destination, false);
    await apply(owner, [source, destination], false);
    expect(tab(destination.path).id).not.toBe(bufferId);
    expect(tab(destination.path).externalChanged).toBe(false);
  });

  it("flushes pending old-workspace edits into its real buffer cache when disposed after an owner switch", async () => {
    const source = file("src/old.ts", "export const original = 1;");
    const dependency = file("node_modules/lib/index.d.ts", "export interface External {}", "declaration");
    const owner = createOwner();
    await apply(owner, [source, dependency]);
    open(source);
    const oldId = tab(source.path).id;
    const target = model(source.path);
    const draft = "export const unsaved = 2;";
    type(target, draft);
    useAppStore.getState().setWorkingDirectory("/other-workspace");
    disposeOwner(owner);
    expect(monaco.editor.getModels()).toEqual([]);
    expect(useAppStore.getState().workingDirectory).toBe("/other-workspace");
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(editorStateForWorkspace(ROOT).editorTabs).toEqual([expect.objectContaining({ id: oldId, content: draft, original: source.content, contentHash: source.content_hash })]);
    expect(latestPublication()).toEqual([{ workspaceRoot: uri(".").toString(), sourceFileNames: [], readOnlyFileNames: [], caseSensitive: true }, []]);
    useAppStore.getState().setWorkingDirectory(ROOT);
    const restored = createOwner();
    await apply(restored, [source, dependency]);
    expect(tab(source.path)).toMatchObject({ id: oldId, content: draft, original: source.content, contentHash: source.content_hash });
    expect(model(source.path).getValue()).toBe(draft);
  });

  it("reuses an already indexed rename destination while preserving the source's actual Undo/Redo history", async () => {
    const original = file("src/old.ts", "export const count = 1;\r\n");
    const destination = file("src/new.ts", original.content);
    const owner = createOwner();
    await apply(owner, [original]);
    open(original);
    const oldModel = model(original.path);
    const firstDraft = "export const count = 2;\r\n";
    type(oldModel, firstDraft);
    await Promise.resolve();
    const bufferId = tab(original.path).id;
    const firstVersion = oldModel.getAlternativeVersionId();
    type(oldModel, "export const count = 3;\r\n");
    await Promise.resolve();
    await apply(owner, [destination], false);
    const indexedDestination = model(destination.path);
    useAppStore.getState().renameEditorPath(original.path, destination.path, ROOT);
    const renamed = renameMonacoModel(monaco, oldModel, uri(destination.path));
    oldModel.dispose();
    await Promise.resolve();
    expect(renamed).toBe(indexedDestination);
    expect(owner.ownsModel(original.path)).toBe(false);
    expect(owner.ownsModel(destination.path)).toBe(true);
    expect(useAppStore.getState().editorTabs).toHaveLength(1);
    expect(tab(destination.path)).toMatchObject({ id: bufferId, content: "export const count = 3;\r\n", original: original.content, contentHash: original.content_hash });
    await renamed.undo();
    await Promise.resolve();
    expect(renamed.getValue()).toBe(firstDraft);
    expect(renamed.getAlternativeVersionId()).toBe(firstVersion);
    expect(tab(destination.path).content).toBe(firstDraft);
    await renamed.redo();
    await Promise.resolve();
    expect(tab(destination.path).content).toBe("export const count = 3;\r\n");
    expect(owner.sourceCount()).toBe(1);
  });
});
