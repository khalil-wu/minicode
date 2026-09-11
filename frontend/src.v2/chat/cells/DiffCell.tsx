import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, FileDiff, RotateCcw } from "lucide-react";
import type { DiffCellState, DiffFileChange } from "./cellTypes";
import { diffCellTitle, diffFileChangeType } from "./diffCellLabels";
import { useAppStore } from "../../stores";
import { RollingNumber } from "../../components/RollingNumber";
import { workspaceRelativeDiffPath } from "../diffPaths";
import { initialDiffReviewPatch } from "../diffReviewState";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../../protocol/ws-outbox";
import { pushToast } from "../../overlays/ToastContainer";
import { showConfirm } from "../../overlays/DialogService";
import "./cells.css";

export function DiffCell({ cell, showActions = true, conversationId, workspaceRoot }: { cell: DiffCellState; showActions?: boolean; conversationId?: string; workspaceRoot?: string }) {
  const [showAllFiles, setShowAllFiles] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [reverted, setReverted] = useState(false);
  const activeWorkspace = useAppStore((state) => state.workingDirectory);
  const workingDirectory = workspaceRoot ?? activeWorkspace;
  const files = useMemo(() => cell.files.map((file) => ({
    ...file,
    path: workspaceRelativeDiffPath(file.path, workingDirectory) || file.path,
    oldPath: file.oldPath
      ? workspaceRelativeDiffPath(file.oldPath, workingDirectory) || file.oldPath
      : undefined,
  })), [cell.files, workingDirectory]);
  const visibleFiles = showAllFiles ? files : files.slice(0, 3);
  const hiddenFileCount = Math.max(0, files.length - visibleFiles.length);
  const canRevert = files.length > 0 && files.every((file) => file.patch && !file.isTruncated);

  const openDiffReview = (path?: string) => {
    const reviewableFiles = files.filter((file) => Boolean(file.patch));
    if (reviewableFiles.length === 0) return;
    const selectedPath = path ?? reviewableFiles[0].path;
    useAppStore.getState().setDiffReviewState({
      requestId: `diff-cell-${cell.id}`,
      conversationId,
      toolName: "助手修改",
      diff: initialDiffReviewPatch(reviewableFiles.map((file) => ({
        path: file.path,
        patch: file.patch ?? "",
        additions: file.additions,
        deletions: file.deletions,
      })), selectedPath),
      files: reviewableFiles.map((file) => ({
        path: file.path,
        patch: file.patch ?? "",
        additions: file.additions,
        deletions: file.deletions,
      })),
      selectedPath,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    useAppStore.getState().setRightStackTab("diff");
  };

  const revertDiffFiles = async () => {
    if (!canRevert || reverting || reverted) return;
    const ownerConversationId = conversationId ?? useAppStore.getState().conversationId;
    const patch = files.map((file) => file.patch).join("\n");
    const confirmed = await showConfirm({
      title: "撤销更改",
      message: `撤销此处显示的 ${files.length} 个文件更改？`,
      confirmLabel: "撤销",
      cancelLabel: "取消",
      danger: true,
    });
    if (!confirmed) return;
    setReverting(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "diff.git_revert_patch",
        conversation_id: ownerConversationId,
        workspace_root: workingDirectory,
        patch,
        confirmed: true,
      }, "diff.git_revert_patch");
      if (!commandResultSucceeded(result)) throw new Error(result.message || "撤销更改失败");
      setReverted(true);
      useAppStore.getState().requestGitChanges();
      pushToast("显示的更改已撤销。", "success", 3000);
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "撤销更改失败", "error", 5000);
    } finally {
      setReverting(false);
    }
  };

  return (
    <div className="diff-cell">
      <div className="diff-cell-header-row">
        <div className="diff-cell-header-button diff-cell-header-static">
          <span className="diff-cell-icon-tile" aria-hidden="true"><FileDiff size={15} /></span>
          <span className="diff-cell-heading">
            <span className="diff-cell-title">{diffCellTitle(cell)} {cell.files.length} 个文件</span>
            <span className="diff-cell-stats diff-cell-header-stats">
              <RollingNumber value={cell.summary.added} prefix="+" className="diff-cell-added" />
              <RollingNumber value={cell.summary.deleted} prefix="-" className="diff-cell-removed" />
            </span>
          </span>
        </div>
        {showActions && (
          <div className="diff-cell-header-actions">
            <button
              type="button"
              className="diff-cell-action-button diff-cell-action-button-danger"
              onClick={() => void revertDiffFiles()}
              disabled={!canRevert || reverting || reverted}
              title="撤销这些更改"
            >
              <RotateCcw size={14} aria-hidden="true" />
              <span>{reverted ? "已撤销" : reverting ? "正在撤销" : "撤销"}</span>
            </button>
            <button
              type="button"
              className="diff-cell-action-button diff-cell-action-button-accent"
              onClick={() => openDiffReview()}
              disabled={!files.some((file) => Boolean(file.patch))}
              title="在审核面板查看更改"
            >
              <span>审核</span>
            </button>
          </div>
        )}
      </div>
      <div className="diff-cell-files">
        {visibleFiles.map((file, index) => <DiffFileSection key={file.path || index} file={file} onOpen={showActions && file.patch ? () => openDiffReview(file.path) : undefined} />)}
      </div>
      {files.length > 3 && (
        <button
          type="button"
          className="diff-cell-more-files"
          aria-expanded={showAllFiles}
          onClick={() => setShowAllFiles((value) => !value)}
        >
          <span>{showAllFiles ? "收起文件列表" : `再显示 ${hiddenFileCount} 个文件`}</span>
          {showAllFiles ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
        </button>
      )}
    </div>
  );
}

function DiffFileSection({ file, onOpen }: { file: DiffFileChange; onOpen?: () => void }) {
  const changeType = diffFileChangeType(file);
  const displayPath = changeType === "renamed" && file.oldPath
    ? `${file.oldPath.split(/[\\/]/).pop()} → ${file.path.split(/[\\/]/).pop()}`
    : file.path;
  return <section className="diff-file-section">
    <div className="diff-file-section-header">
      {onOpen
        ? <button type="button" className="diff-cell-file-path" title={file.path} onClick={onOpen}>{displayPath}</button>
        : <span className="diff-cell-file-path" title={file.path}>{displayPath}</span>}
      <span className="diff-cell-stats"><RollingNumber value={file.additions} prefix="+" className="diff-cell-added" /><RollingNumber value={file.deletions} prefix="-" className="diff-cell-removed" /></span>
    </div>
  </section>;
}
