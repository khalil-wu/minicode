import type { DiffReviewFile } from "../stores/types";

/**
 * Initial `diff` payload for a DiffReviewState opened from a multi-file
 * change set: the selected file's patch when available, otherwise every
 * reviewable patch joined so the panel is never empty.
 */
export function initialDiffReviewPatch(
  files: DiffReviewFile[],
  selectedPath?: string,
): string {
  const selected = selectedPath
    ? files.find((file) => file.path === selectedPath)
    : files[0];
  if (selected?.patch) return selected.patch;
  return files
    .map((file) => file.patch || "")
    .filter(Boolean)
    .join("\n\n");
}
