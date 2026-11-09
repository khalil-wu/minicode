import type * as Monaco from "monaco-editor/editor/editor.api.js";

interface UndoSnapshot { resource: Monaco.Uri; elements: number[] }
interface ModelUndoElement {
  actual: { setModel: (model: RenameableModel) => void };
  strResource: string;
  resourceLabel: string;
  strResources: string[];
  resourceLabels: string[];
}
interface ModelUndoStack {
  strResource: string;
  resourceLabel: string;
  _past: ModelUndoElement[];
  _future: ModelUndoElement[];
}
interface RenameableModel extends Monaco.editor.ITextModel {
  _undoRedoService: {
    getUriComparisonKey: (resource: Monaco.Uri) => string;
    removeElements: (resource: Monaco.Uri) => void;
    _editStacks: Map<string, ModelUndoStack>;
  };
  getInitialUndoRedoSnapshot: () => UndoSnapshot | null;
  _overwriteVersionId: (version: number) => void;
  _overwriteAlternativeVersionId: (version: number) => void;
  _overwriteInitialUndoRedoSnapshot: (snapshot: UndoSnapshot | null) => void;
}

/**
 * Monaco 0.56.0 has no public model-URI rename API. Its standalone editor keeps
 * per-model SingleModelEditStackElements, including bulk text edits. Moving
 * that exact resource stack preserves undo groups, redo and saved-version IDs
 * while language services receive the new real file URI. Keep this contract
 * aligned when upgrading the project's pinned Monaco version.
 *
 * The caller detaches and disposes the old model after restoring its view.
 */
export function renameMonacoModel(monaco: typeof Monaco, source: Monaco.editor.ITextModel, target: Monaco.Uri): Monaco.editor.ITextModel {
  const original = source as RenameableModel;
  const indexedTarget = monaco.editor.getModel(target);
  const renamed = (indexedTarget ?? monaco.editor.createModel(original.getValue(undefined, true), original.getLanguageId(), target)) as RenameableModel;
  if (indexedTarget) {
    renamed.setValue(original.getValue(undefined, true));
  }
  renamed.updateOptions(original.getOptions());
  renamed.setEOL(original.getEOL() === "\r\n" ? monaco.editor.EndOfLineSequence.CRLF : monaco.editor.EndOfLineSequence.LF);

  const service = original._undoRedoService;
  const oldKey = service.getUriComparisonKey(original.uri);
  const newKey = service.getUriComparisonKey(renamed.uri);
  // A previously closed file can leave history under the destination URI.
  // The renamed buffer owns its source history, including an empty history.
  service.removeElements(renamed.uri);
  const stack = service._editStacks.get(oldKey);
  if (stack) {
    service._editStacks.delete(oldKey);
    stack.strResource = newKey;
    stack.resourceLabel = renamed.uri.fsPath;
    for (const element of [...stack._past, ...stack._future]) {
      element.strResource = newKey;
      element.resourceLabel = renamed.uri.fsPath;
      element.strResources = [newKey];
      element.resourceLabels = [renamed.uri.fsPath];
      element.actual.setModel(renamed);
    }
    service._editStacks.set(newKey, stack);
  }
  renamed._overwriteVersionId(original.getVersionId());
  renamed._overwriteAlternativeVersionId(original.getAlternativeVersionId());
  const initial = original.getInitialUndoRedoSnapshot();
  renamed._overwriteInitialUndoRedoSnapshot(initial ? { resource: renamed.uri, elements: initial.elements } : null);
  return renamed;
}
