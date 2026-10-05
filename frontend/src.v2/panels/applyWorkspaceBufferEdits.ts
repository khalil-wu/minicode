import type { EditorTextSurface } from "./editor-text-surface";
import { useAppStore } from "../stores";
import { editorWorkspaceKey, editorPathComparisonKey, editorPathsEqual, editorStateForWorkspace, normalizeEditorPath } from "../stores/shared-helpers";
import { workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { editorModelUri } from "./monacoLanguageServices";

export interface WorkspaceOffsetEdit { offset: number; length: number; text: string; }
export interface WorkspaceBufferTransaction { before: string; after: string; edits: WorkspaceOffsetEdit[]; label: string; }
export interface WorkspaceBufferEdit {
  path: string; before: string; original?: string; contentHash?: string; sizeBytes?: number; expectedVersion?: number;
  edits: WorkspaceOffsetEdit[];
}

const surfaces = new Map<string, EditorTextSurface>();
const surfaceKey = (workspace: string, path: string) => JSON.stringify([editorWorkspaceKey(workspace), editorPathComparisonKey(path, workspace)]);
const trackedModels = new WeakSet<object>();
const retainedModels = new Set<string>();
export const retainsWorkspaceBufferEditModel = (workspace: string, path: string): boolean => retainedModels.has(surfaceKey(workspace, path));

export function registerWorkspaceEditorSurface(workspace: string, path: string, editor: EditorTextSurface): () => void {
  const key = surfaceKey(workspace, path);
  surfaces.set(key, editor);
  return () => { if (surfaces.get(key) === editor) surfaces.delete(key); };
}

export function applyOffsetEdits(before: string, edits: WorkspaceOffsetEdit[]): string {
  let cursor = 0;
  const parts: string[] = [];
  for (const edit of [...edits].sort((a, b) => a.offset - b.offset)) {
    parts.push(before.slice(cursor, edit.offset), edit.text);
    cursor = edit.offset + edit.length;
  }
  return parts.join("") + before.slice(cursor);
}

export function offsetRange(before: string, edit: WorkspaceOffsetEdit) {
  const position = (offset: number) => {
    const lines = before.slice(0, offset).split(/\r\n|\r|\n/);
    return { line: lines.length, column: lines.at(-1)!.length + 1 };
  };
  const start = position(edit.offset);
  const end = position(edit.offset + edit.length);
  return { startLineNumber: start.line, startColumn: start.column, endLineNumber: end.line, endColumn: end.column };
}

/** One explicit edit command for search replacements and language-service fixes. */
export async function applyWorkspaceBufferEdits(workspaceRoot: string, files: WorkspaceBufferEdit[], label: string): Promise<void> {
  const [monaco, { StandaloneServices }, { IBulkEditService }, { withNativeModelUndoGroup }] = await Promise.all([
    import("monaco-editor/editor/editor.api.js"),
    import("monaco-editor/editor/standalone/browser/standaloneServices.js"),
    import("monaco-editor/editor/browser/services/bulkEditService.js"),
    import("./nativeModelUndoGroup"),
  ]);
  const state = useAppStore.getState();
  if (!workspaceRootsEqual(state.workingDirectory, workspaceRoot)) throw new Error("工作区已切换，请回到原工作区后重新预览。");
  const resolved = files.map((file) => {
    const eol = file.before.match(/\r\n|\r|\n/)?.[0] ?? "\n";
    const edits = file.edits.map((edit) => ({ ...edit, text: edit.text.replace(/\r\n|\r|\n/g, eol) }));
    const path = normalizeEditorPath(file.path.startsWith("file:") ? monaco.Uri.parse(file.path).fsPath : file.path, workspaceRoot);
    const resource = monaco.Uri.parse(editorModelUri(path, workspaceRoot));
    if (!workspacePathWithin(resource.fsPath, workspaceRoot)) throw new Error(`文件不属于当前工作区：${path}`);
    const tab = state.editorTabs.find((entry) => editorPathsEqual(entry.path, path, workspaceRoot));
    const markdown = /\.mdx?$/i.test(path);
    const surface = markdown ? surfaces.get(surfaceKey(workspaceRoot, path)) : undefined;
    const surfaceModel = surface?.getModel();
    const surfaceText = surfaceModel?.getValueInRange({ startLineNumber: 1, startColumn: 1,
      endLineNumber: surfaceModel.getLineCount(), endColumn: surfaceModel.getLineMaxColumn(surfaceModel.getLineCount()) });
    const model = markdown ? null : monaco.editor.getModel(resource);
    const current = tab?.content ?? model?.getValue(undefined, true) ?? file.before;
    if (tab?.readOnly) throw new Error(`文件仅供只读：${path}`);
    if (current !== file.before || (surfaceText !== undefined && surfaceText !== file.before) || (model && model.getValue(undefined, true) !== file.before)
      || (model && file.expectedVersion !== undefined && model.getVersionId() !== file.expectedVersion)) throw new Error(`${path} 已改变，请重新计算并预览修改。`);
    return { ...file, edits, path, resource, tab, markdown, model, after: applyOffsetEdits(file.before, edits) };
  });
  const native = resolved.filter((file) => !file.markdown).map((file) => {
    const model = file.model ?? monaco.editor.createModel(file.before, undefined, file.resource);
    if (!trackedModels.has(model)) {
      trackedModels.add(model);
      const key = surfaceKey(workspaceRoot, file.path);
      retainedModels.add(key);
      const changed = model.onDidChangeContent(() => {
        const current = useAppStore.getState();
        const owner = workspaceRootsEqual(current.workingDirectory, workspaceRoot) ? current : editorStateForWorkspace(workspaceRoot);
        const tab = owner.editorTabs.find((entry) => editorPathsEqual(entry.path, file.path, workspaceRoot));
        current.adoptEditorModelChanges([{ path: file.path, content: model.getValue(undefined, true), original: tab?.original ?? file.original ?? file.before,
          contentHash: tab?.contentHash ?? file.contentHash ?? "", sizeBytes: file.sizeBytes }], workspaceRoot);
      });
      model.onWillDispose(() => { changed.dispose(); retainedModels.delete(key); });
    }
    return { ...file, model };
  });
  const bulk = StandaloneServices.get<import("monaco-editor/editor/browser/services/bulkEditService.js").BulkEditService>(IBulkEditService);
  const edits = native.flatMap((file) => file.edits.map((edit) => ({ resource: file.resource, versionId: file.model.getVersionId(), textEdit: { range: offsetRange(file.before, edit), text: edit.text } })));
  if (edits.length) await withNativeModelUndoGroup(native.map((file) => file.model), () => bulk.apply({ edits }, { label }));
  for (const file of resolved.filter((entry) => entry.markdown)) {
    const surface = surfaces.get(surfaceKey(workspaceRoot, file.path));
    if (surface) {
      surface.pushUndoStop?.();
      surface.executeEdits(label, file.edits.map((edit) => ({ range: offsetRange(file.before, edit), text: edit.text, forceMoveMarkers: true })));
      surface.pushUndoStop?.();
    } else {
      useAppStore.getState().queueEditorBufferTransaction(file.path, { before: file.before, after: file.after, edits: file.edits, label }, file.original ?? file.before, file.contentHash ?? "", workspaceRoot);
    }
  }
  useAppStore.getState().adoptEditorModelChanges(resolved.map((file) => ({ path: file.path, content: file.after, original: file.original ?? file.before,
    contentHash: file.contentHash ?? "", sizeBytes: file.sizeBytes })), workspaceRoot);
  if (resolved[0]) useAppStore.getState().openEditorFile(resolved[0].path, undefined, { exact: true });
}
