import type { DiffCellState, DiffFileChange } from "./cellTypes";

type DiffChangeType = NonNullable<DiffFileChange["changeType"]>;

const CREATED_PATCH_RE = /^(?:new file mode\b|---\s+\/dev\/null$)/m;
const DELETED_PATCH_RE = /^(?:deleted file mode\b|\+\+\+\s+\/dev\/null$)/m;

export function diffFileChangeType(file: DiffFileChange): DiffChangeType {
  if (file.changeType) return file.changeType;
  const patch = file.patch ?? "";
  if (DELETED_PATCH_RE.test(patch)) return "deleted";
  if (CREATED_PATCH_RE.test(patch)) return "created";
  return "updated";
}

export function diffCellTitle(cell: DiffCellState): string {
  const count = cell.summary.modifiedFiles || cell.files.length;
  const types = new Set(cell.files.map(diffFileChangeType));
  const verb =
    types.size === 1 && types.has("created")
      ? "已创建"
      : types.size === 1 && types.has("deleted")
        ? "已删除"
        : types.size === 1 && types.has("updated")
          ? "已编辑"
          : "已更改";
  return `${verb} ${count} 个文件`;
}

export function diffChangeBreakdown(files: DiffFileChange[]): Record<DiffChangeType, number> {
  return files.reduce(
    (counts, file) => {
      counts[diffFileChangeType(file)] += 1;
      return counts;
    },
    { created: 0, updated: 0, deleted: 0 } satisfies Record<DiffChangeType, number>,
  );
}

export function diffChangeBreakdownLabel(files: DiffFileChange[]): string {
  const counts = diffChangeBreakdown(files);
  return [
    counts.created ? `新建 ${counts.created}` : "",
    counts.updated ? `修改 ${counts.updated}` : "",
    counts.deleted ? `删除 ${counts.deleted}` : "",
  ].filter(Boolean).join(" · ");
}

export function diffFileChangeTypeLabel(file: DiffFileChange): string {
  switch (diffFileChangeType(file)) {
    case "created":
      return "新建";
    case "deleted":
      return "删除";
    case "updated":
    default:
      return "修改";
  }
}
