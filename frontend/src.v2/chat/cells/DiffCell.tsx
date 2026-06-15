import { useCallback, useMemo, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, FileCode2, FileSearch, GitBranch, RotateCcw } from "lucide-react";
import type { DiffCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import { sendClientCommand } from "../../protocol/ws-outbox";
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
      toolName: "assistant changes",
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
      title: "Revert changes",
      message:
        revertable.length === 1
          ? `Discard changes to ${revertable[0].path}? This cannot be undone.`
          : `Discard changes to all ${revertable.length} files in this edit? This cannot be undone.`,
      confirmLabel: "Revert",
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

  return (
    <div className="diff-cell">
      <div className="diff-cell-header-row">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="diff-cell-header-button"
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <GitBranch size={13} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          <span className="diff-cell-title">
            Edited {cell.summary.modifiedFiles} {cell.summary.modifiedFiles === 1 ? "file" : "files"}
          </span>
          <span className="diff-cell-stats">
            <span className="diff-cell-added">
              +{cell.summary.added}
            </span>{" "}
            <span className="diff-cell-removed">
              -{cell.summary.deleted}
            </span>
          </span>
        </button>
        <div className="diff-cell-header-actions">
          <button
            type="button"
            onClick={revertAll}
            className="diff-cell-action-button diff-cell-action-button-danger"
            title="Revert these changes"
          >
            <RotateCcw size={12} />
            Revert
          </button>
          {reviewableFiles.length > 0 && (
            <button
              type="button"
              onClick={openReview}
              className="diff-cell-action-button diff-cell-action-button-accent"
              title="Review changes in the diff panel"
            >
              <FileSearch size={12} />
              Review
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
              Show {hiddenCount} more {hiddenCount === 1 ? "file" : "files"}
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
      requestId: `diff-cell-${Date.now()}`,
      toolName: "diff preview",
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
        aria-label={`Open ${file.path}`}
        title={`Open ${file.path}`}
      >
        <FileCode2 size={12} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
        <span className="diff-cell-file-path">{shortPath(file.path)}</span>
      </button>
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
          aria-label={`Review ${file.path}`}
          title={`Review ${file.path}`}
        >
          Review
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
