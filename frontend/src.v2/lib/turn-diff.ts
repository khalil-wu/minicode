import type { GitChangeFile, TurnDiffState } from "../stores/types";
import { countUnifiedDiffLines, parseUnifiedDiffLines, unifiedDiffFilePaths } from "./unified-diff";
import type { ChatTurnState, DiffCellState } from "../chat/cells/cellTypes";
import { diffFileChangeType } from "../chat/cells/diffCellLabels";

export interface TurnDiffSummary {
  files: GitChangeFile[];
  additions: number;
  deletions: number;
}

const DIFF_HEADER = /^diff --git a\/(.+) b\/(.+)$/;

export function summarizeTurnDiff(state: TurnDiffState | null | undefined): TurnDiffSummary | null {
  if (state?.deferred && state.files?.length) return {
    files: state.files,
    additions: state.files.reduce((sum, file) => sum + file.additions, 0),
    deletions: state.files.reduce((sum, file) => sum + file.deletions, 0),
  };
  if (!state?.diff) return null;
  const chunks = state.diff.split(/(?=^diff --git )/m).filter((chunk) => chunk.startsWith("diff --git "));
  const files: GitChangeFile[] = [];

  for (const patch of chunks) {
    const header = patch.split(/\r?\n/, 1)[0] ?? "";
    const match = DIFF_HEADER.exec(header);
    if (!match) continue;
    const paths = unifiedDiffFilePaths(parseUnifiedDiffLines(patch));
    const oldPath = paths.oldPath || match[1];
    const newPath = paths.newPath || match[2];
    const { plus: additions, minus: deletions } = countUnifiedDiffLines(patch);
    const isBinary = /^(?:Binary files |GIT binary patch)/m.test(patch);
    files.push({
      path: newPath === "/dev/null" ? oldPath : newPath,
      ...(oldPath !== newPath && oldPath !== "/dev/null" && newPath !== "/dev/null" ? { oldPath } : {}),
      patch,
      additions,
      deletions,
      isBinary,
    });
  }

  if (!files.length) return null;
  return {
    files,
    additions: files.reduce((sum, file) => sum + file.additions, 0),
    deletions: files.reduce((sum, file) => sum + file.deletions, 0),
  };
}

export const applyAuthoritativeTurnDiff = (
  turns: ChatTurnState[],
  turnDiff: TurnDiffState | undefined,
): ChatTurnState[] => {
  // An exact empty diff retracts the preview. An unavailable aggregate retains
  // the tool receipts as history, without claiming they match current files.
  if (!turnDiff?.turnId) return turns;
  const index = turns.findIndex((turn) => turn.turnId === turnDiff.turnId
    && (!turnDiff.messageId || turn.id === turnDiff.messageId));
  if (index < 0) return turns;
  const turn = turns[index];
  if (turnDiff.diff === null && !turnDiff.deferred) {
    const next = turns.slice();
    next[index] = { ...turn, committedCells: turn.committedCells.map((cell) =>
      cell.kind === "diff" ? { ...cell, historical: true } : cell,
    ) };
    return next;
  }
  const summary = summarizeTurnDiff(turnDiff);
  if (!summary) {
    const withoutFallback = turn.committedCells.filter((cell) => cell.kind !== "diff");
    if (withoutFallback.length === turn.committedCells.length) return turns;
    const next = turns.slice();
    next[index] = { ...turn, committedCells: withoutFallback };
    return next;
  }
  const fallback = turn.committedCells.find((cell): cell is DiffCellState => cell.kind === "diff");
  const authoritative: DiffCellState = {
    kind: "diff",
    id: `turn-diff-${turnDiff.turnId}`,
    status: "updated",
    source: turnDiff.source,
    truncated: turnDiff.truncated,
    ...(turnDiff.deferred && turnDiff.messageId ? { deferredDiff: {
      conversationId: turnDiff.threadId, messageId: turnDiff.messageId,
      turnId: turnDiff.turnId, revision: turnDiff.revision,
    } } : {}),
    files: summary.files.map((file) => ({
      path: file.path,
      oldPath: file.oldPath,
      patch: file.patch,
      additions: file.additions,
      deletions: file.deletions,
      changeType: file.oldPath && file.oldPath !== file.path ? "renamed"
        : diffFileChangeType(file),
      isLarge: file.additions + file.deletions > 200,
      isTruncated: turnDiff.truncated,
    })),
    summary: {
      added: summary.additions,
      deleted: summary.deletions,
      modifiedFiles: summary.files.length,
    },
    toolCallCount: fallback?.toolCallCount,
    collapsed: false,
    createdAt: fallback?.createdAt ?? turn.startedAt,
  };
  const next = turns.slice();
  next[index] = {
    ...turn,
    committedCells: [
      ...turn.committedCells.filter((cell) => cell.kind !== "diff"),
      authoritative,
    ],
  };
  return next;
};
