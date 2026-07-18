import type { DiffReviewFile } from "../stores/types";

export function initialDiffReviewPatch(
  files: DiffReviewFile[],
  selectedPath?: string,
): string {
  const selected = selectedPath
    ? files.find((file) => file.path === selectedPath)
    : files[0];
  return selected?.patch || files.find((file) => file.patch)?.patch || "";
}
