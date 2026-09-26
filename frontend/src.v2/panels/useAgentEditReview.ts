/**
 * Inline agent-edit review wired to a Monaco editor.
 *
 * Reads the authoritative turn diff for the active file, locates the changed
 * blocks in the live buffer (agent-edit-review.ts), paints line decorations,
 * and exposes navigation plus per-block Keep/Undo. All block math is in the
 * pure helpers; this hook only bridges them to Monaco and the store. Blocks a
 * user keeps or undoes drop out of the set, so nothing is reverted twice.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

export interface AgentEditReviewModel {
  editor: {
    getModel?: () => {
      getLineCount: () => number;
      getLineMaxColumn: (lineNumber: number) => number;
      getEOL?: () => string;
    } | null;
    createDecorationsCollection?: (decorations: unknown[]) => { set: (d: unknown[]) => void; clear: () => void };
    executeEdits: (source: string, edits: Array<{ range: unknown; text: string; forceMoveMarkers?: boolean }>) => void;
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
  if (!turnDiff?.diff || !path) return null;
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
}: UseAgentEditReviewArgs) {
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const [cursorLine, setCursorLine] = useState(1);
  const collectionRef = useRef<{ set: (d: unknown[]) => void; clear: () => void } | null>(null);

  const patch = useMemo(
    () => patchForActiveFile(turnDiff, path, workingDirectory),
    [turnDiff, path, workingDirectory],
  );

  // Reset review bookkeeping when the file or the authoritative diff changes.
  useEffect(() => {
    setDismissed(new Set());
  }, [path, turnDiff?.turnId, turnDiff?.revision]);

  const blocks = useMemo<AgentEditBlock[]>(() => {
    if (!patch || readOnly || content === undefined) return [];
    return reviewAgentEdits(patch, content).blocks.filter((block) => !dismissed.has(block.key));
  }, [patch, content, readOnly, dismissed]);

  // Paint line + glyph decorations for the current blocks.
  useEffect(() => {
    const editor = editorRef.current;
    const create = editor?.createDecorationsCollection?.bind(editor);
    if (!editor || !create) return;
    if (!collectionRef.current) collectionRef.current = create([]);
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
    collectionRef.current.set(decorations);
    return () => {
      collectionRef.current?.clear();
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
    setDismissed((prev) => new Set(prev).add(block.key));
  }, [blocks, currentIndex]);

  const keepAll = useCallback(() => {
    setDismissed((prev) => {
      const next = new Set(prev);
      for (const block of blocks) next.add(block.key);
      return next;
    });
  }, [blocks]);

  const undo = useCallback(() => {
    const block = blocks[currentIndex];
    const editor = editorRef.current;
    const model = editor?.getModel?.();
    if (!block || !editor || !model) return;
    const eol = model.getEOL?.() ?? "\n";
    const edit = agentEditUndoEdit(block, model.getLineCount(), (line) => model.getLineMaxColumn(line), eol);
    editor.executeEdits("agent-edit-undo", [{ range: edit.range, text: edit.text, forceMoveMarkers: true }]);
    setDismissed((prev) => new Set(prev).add(block.key));
    editor.focus();
  }, [blocks, currentIndex, editorRef]);

  return {
    total: blocks.length,
    currentIndex,
    onCursorLine: setCursorLine,
    next: goNext,
    prev: goPrev,
    keep,
    keepAll,
    undo,
  };
}
