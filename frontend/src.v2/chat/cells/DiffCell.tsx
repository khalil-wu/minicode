import { useCallback, useMemo, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, FileDiff, FileSearch, RotateCcw } from "lucide-react";
import { fileIcon } from "../../shell/fileTreeHelpers";
import type { DiffCellState } from "./cellTypes";
import {
  diffCellTitle,
  diffChangeBreakdownLabel,
  diffFileChangeType,
  diffFileChangeTypeLabel,
} from "./diffCellLabels";
import { useAppStore } from "../../stores";
import { sendClientCommand } from "../../protocol/ws-outbox";

// Monotonic id so rapid clicks can't collide (Date.now() repeats within 1ms).
let diffReviewRequestSeq = 0;
const nextDiffReviewRequestId = (): string => {
  diffReviewRequestSeq += 1;
  return `diff-cell-${diffReviewRequestSeq}`;
};
import { RollingNumber } from "../../components/RollingNumber";
import { initialDiffReviewPatch } from "../diffReviewState";
import { workspaceRelativeDiffPath } from "../diffPaths";
import "./cells.css";

const COLLAPSED_FILE_LIMIT = 3;

/**
 * DiffCell file modification summary.
 *
 * Collapsed: file count + +/- stats
 * Expanded: compact file list; full diff stays in Review
 */
export function DiffCell({ cell }: { cell: DiffCellState }) {
  const [expanded, setExpanded] = useState(!cell.collapsed);
  const [showAllFiles, setShowAllFiles] = useState(false);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const files = useMemo(
    () => cell.files.map((file) => ({
      ...file,
      path: workspaceRelativeDiffPath(file.path, workingDirectory) || file.path,
    })),
    [cell.files, workingDirectory],
  );

  const shortPath = useCallback((fullPath: string): string => {
    const parts = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
    const value =
      parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
    return value.length > 60 ? `${value.slice(0, 57)}...` : value;
  }, []);

  const reviewableFiles = useMemo(
    () => files.filter((f) => f.patch),
    [files],
  );

  const openReview = () => {
    if (reviewableFiles.length === 0) return;
    const store = useAppStore.getState();
    const reviewFiles = reviewableFiles.map((f) => ({
      path: f.path,
      patch: f.patch ?? "",
      additions: f.additions,
      deletions: f.deletions,
    }));
    const selectedPath = reviewFiles[0]?.path;
    store.setDiffReviewState({
      requestId: `diff-cell-${cell.id}`,
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
    const revertable = files.filter((f) => f.path);
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
    ? files
    : files.slice(0, COLLAPSED_FILE_LIMIT);
  const hiddenCount = files.length - visibleFiles.length;
  const title = diffCellTitle(cell);
  const breakdownLabel = diffChangeBreakdownLabel(files);
  const fileSummary = useMemo(() => {
    const firstPath = files[0]?.path;
    if (!firstPath) return "";
    const first = shortPath(firstPath);
    if (files.length <= 1) return first;
    return `${first} +${files.length - 1}`;
  }, [files, shortPath]);

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
            <FileDiff size={15} />
          </span>
          <span className="diff-cell-title-block">
            <span className="diff-cell-title">{title}</span>
            <span className="diff-cell-meta-row">
              {fileSummary && (
                <span className="diff-cell-file-summary" title={cell.files.map((file) => file.path).join("\n")}>
                  {fileSummary}
                </span>
              )}
              <span className="diff-cell-stats diff-cell-header-stats">
                <RollingNumber value={cell.summary.added} prefix="+" className="diff-cell-added" />
                <RollingNumber value={cell.summary.deleted} prefix="-" className="diff-cell-removed" />
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
        <span style={{ color: "var(--text-muted)", flexShrink: 0, display: "inline-flex" }}>{fileIcon(file.path, { size: 12, className: "diff-cell-file-icon" })}</span>
        <span className="diff-cell-file-path">{shortPath(file.path)}</span>
      </button>
      <span className={`diff-cell-file-kind diff-cell-file-kind-${changeType}`}>
        {changeLabel}
      </span>
      <span className="diff-cell-stats">
        <RollingNumber value={file.additions} prefix="+" className="diff-cell-added" />
        <RollingNumber value={file.deletions} prefix="-" className="diff-cell-removed" />
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
