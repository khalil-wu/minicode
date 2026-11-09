/* @vitest-environment jsdom */
import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { typescript as ts } from "monaco-editor/languages/features/typescript/lib/typescriptServices.js";
import { WorkspaceTypeScriptService } from "./workspaceTypeScriptService";
import { WORKSPACE_TYPESCRIPT_METADATA_URI } from "./workspaceTypeScriptContract";
import { useWorkspaceModelIndex } from "./useWorkspaceModelIndex";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { readWorkspaceFile, readWorkspaceProjectIndex } from "../protocol/workspace";
import { editorModelUri } from "./monacoLanguageServices";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

const runtime = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
  return { root: "/index 热更新", revision: 1, version: 0,
    main: 'import { value } from "library";\nconst label: string = value;',
    bodies: new Map<string, string>(), extras: {} as Record<string, { content: string; version: number }>,
    service: null as unknown as WorkspaceTypeScriptService };
});
const hash = (content: string) => createHash("sha256").update(content).digest("hex");
vi.mock("../protocol/workspace", () => ({ readWorkspaceFile: vi.fn(), readWorkspaceProjectIndex: vi.fn() }));
vi.mock("./monacoLanguageServices", async (original) => ({
  ...await original<typeof import("./monacoLanguageServices")>(),
  setWorkspaceTypeScriptFiles: (metadata: unknown, files: Array<{ filePath: string; content: string }>) => {
    runtime.version++;
    runtime.extras = Object.fromEntries(files.map((file) => [file.filePath, { content: file.content, version: runtime.version }]));
    runtime.extras[WORKSPACE_TYPESCRIPT_METADATA_URI] = { content: JSON.stringify(metadata), version: runtime.version };
    runtime.service = new WorkspaceTypeScriptService({ getMirrorModels: () => monaco.editor.getModels().map((model) => ({
      uri: model.uri, version: model.getVersionId(), getValue: () => model.getValue(),
    })) }, { compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.NodeJs }, extraLibs: runtime.extras });
  },
  syncWorkspaceTypeScriptModels: async () => {
    const service = runtime.service;
    return {
      configurationRequests: () => service.getConfigurationFileRequests(),
      dependencyRequests: () => service.getDependencyFileRequests(),
      configurationDiagnostics: () => service.getConfigurationDiagnostics(),
      addConfigurationFiles: async (files: Array<{ filePath: string; content: string }>) => {
        const next = { ...runtime.extras };
        for (const file of files) next[file.filePath] = { content: file.content, version: ++runtime.version };
        runtime.extras = next;
        await service.updateExtraLibs(next);
      },
    };
  },
}));

beforeAll(() => monaco.languages.register({ id: "typescript" }));
beforeEach(() => {
  vi.clearAllMocks();
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  runtime.revision = 1;
  runtime.version = 0;
  runtime.bodies = new Map([
    ["node_modules/library/package.json", '{"types":"first.d.ts"}'],
    ["node_modules/library/first.d.ts", "export declare const value: string;"],
    ["node_modules/library/second.d.ts", "export declare const value: string;"],
    ["node_modules/unrelated/index.d.ts", "declare const poisoned: true;"],
  ]);
  useAppStore.setState({ workingDirectory: runtime.root, conversationId: "owner", fileChanges: [], editorTabs: [],
    editorOpenRequests: [], activeTabPath: null, activeEditorPath: null });
  vi.mocked(readWorkspaceProjectIndex).mockImplementation(async () => ({ workspace_root: runtime.root,
    revision: runtime.revision, index_instance_id: "index-instance", dependency_paths: [...runtime.bodies.keys()],
    complete: true, issues: [], files: [{ path: "src/main.ts", content: runtime.main, content_hash: hash(runtime.main), kind: "source" }] }));
  vi.mocked(readWorkspaceFile).mockImplementation(async (path) => {
    const relative = path.replaceAll("\\", "/").slice(runtime.root.length + 1);
    const content = runtime.bodies.get(relative)!;
    expect(content).toBeDefined();
    return { path: relative, content, content_hash: hash(content), size_bytes: Buffer.byteLength(content) };
  });
});
afterEach(() => {
  cleanup();
  monaco.editor.getModels().forEach((model) => model.dispose());
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
});

