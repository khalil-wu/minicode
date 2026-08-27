import type { GitChangeFile, TurnDiffState } from "../stores/types";

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
    let additions = 0;
    let deletions = 0;
    let isBinary = false;
    for (const line of patch.split(/\r?\n/)) {
      if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) isBinary = true;
      else if (line.startsWith("+++") || line.startsWith("---")) continue;
      else if (line.startsWith("+")) additions += 1;
      else if (line.startsWith("-")) deletions += 1;
    }
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

