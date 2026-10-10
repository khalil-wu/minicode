/**
 * Inline agent-edit review wired to a Monaco editor.
 *
 * Reads the authoritative turn diff for the active file, locates the changed
 * blocks in the live buffer (agent-edit-review.ts), paints line decorations,
 * and exposes navigation plus per-block Keep/Undo. All block math is in the
 * pure helpers; this hook only bridges them to Monaco and the store. Blocks a
 * user keeps or undoes drop out of the set, so nothing is reverted twice.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  agentEditUndoEdit,
  reviewAgentEdits,
  type AgentEditBlock,
} from "../lib/agent-edit-review";
import {
  buildAgentEditDecorations,
  blockAnchorLine,
  currentAgentEditBlockIndex,
  nextAgentEditBlockIndex,
  previousAgentEditBlockIndex,
} from "../lib/agent-edit-review-placement";
import { summarizeTurnDiff } from "../lib/turn-diff";
import type { TurnDiffState } from "../stores/types";
import { workspaceFilePathsEqual } from "../lib/workspace-path";
import { withNativeModelUndoGroup, type NativeEditModel } from "./nativeModelUndoGroup";
import { useAppStore } from "../stores";
import { agentEditReviewScope } from "../stores/shared-helpers";

const NO_KEPT_BLOCKS: string[] = [];

export interface AgentEditReviewModel {
  editor: {
    getModel?: () => {
      getLineCount: () => number;
      getLineMaxColumn: (lineNumber: number) => number;
      getEOL?: () => string;
      pushEOL?: (eol: 0 | 1) => void;
      pushEditOperations?: NativeEditModel["pushEditOperations"];
    } | null;
    createDecorationsCollection?: (decorations: unknown[]) => { set: (d: unknown[]) => void; clear: () => void };
    executeEdits: (source: string, edits: Array<{ range: unknown; text: string; forceMoveMarkers?: boolean; eol?: string }>) => void;
    pushUndoStop?: () => unknown;
    setPosition?: (position: { lineNumber: number; column: number }) => void;
    revealLineInCenter?: (lineNumber: number) => void;
    focus: () => void;
  } | null;
}

interface UseAgentEditReviewArgs {
  editorRef: { current: AgentEditReviewModel["editor"] };
  path: string | undefined;
  content: string | undefined;
  readOnly: boolean | undefined;
  turnDiff: TurnDiffState | undefined;
  workingDirectory: string;
  /** Bumped when the editor mounts so decorations attach to a fresh instance. */
  editorEpoch: number;
  conversationId?: string | null;
}

const LINE_CLASS: Record<string, string> = {
  add: "agent-edit-line-add",
  delete: "agent-edit-line-delete",
  replace: "agent-edit-line-replace",
};
const GLYPH_CLASS: Record<string, string> = {
  add: "agent-edit-glyph-add",
  delete: "agent-edit-glyph-delete",
  replace: "agent-edit-glyph-replace",
};

const patchForActiveFile = (
  turnDiff: TurnDiffState | undefined,
  path: string | undefined,
  workingDirectory: string,
): string | null => {
  if (!turnDiff?.diff || !path || turnDiff.source === "workspace_snapshot" || turnDiff.truncated) return null;
  const summary = summarizeTurnDiff(turnDiff);
  if (!summary) return null;
  const file = summary.files.find((entry) => workspaceFilePathsEqual(entry.path, path, workingDirectory));
  return file?.patch ?? null;
};

