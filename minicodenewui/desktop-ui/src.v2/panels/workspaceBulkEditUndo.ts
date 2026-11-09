import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { IBulkEditService, ResourceTextEdit, type BulkEditService } from "monaco-editor/editor/browser/services/bulkEditService.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { withNativeModelUndoGroup } from "./nativeModelUndoGroup";

/**
 * Monaco 0.56.0 validates and applies standalone bulk edits synchronously, but
 * omits TextModel's UndoRedoGroup argument. Bind one group for the duration of
 * that native call so a workspace rename is one Undo/Redo across its sources.
 * Native version checks, text-only checks and stack boundaries stay intact.
 * The ownership callback selects the workspace's editable source resources.
 */
export function installWorkspaceBulkEditUndo(monaco: typeof Monaco, ownsResource: (uri: Monaco.Uri) => boolean): Monaco.IDisposable {
  const service = StandaloneServices.get<BulkEditService>(IBulkEditService);
  const originalApply = service.apply;
  service.apply = function (editsIn, options) {
    const edits = Array.isArray(editsIn) ? editsIn : editsIn.edits;
    const resourceEdits = edits.filter(ResourceTextEdit.is);
    if (resourceEdits.length !== edits.length || resourceEdits.some((edit) => !ownsResource(edit.resource))) {
      return originalApply.call(this, editsIn, options);
    }
    const models = [...new Set(resourceEdits.map((edit) => monaco.editor.getModel(edit.resource)))];
    if (models.length < 2 || models.includes(null)) return originalApply.call(this, editsIn, options);

    // apply has no await in 0.56.0: the binding ends before Promise users
    // can make a separate edit with its own history.
    return withNativeModelUndoGroup(models as Monaco.editor.ITextModel[], () => originalApply.call(this, editsIn, options));
  };
  return { dispose: () => { service.apply = originalApply; } };
}
