import { useLayoutEffect, useRef } from "react";
import type * as Monaco from "monaco-editor/editor/editor.api.js";

/** These read-only diff surfaces own their temporary original/modified models. */
export function useOwnedDiffModelCleanup(modelKey?: string): (editor: Monaco.editor.IStandaloneDiffEditor) => void {
  const editorRef = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  useLayoutEffect(() => () => {
    const editor = editorRef.current;
    const models = editor?.getModel();
    if (editor && models) {
      // Layout cleanup precedes @monaco-editor/react's passive disposal.
      editor.setModel(null);
      models.original.dispose();
      models.modified.dispose();
    }
    editorRef.current = null;
  }, [modelKey]);
  return (editor) => { editorRef.current = editor; };
}