export function useAgentEditReview({
  editorRef,
  path,
  content,
  readOnly,
  turnDiff,
  workingDirectory,
  editorEpoch,
  conversationId,
}: UseAgentEditReviewArgs) {
  const scope = agentEditReviewScope(workingDirectory, conversationId ?? turnDiff?.threadId ?? "", turnDiff?.turnId ?? "", path ?? "");
  const kept = useAppStore((state) => state.agentEditReviewKept[scope] ?? NO_KEPT_BLOCKS);
  const keepBlocks = useAppStore((state) => state.keepAgentEditBlocks);
  const dismissed = useMemo(() => new Set(kept), [kept]);
  const [undone, setUndone] = useState<Set<string>>(() => new Set());
  const [cursorLine, setCursorLine] = useState(1);
  useEffect(() => setUndone(new Set()), [scope]);

  const patch = useMemo(
    () => patchForActiveFile(turnDiff, path, workingDirectory),
    [turnDiff, path, workingDirectory],
  );

  const blocks = useMemo<AgentEditBlock[]>(() => {
    if (!patch || readOnly || content === undefined) return [];
    return reviewAgentEdits(patch, content).blocks.filter((block) => !dismissed.has(block.key) && !undone.has(block.key));
  }, [patch, content, readOnly, dismissed, undone]);

  // Paint line + glyph decorations for the current blocks.
  useEffect(() => {
    const editor = editorRef.current;
    const create = editor?.createDecorationsCollection?.bind(editor);
    if (!editor || !create) return;
    const decorations = buildAgentEditDecorations(blocks).map((decoration) => ({
      range: {
        startLineNumber: decoration.startLine,
        startColumn: 1,
        endLineNumber: decoration.endLine,
        endColumn: 1,
      },
      options: {
        isWholeLine: true,
        className: LINE_CLASS[decoration.kind],
        glyphMarginClassName: GLYPH_CLASS[decoration.kind],
        overviewRuler: { color: "var(--accent-primary)", position: 4 },
      },
    }));
    const collection = create(decorations);
    return () => {
      collection.clear();
    };
  }, [editorRef, blocks, editorEpoch]);

  const revealBlock = useCallback((index: number) => {
    const block = blocks[index];
    const editor = editorRef.current;
    if (!block || !editor) return;
    const line = blockAnchorLine(block);
    editor.setPosition?.({ lineNumber: line, column: 1 });
    editor.revealLineInCenter?.(line);
    editor.focus();
    setCursorLine(line);
  }, [blocks, editorRef]);

  const goNext = useCallback(() => revealBlock(nextAgentEditBlockIndex(blocks, cursorLine)), [blocks, cursorLine, revealBlock]);
  const goPrev = useCallback(() => revealBlock(previousAgentEditBlockIndex(blocks, cursorLine)), [blocks, cursorLine, revealBlock]);

  const currentIndex = useMemo(() => currentAgentEditBlockIndex(blocks, cursorLine), [blocks, cursorLine]);

  const keep = useCallback(() => {
    const block = blocks[currentIndex];
    if (!block) return;
    keepBlocks(scope, [block.key]);
  }, [blocks, currentIndex, keepBlocks, scope]);

  const keepAll = useCallback(() => {
    keepBlocks(scope, blocks.map((block) => block.key));
  }, [blocks, keepBlocks, scope]);

  const undo = useCallback(() => {
    const block = blocks[currentIndex];
    const editor = editorRef.current;
    const model = editor?.getModel?.();
    if (!block || !editor || !model) return;
    const eol = model.getEOL?.() ?? "\n";
    const edit = agentEditUndoEdit(block, model.getLineCount(), (line) => model.getLineMaxColumn(line), eol);
    const applyEdit = () => editor.executeEdits("agent-edit-undo", [{ range: edit.range, text: edit.text, forceMoveMarkers: true, eol: block.removedEol }]);
    editor.pushUndoStop?.();
    if (block.removedEol && block.removedEol !== eol && model.pushEOL && model.pushEditOperations) {
      withNativeModelUndoGroup([model as NativeEditModel], () => {
        // Monaco 0.56.0 stores byte offsets per stack element. An EOL change
        // and text edit need separate elements in one native group, otherwise
        // Redo interprets LF offsets in the previous CRLF document.
        model.pushEditOperations!([], [], () => []);
        model.pushEOL!(block.removedEol === "\r\n" ? 1 : 0);
        editor.pushUndoStop?.();
        applyEdit();
      });
    } else applyEdit();
    editor.pushUndoStop?.();
    setUndone((previous) => new Set(previous).add(block.key));
    editor.focus();
  }, [blocks, currentIndex, editorRef]);

  return {
    total: blocks.length,
    currentIndex,
    currentLine: blocks[currentIndex] ? blockAnchorLine(blocks[currentIndex]) : null,
    reveal: () => revealBlock(currentIndex),
    onCursorLine: setCursorLine,
    next: goNext,
    prev: goPrev,
    keep,
    keepAll,
    undo,
  };
}
