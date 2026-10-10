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
  if (cell.source === "workspace_snapshot") return "工作区比较";
  return cell.historical ? "编辑记录" : "已编辑";
}
