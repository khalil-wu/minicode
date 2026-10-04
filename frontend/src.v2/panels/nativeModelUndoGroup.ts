import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { UndoRedoGroup } from "monaco-editor/platform/undoRedo/common/undoRedo.js";

export type NativeEditModel = Pick<Monaco.editor.ITextModel, "pushEditOperations">;
type PushEditArguments = [...Parameters<NativeEditModel["pushEditOperations"]>, group?: UndoRedoGroup, reason?: unknown];
interface GroupedTextModel extends NativeEditModel {
  pushEditOperations(...args: PushEditArguments): ReturnType<NativeEditModel["pushEditOperations"]>;
}

/** Bind Monaco's native group only for the synchronous edits in this call. */
export function withNativeModelUndoGroup<T>(models: NativeEditModel[], apply: () => T): T {
  const group = new UndoRedoGroup();
  const restorations = (models as GroupedTextModel[]).map((model) => {
    const original = model.pushEditOperations;
    const descriptor = Object.getOwnPropertyDescriptor(model, "pushEditOperations");
    model.pushEditOperations = function (before, operations, cursor, _group, reason) {
      return original.call(this, before, operations, cursor, group, reason);
    };
    return () => {
      if (descriptor) Object.defineProperty(model, "pushEditOperations", descriptor);
      else Reflect.deleteProperty(model, "pushEditOperations");
    };
  });
  try {
    return apply();
  } finally {
    for (const restore of restorations) restore();
  }
}