it("rereads changed declarations and package metadata through the real native resolver without losing drafts", async () => {
  const path = "src/main.ts";
  useAppStore.getState().openEditorTab(path);
  useAppStore.getState().markTabLoaded(path, runtime.main, undefined, hash(runtime.main));
  const resource = monaco.Uri.parse(editorModelUri(path, runtime.root));
  const model = monaco.editor.createModel(runtime.main, "typescript", resource);
  const { result } = renderHook(() => useWorkspaceModelIndex(runtime.root));
  act(() => result.current.initialize(monaco));
  await waitFor(() => expect(result.current.status, JSON.stringify(result.current.status.issues)).toMatchObject({ phase: "ready" }), { timeout: 5000 });
  expect(await runtime.service.getSemanticDiagnostics(resource.toString())).toEqual([]);
  const draft = runtime.main + "\n// 未保存的修改";
  act(() => model.pushEditOperations([], [{ range: model.getFullModelRange(), text: draft }], () => []));
  await act(async () => { await Promise.resolve(); });

  const signal = (revision: number, owner = "owner", workspaceRoot = runtime.root) => {
    const event = normalizeInboundServerEvent({ type: "workspace.project_index.invalidated", conversation_id: owner,
      workspace_root: workspaceRoot, index_instance_id: "index-instance", revision });
    expect(event).not.toBeNull();
    act(() => { expect(handlePeripheralEvent(event!)).toBe(true); });
  };
  runtime.bodies.set("node_modules/library/first.d.ts", "export declare const value: number;");
  runtime.revision = 2;
  signal(2);
  await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.status.phase).toBe("ready"), { timeout: 5000 });
  expect(await runtime.service.getSemanticDiagnostics(resource.toString()))
    .toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
  expect(vi.mocked(readWorkspaceFile).mock.calls.filter(([file]) => file.endsWith("first.d.ts"))).toHaveLength(2);

  runtime.bodies.set("node_modules/library/package.json", '{"types":"second.d.ts"}');
  runtime.revision = 3;
  signal(3);
  await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(3));
  await waitFor(() => expect(result.current.status.phase).toBe("ready"), { timeout: 5000 });
  expect(await runtime.service.getSemanticDiagnostics(resource.toString())).toEqual([]);
  expect(vi.mocked(readWorkspaceFile).mock.calls.filter(([file]) => file.endsWith("package.json"))).toHaveLength(3);
  expect(vi.mocked(readWorkspaceFile).mock.calls.filter(([file]) => file.endsWith("second.d.ts"))).toHaveLength(1);
  expect(vi.mocked(readWorkspaceFile).mock.calls.some(([file]) => file.includes("unrelated"))).toBe(false);
  expect(vi.mocked(readWorkspaceProjectIndex).mock.calls[2].slice(3)).toEqual([2, "index-instance"]);
  expect(model.getValue()).toBe(draft);
  expect(useAppStore.getState().editorTabs.find((tab) => tab.path === path)?.content).toBe(draft);
  expect(useAppStore.getState().fileChanges).toEqual([]);
  signal(3);
  expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(3);
  act(() => useAppStore.setState({ conversationId: "another-conversation" }));
  runtime.revision = 4;
  signal(4, "owner");
  await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(4));
  await waitFor(() => expect(result.current.status.phase).toBe("ready"), { timeout: 5000 });
  signal(5, "owner", "/different-workspace");
  expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(4);
  expect(model.getValue()).toBe(draft);
  // Four bounded native compiler cycles share this aggregate budget. Root's
  // isolated run measured 6.33 s; parallel full-suite CPU contention exceeded
  // the old aggregate 10 s. Each individual ready gate remains 5 s.
}, 4 * 5000);
