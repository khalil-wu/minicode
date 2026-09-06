import type { GitChangeFile, TurnDiffState } from "../stores/types";
import { countUnifiedDiffLines } from "./unified-diff";

export interface TurnDiffSummary {
  files: GitChangeFile[];
  additions: number;
  deletions: number;
}

const DIFF_HEADER = /^diff --git a\/(.+) b\/(.+)$/;

export function summarizeTurnDiff(state: TurnDiffState | null | undefined): TurnDiffSummary | null {
  if (!state?.diff) return null;
  const chunks = state.diff.split(/(?=^diff --git )/m).filter((chunk) => chunk.startsWith("diff --git "));
  const files: GitChangeFile[] = [];

  for (const patch of chunks) {
    const header = patch.split(/\r?\n/, 1)[0] ?? "";
    const match = DIFF_HEADER.exec(header);
    if (!match) continue;
    const oldPath = match[1];
    const newPath = match[2];
    const { plus: additions, minus: deletions } = countUnifiedDiffLines(patch);
    const isBinary = /^(?:Binary files |GIT binary patch)/m.test(patch);
    files.push({
      path: newPath === "/dev/null" ? oldPath : newPath,
      ...(oldPath !== newPath ? { oldPath } : {}),
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
