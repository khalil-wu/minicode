import { useCallback, useMemo, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, FileCode2, FileSearch, RotateCcw } from "lucide-react";
import type { DiffCellState } from "./cellTypes";
import {
  diffCellTitle,
  diffChangeBreakdownLabel,
  diffFileChangeType,
  diffFileChangeTypeLabel,
} from "./diffCellLabels";
import { useAppStore } from "../../stores";
import { sendClientCommand } from "../../protocol/ws-outbox";
import "./cells.css";

const COLLAPSED_FILE_LIMIT = 3;

// Stable, monotonically increasing id source for diff-review requests.
// Date.now() collides on fast/concurrent clicks (same millisecond), which lets
// a stale review response overwrite a newer one. A counter is always unique.
let diffReviewRequestSeq = 0;
const nextDiffReviewRequestId = (): string => {
  diffReviewRequestSeq += 1;
  return `diff-cell-${diffReviewRequestSeq}`;
};

/**
 * DiffCell file modification summary.
 *
 * Collapsed: file count + +/- stats
 * Expanded: compact file list; full diff stays in Review
 */
export function DiffCell({ cell }: { cell: DiffCellState }) {
  const [expanded, setExpanded] = useState(!cell.collapsed);
  const [showAllFiles, setShowAllFiles] = useState(false);

  const shortPath = useCallback((fullPath: string): string => {
    const parts = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
    const value =
      parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
    return value.length > 60 ? `${value.slice(0, 57)}...` : value;
  }, []);

  const reviewableFiles = useMemo(
    () => cell.files.filter((f) => f.patch),
    [cell.files],
  );

  const openReview = () => {
    if (reviewableFiles.length === 0) return;
    const store = useAppStore.getState();
    store.setDiffReviewState({
      requestId: `diff-cell-${cell.id}`,
      toolName: "助手修改",
      diff: reviewableFiles.map((f) => f.patch).filter(Boolean).join("\n\n"),
      files: reviewableFiles.map((f) => ({
        path: f.path,
        patch: f.patch ?? "",
        additions: f.additions,
        deletions: f.deletions,
      })),
      selectedPath: reviewableFiles[0]?.path,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  const revertAll = async () => {
    const revertable = cell.files.filter((f) => f.path);
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

  const visibleFiles = showAllFiles
    ? cell.files
    : cell.files.slice(0, COLLAPSED_FILE_LIMIT);
  const hiddenCount = cell.files.length - visibleFiles.length;
  const title = diffCellTitle(cell);
  const breakdownLabel = diffChangeBreakdownLabel(cell.files);

  return (
    <div className="diff-cell">
      <div className="diff-cell-header-row">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="diff-cell-header-button"
          aria-expanded={expanded}
          aria-label={expanded ? "收起文件更改" : "展开文件更改"}
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="diff-cell-icon-tile" aria-hidden="true">
            <FileCode2 size={15} />
          </span>
          <span className="diff-cell-title-block">
            <span className="diff-cell-title">{title}</span>
            <span className="diff-cell-meta-row">
              <span className="diff-cell-stats diff-cell-header-stats">
                <span className="diff-cell-added">
                  +{cell.summary.added}
                </span>{" "}
                <span className="diff-cell-removed">
                  -{cell.summary.deleted}
                </span>
              </span>
              {breakdownLabel && (
                <span className="diff-cell-breakdown">{breakdownLabel}</span>
              )}
            </span>
          </span>
        </button>
        <div className="diff-cell-header-actions">
          <button
            type="button"
            onClick={revertAll}
            className="diff-cell-action-button diff-cell-action-button-danger"
            title="撤销这些更改"
          >
            <RotateCcw size={12} />
            撤销
          </button>
          {reviewableFiles.length > 0 && (
            <button
              type="button"
              onClick={openReview}
              className="diff-cell-action-button diff-cell-action-button-accent"
              title="在审核面板查看更改"
            >
              <FileSearch size={12} />
              审核
            </button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="diff-cell-file-list">
          {visibleFiles.map((f, i) => (
            <DiffFileRow key={f.path || i} file={f} shortPath={shortPath} />
          ))}
          {hiddenCount > 0 && (
            <button
              type="button"
              onClick={() => setShowAllFiles(true)}
              className="diff-cell-show-more"
            >
              再显示 {hiddenCount} 个文件
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function DiffFileRow({
  file,
  shortPath,
}: {
  file: DiffCellState["files"][number];
  shortPath: (p: string) => string;
}) {
  const changeType = diffFileChangeType(file);
  const changeLabel = diffFileChangeTypeLabel(file);

  const openFile = () => {
    const store = useAppStore.getState();
    const label = file.path.split(/[/\\]/).filter(Boolean).pop() ?? file.path;
    const line = firstChangedLineFromPatch(file.patch);
    store.openEditorFile(file.path, label, line ? { line } : undefined);
  };

  const openFullDiff = () => {
    if (!file.patch) return;
    const store = useAppStore.getState();
    store.setDiffReviewState({
      requestId: nextDiffReviewRequestId(),
      toolName: "差异预览",
      diff: file.patch,
      files: [{
        path: file.path,
        patch: file.patch,
        additions: file.additions,
        deletions: file.deletions,
      }],
      selectedPath: file.path,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  return (
    <div className="diff-cell-file-row" title={file.path}>
      <button
        type="button"
        onClick={openFile}
        className="diff-cell-file-button"
        aria-label={`打开 ${file.path}`}
        title={`打开 ${file.path}`}
      >
        <FileCode2 size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
        <span className="diff-cell-file-path">{shortPath(file.path)}</span>
      </button>
      <span className={`diff-cell-file-kind diff-cell-file-kind-${changeType}`}>
        {changeLabel}
      </span>
      <span className="diff-cell-stats">
        <span className="diff-cell-added">
          +{file.additions}
        </span>{" "}
        <span className="diff-cell-removed">
          -{file.deletions}
        </span>
      </span>
      {file.patch ? (
        <button
          type="button"
          onClick={openFullDiff}
          className="diff-cell-file-action"
          aria-label={`审核 ${file.path}`}
          title={`审核 ${file.path}`}
        >
          审核
        </button>
      ) : (
        <span style={{ width: 40, height: 1 }} />
      )}
    </div>
  );
}

function firstChangedLineFromPatch(patch?: string): number | undefined {
  if (!patch) return undefined;
  const match = /^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/m.exec(patch);
  if (!match) return undefined;
  const line = Number(match[1]);
  return Number.isFinite(line) && line > 0 ? line : undefined;
}
