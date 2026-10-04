import type { CodeSelectionRange } from "../stores/types";

/** The text operations shared by the code editor and live Markdown surface. */
export interface EditorTextSurface {
  getSelection: () => CodeSelectionRange | null;
  getPosition: () => { lineNumber: number; column: number } | null;
  getModel: () => {
    uri: { path: string };
    getValueInRange: (range: unknown) => string;
    getLineCount: () => number;
    getLineMaxColumn: (lineNumber: number) => number;
    getEOL?: () => string;
    pushEOL?: (eol: 0 | 1) => void;
  } | null;
  getAction?: (id: string) => { run: () => void } | null;
  createDecorationsCollection?: (decorations: unknown[]) => { set: (decorations: unknown[]) => void; clear: () => void };
  addAction?: (descriptor: {
    id: string;
    label: string;
    contextMenuGroupId?: string;
    contextMenuOrder?: number;
    run: (editor: EditorTextSurface) => void;
  }) => unknown;
  /** eol restores the source separator together with a review edit. Native
   * Monaco uses pushEOL; the Markdown adapter carries it in one transaction. */
  executeEdits: (source: string, edits: Array<{ range: unknown; text: string; forceMoveMarkers?: boolean; eol?: string }>) => void;
  pushUndoStop?: () => unknown;
  focus: () => void;
  saveViewState?: () => unknown;
  restoreViewState?: (state: unknown) => void;
  revealLineInCenter?: (lineNumber: number) => void;
  revealPositionInCenter?: (position: { lineNumber: number; column: number }) => void;
  setPosition?: (position: { lineNumber: number; column: number }) => void;
  setSelection?: (range: CodeSelectionRange) => void;
  onDidChangeCursorPosition: (handler: (event: { position: { lineNumber: number; column: number } }) => void) => unknown;
  onDidChangeCursorSelection: (handler: () => void) => unknown;
  onDidChangeModel: (handler: () => void) => unknown;
  onDidChangeModelContent: (handler: () => void) => unknown;
  onDidDispose: (handler: () => void) => unknown;
}
