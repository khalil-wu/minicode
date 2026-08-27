import type { DiffReviewFile } from "../stores/types";
import { workspaceFilePathsEqual } from "../lib/workspace-path";

export const diffFilePathsEqual = (
  left: unknown,
  right: unknown,
  workspaceRoot: unknown = "",
): boolean => workspaceFilePathsEqual(left, right, workspaceRoot);

export const diffFileDecisionForPath = (
  decisions: Record<string, "approved" | "rejected"> | undefined,
  path: string,
  workspaceRoot: unknown = "",
): "approved" | "rejected" | undefined => {
  if (!decisions) return undefined;
  return Object.entries(decisions).find(([candidate]) =>
    diffFilePathsEqual(candidate, path, workspaceRoot),
  )?.[1];
};

export function initialDiffReviewPatch(
  files: DiffReviewFile[],
  selectedPath?: string,
  workspaceRoot: unknown = "",
): string {
  const selected = selectedPath
    ? files.find((file) => diffFilePathsEqual(file.path, selectedPath, workspaceRoot))
    : files[0];
  return selected?.patch || files.find((file) => file.patch)?.patch || "";
}
