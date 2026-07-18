import { useMemo, useState } from "react";
import { ChevronDown, Code2, Copy, FileSearch, FileText, RotateCcw } from "lucide-react";
import type { DiffCellState } from "../../chat/cells/cellTypes";
import {
  diffFileChangeType,
} from "../../chat/cells/diffCellLabels";
import { RollingNumber } from "../../components/RollingNumber";
import { useAppStore } from "../../stores";
import { workspaceRelativeDiffPath } from "../../chat/diffPaths";
import { initialDiffReviewPatch } from "../../chat/diffReviewState";
import { sendClientCommand } from "../../protocol/ws-outbox";

const FILE_LIMIT = 3;

export function FileChangesCard({ cell }: { cell: DiffCellState }) {
  const [showAllFiles, setShowAllFiles] = useState(false);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const files = useMemo(
    () => cell.files.map((file) => ({
      ...file,
      path: workspaceRelativeDiffPath(file.path, workingDirectory) || file.path,
    })),
    [cell.files, workingDirectory],
  );
  const visibleFiles = showAllFiles ? files : files.slice(0, FILE_LIMIT);
  const hiddenCount = files.length - visibleFiles.length;
  const canCollapse = showAllFiles && files.length > FILE_LIMIT;
  const reviewableFiles = useMemo(
    () => files.filter((file) => file.patch),
    [files],
  );
  const openReview = () => {
    if (reviewableFiles.length === 0) return;
    const store = useAppStore.getState();
    const reviewFiles = reviewableFiles.map((file) => ({
      path: file.path,
      patch: file.patch ?? "",
      additions: file.additions,
      deletions: file.deletions,
    }));
    const selectedPath = reviewFiles[0]?.path;
    store.setDiffReviewState({
      requestId: `agent-loop-diff-${cell.id}`,
      toolName: "助手修改",
      diff: initialDiffReviewPatch(reviewFiles, selectedPath),
      files: reviewFiles,
      selectedPath,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  const revertAll = async () => {
    const revertable = files.filter((file) => file.path);
    if (revertable.length === 0) return;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "撤销更改",
      message:
        revertable.length === 1
          ? `撤销 ${revertable[0].path} 的更改？此操作不可恢复。`
          : `撤销本次编辑中的 ${revertable.length} 个文件更改？此操作不可恢复。`,
      confirmLabel: "撤销",
      danger: true,
    });
    if (!ok) return;
    for (const file of revertable) {
      sendClientCommand({ type: "diff.git_revert_file", path: file.path });
    }
  };

  return (
    <section className="agent-loop-file-card">
      <div className="agent-loop-file-card-header">
        <div className="agent-loop-file-card-main">
          <span className="agent-loop-file-icon" aria-hidden="true">
            <FileText size={15} />
          </span>
          <span className="agent-loop-file-card-title">
            <span>{fileChangesCardTitle(cell)}</span>
            <span className="agent-loop-file-stats agent-loop-file-card-stats">
              <RollingNumber value={cell.summary.added} prefix="+" className="agent-loop-file-added" animateOnMount />
              <RollingNumber value={cell.summary.deleted} prefix="-" className="agent-loop-file-removed" animateOnMount />
            </span>
          </span>
        </div>
        <div className="agent-loop-file-actions">
          <button
            type="button"
            className="agent-loop-file-action"
            onClick={revertAll}
            title="撤销这些更改"
          >
            <RotateCcw size={12} />
            撤销
          </button>
          {reviewableFiles.length > 0 && (
            <button
              type="button"
              className="agent-loop-file-action agent-loop-file-action-primary"
              onClick={openReview}
              title="在审核面板查看更改"
            >
              <FileSearch size={12} />
              审核
            </button>
          )}
        </div>
      </div>

      <div className="agent-loop-file-list">
        {visibleFiles.map((file, index) => (
          <FileChangeRow key={file.path || index} file={file} />
        ))}
        {hiddenCount > 0 && (
          <button
            type="button"
            className="agent-loop-file-more"
            onClick={() => setShowAllFiles(true)}
          >
            再显示 {hiddenCount} 个文件
            <ChevronDown size={14} />
          </button>
        )}
        {canCollapse && (
          <button
            type="button"
            className="agent-loop-file-more"
            onClick={() => setShowAllFiles(false)}
          >
            收起文件列表
            <ChevronDown size={14} className="rotated-180" />
          </button>
        )}
      </div>
    </section>
  );
}

function FileChangeRow({ file }: { file: DiffCellState["files"][number] }) {
  const openFile = () => {
    const store = useAppStore.getState();
    store.openEditorFile(file.path, basename(file.path));
  };

  return (
    <div className="agent-loop-file-row" title={file.path}>
      <button
        type="button"
        className="agent-loop-file-button"
        onClick={openFile}
        aria-label={`打开 ${file.path}`}
      >
        <span>{shortPath(file.path)}</span>
      </button>
      <span className="agent-loop-file-row-stats">
        <RollingNumber value={file.additions} prefix="+" className="agent-loop-file-added" animateOnMount />
        <RollingNumber value={file.deletions} prefix="-" className="agent-loop-file-removed" animateOnMount />
      </span>
    </div>
  );
}

function fileChangesCardTitle(cell: DiffCellState): string {
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

export function InlineDiffPreview({
  path,
  patch,
}: {
  path: string;
  patch: string;
  additions?: number;
  deletions?: number;
}) {
  const [showAll, setShowAll] = useState(false);
  const preview = useMemo(() => buildPatchPreview(patch, showAll), [patch, showAll]);
  const copyPatch = () => {
    void navigator.clipboard?.writeText(patch);
  };

  return (
    <div className="agent-loop-file-inline-diff" aria-label={`${path} diff`}>
      <div className="agent-loop-file-inline-diff-header">
        <div className="agent-loop-file-inline-diff-title">
          <Code2 size={13} />
          <span>{shortPath(path)}</span>
          <span className="agent-loop-file-inline-diff-meta">
            {preview.totalLines} 行
          </span>
        </div>
        <div className="agent-loop-file-inline-diff-actions">
          <button
            type="button"
            className="agent-loop-file-inline-diff-copy"
            aria-label={`复制 ${path} diff`}
            title="复制 diff"
            onClick={copyPatch}
          >
            <Copy size={12} />
          </button>
        </div>
      </div>
      <div className="agent-loop-file-inline-diff-body">
        {preview.lines.map((line, index) => (
          <div
            key={`${index}-${line.oldNumber ?? ""}-${line.newNumber ?? ""}-${line.content}`}
            className="agent-loop-file-inline-diff-line"
            data-kind={line.kind}
          >
            <span className="agent-loop-file-inline-diff-gutter">
              <span>{line.oldNumber ?? ""}</span>
              <span>{line.newNumber ?? ""}</span>
            </span>
            <code className="agent-loop-file-inline-diff-code">{line.content || " "}</code>
          </div>
        ))}
        {preview.foldedCount > 0 && (
          <button
            type="button"
            className="agent-loop-file-inline-diff-folded"
            onClick={() => setShowAll(true)}
          >
            {preview.foldedCount} 行已折叠
            <ChevronDown size={12} />
          </button>
        )}
        {showAll && (
          <button
            type="button"
            className="agent-loop-file-inline-diff-folded"
            onClick={() => setShowAll(false)}
          >
            收起 diff
            <ChevronDown size={12} className="rotated-180" />
          </button>
        )}
      </div>
    </div>
  );
}

type PatchPreviewLine = {
  kind: "meta" | "hunk" | "add" | "remove" | "context";
  content: string;
  oldNumber?: number;
  newNumber?: number;
};

export function buildPatchPreview(patch: string, showAll = false): {
  lines: PatchPreviewLine[];
  totalLines: number;
  foldedCount: number;
} {
  const lines = patch.split("\n");
  const maxLines = 80;
  const visible = showAll ? lines : lines.slice(0, maxLines);
  let oldLine: number | undefined;
  let newLine: number | undefined;

  const parsed = visible.map((raw) => {
    const hunk = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(raw);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      return { kind: "hunk", content: raw } satisfies PatchPreviewLine;
    }

    if (raw.startsWith("diff --git") || raw.startsWith("index ") || raw.startsWith("--- ") || raw.startsWith("+++ ")) {
      return { kind: "meta", content: raw } satisfies PatchPreviewLine;
    }

    if (raw.startsWith("+") && !raw.startsWith("+++ ")) {
      const row = {
        kind: "add",
        content: raw,
        newNumber: newLine,
      } satisfies PatchPreviewLine;
      if (newLine !== undefined) newLine += 1;
      return row;
    }

    if (raw.startsWith("-") && !raw.startsWith("--- ")) {
      const row = {
        kind: "remove",
        content: raw,
        oldNumber: oldLine,
      } satisfies PatchPreviewLine;
      if (oldLine !== undefined) oldLine += 1;
      return row;
    }

    const row = {
      kind: "context",
      content: raw,
      oldNumber: oldLine,
      newNumber: newLine,
    } satisfies PatchPreviewLine;
    if (oldLine !== undefined) oldLine += 1;
    if (newLine !== undefined) newLine += 1;
    return row;
  });

  return {
    lines: parsed,
    totalLines: lines.length,
    foldedCount: showAll ? 0 : Math.max(0, lines.length - visible.length),
  };
}

function shortPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const value = parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
  return value.length > 60 ? `${value.slice(0, 57)}...` : value;
}

function basename(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).pop() ?? path;
}
