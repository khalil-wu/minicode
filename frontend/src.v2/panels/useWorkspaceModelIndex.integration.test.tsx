/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { readWorkspaceProjectIndex, type WorkspaceProjectIndex } from "../protocol/workspace";
import { editorModelUri } from "./monacoLanguageServices";
import { useWorkspaceModelIndex } from "./useWorkspaceModelIndex";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) }));
vi.mock("../protocol/workspace", () => ({ readWorkspaceProjectIndex: vi.fn(), readWorkspaceFile: vi.fn() }));
vi.mock("./monacoLanguageServices", async (original) => ({
  ...await original<typeof import("./monacoLanguageServices")>(),
  setWorkspaceTypeScriptFiles: vi.fn(),
  syncWorkspaceTypeScriptModels: vi.fn(async () => ({ configurationRequests: async () => [], configurationDiagnostics: async () => [], addConfigurationFiles: async () => {} })),
}));

const root = "/pending-index";
const path = "src/draft.ts";
const original = "export const value = 1;";
const snapshot = (content: string): WorkspaceProjectIndex => ({ workspace_root: root, complete: true, issues: [],
  files: [{ path, content, content_hash: content, kind: "source" }] });
beforeAll(() => monaco.languages.register({ id: "typescript" }));
beforeEach(() => {
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  useAppStore.setState({ workingDirectory: root, fileChanges: [], editorTabs: [], editorOpenRequests: [],
    activeTabPath: null, activeEditorPath: null, conversationId: null });
});
afterEach(() => { cleanup(); monaco.editor.getModels().forEach((model) => model.dispose()); clearEditorWorkspaceBufferCacheForTests(); localStorage.clear(); vi.clearAllMocks(); });

describe("shared index refresh with actual editor drafts", () => {
  it.each(["missing", "recreated"])("retains a queued deletion beyond the global history when its path is %s", async (target) => {
    useAppStore.getState().openEditorTab(path);
    useAppStore.getState().markTabLoaded(path, original, undefined, original);
    const model = monaco.editor.createModel(original, "typescript", monaco.Uri.parse(editorModelUri(path, root)));
    let finishFirst!: (snapshot: WorkspaceProjectIndex) => void;
    let finishSecond!: (snapshot: WorkspaceProjectIndex) => void;
    vi.mocked(readWorkspaceProjectIndex)
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve; }));
    const { result } = renderHook(() => useWorkspaceModelIndex(root));
    act(() => result.current.initialize(monaco));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce());
    await act(async () => {
      model.pushStackElement();
      model.pushEditOperations([], [{ range: model.getFullModelRange(), text: "export const value = 2;" }], () => []);
      model.pushStackElement();
      useAppStore.getState().addFileChange({ path, event: "deleted", timestamp: 1 });
      if (target === "recreated") useAppStore.getState().addFileChange({ path, event: "created", timestamp: 2 });
      for (let timestamp = 3; timestamp <= 252; timestamp++) useAppStore.getState().addFileChange({
        path: `src/other${timestamp % 5}.ts`, event: "modified", timestamp,
      });
    });
    expect(useAppStore.getState().fileChanges).toHaveLength(100);
    expect(useAppStore.getState().fileChanges.some((change) => change.path === path)).toBe(false);
    expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce();
    await act(async () => finishFirst(snapshot(original)));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    await act(async () => finishSecond(target === "recreated" ? snapshot(original) : { ...snapshot(original), files: [] }));
    expect(result.current.status.phase).toBe("ready");
    expect(result.current.status.sourceCount).toBe(target === "recreated" ? 1 : 0);
    expect(monaco.editor.getModels()).toEqual([model]);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "export const value = 2;", original, externalChanged: true });
    await act(async () => model.undo());
    expect(model.getValue()).toBe(original);
    expect(useAppStore.getState().editorTabs[0].externalChanged).toBe(true);
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
  });

  it("retains draft model identity and Undo while a queued refresh reports a newly changed disk baseline", async () => {
    useAppStore.getState().openEditorTab(path);
    useAppStore.getState().markTabLoaded(path, original, undefined, original);
    const model = monaco.editor.createModel(original, "typescript", monaco.Uri.parse(editorModelUri(path, root)));
    let finishFirst!: (snapshot: WorkspaceProjectIndex) => void;
    let finishSecond!: (snapshot: WorkspaceProjectIndex) => void;
    vi.mocked(readWorkspaceProjectIndex)
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve; }));
    const { result } = renderHook(() => useWorkspaceModelIndex(root));
    act(() => result.current.initialize(monaco));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce());
    const signal = vi.mocked(readWorkspaceProjectIndex).mock.calls[0][2]!;
    await act(async () => {
      model.pushStackElement();
      model.pushEditOperations([], [{ range: model.getFullModelRange(), text: "export const value = 2;" }], () => []);
      model.pushStackElement();
      for (let sequence = 1; sequence <= 25; sequence++) useAppStore.setState((state) => ({ fileChanges: [...state.fileChanges,
        { path, event: "modified", timestamp: sequence, sequence, workspaceRoot: root }] }));
    });
    const draftVersion = model.getAlternativeVersionId();
    expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce();
    expect(signal.aborted).toBe(false);
    await act(async () => finishFirst(snapshot(original)));
    expect(result.current.status.phase).toBe("ready");
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith(root, false, signal);
    await act(async () => finishSecond(snapshot("export const value = 3;")));
    expect(result.current.status.phase).toBe("ready");
    expect(monaco.editor.getModels()).toEqual([model]);
    expect(model.getAlternativeVersionId()).toBe(draftVersion);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "export const value = 2;", original, externalChanged: true });
    await act(async () => model.undo());
    expect(model.getValue()).toBe(original);
    expect(useAppStore.getState().editorTabs[0].content).toBe(original);
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
  });
});
