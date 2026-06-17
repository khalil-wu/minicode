import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, FileCode2, FileSearch, RotateCcw } from "lucide-react";
import type { DiffCellState } from "../../chat/cells/cellTypes";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";

const FILE_LIMIT = 3;

export function FileChangesCard({ cell }: { cell: DiffCellState }) {
  const [expanded, setExpanded] = useState(true);
  const [showAllFiles, setShowAllFiles] = useState(false);
  const reviewableFiles = useMemo(
    () => cell.files.filter((file) => file.patch),
    [cell.files],
  );
  const visibleFiles = showAllFiles ? cell.files : cell.files.slice(0, FILE_LIMIT);
  const hiddenCount = cell.files.length - visibleFiles.length;

  const openReview = () => {
    if (reviewableFiles.length === 0) return;
    const store = useAppStore.getState();
    store.setDiffReviewState({
      requestId: `agent-loop-diff-${cell.id}`,
      toolName: "助手修改",
      diff: reviewableFiles.map((file) => file.patch).filter(Boolean).join("\n\n"),
      files: reviewableFiles.map((file) => ({
        path: file.path,
        patch: file.patch ?? "",
        additions: file.additions,
        deletions: file.deletions,
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
    const revertable = cell.files.filter((file) => file.path);
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
        <button
          type="button"
          className="agent-loop-file-card-toggle"
          aria-label={expanded ? "收起文件更改" : "展开文件更改"}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span className="agent-loop-file-icon" aria-hidden="true">
            <FileCode2 size={15} />
          </span>
          <span className="agent-loop-file-card-title">
            <span>已编辑 {cell.summary.modifiedFiles} 个文件</span>
            <span className="agent-loop-file-stats">
              <span className="agent-loop-file-added">+{cell.summary.added}</span>
              <span className="agent-loop-file-removed">-{cell.summary.deleted}</span>
            </span>
          </span>
        </button>
        <div className="agent-loop-file-actions">
          <button type="button" className="agent-loop-file-action" onClick={revertAll}>
            <RotateCcw size={12} />
            撤销
          </button>
          {reviewableFiles.length > 0 && (
            <button type="button" className="agent-loop-file-action" onClick={openReview}>
              <FileSearch size={12} />
              审核
            </button>
          )}
        </div>
      </div>

      {expanded && (
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
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function FileChangeRow({ file }: { file: DiffCellState["files"][number] }) {
  const openFile = () => {
    const store = useAppStore.getState();
    const label = file.path.split(/[/\\]/).filter(Boolean).pop() ?? file.path;
    const line = firstChangedLineFromPatch(file.patch);
    store.openEditorFile(file.path, label, line ? { line } : undefined);
  };

  const openDiff = () => {
    if (!file.patch) return;
    const store = useAppStore.getState();
    store.setDiffReviewState({
      requestId: `agent-loop-file-${Date.now()}`,
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
    <div className="agent-loop-file-row" title={file.path}>
      <button type="button" className="agent-loop-file-button" onClick={openFile}>
        <FileCode2 size={12} />
        <span>{shortPath(file.path)}</span>
      </button>
      <span className="agent-loop-file-row-stats">
        <span className="agent-loop-file-added">+{file.additions}</span>
        <span className="agent-loop-file-removed">-{file.deletions}</span>
      </span>
      {file.patch ? (
        <button type="button" className="agent-loop-file-row-action" onClick={openDiff}>
          审核
        </button>
      ) : (
        <span className="agent-loop-file-row-spacer" />
      )}
    </div>
  );
}

function shortPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const value = parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
  return value.length > 60 ? `${value.slice(0, 57)}...` : value;
}

function firstChangedLineFromPatch(patch?: string): number | undefined {
  if (!patch) return undefined;
  const match = /^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/m.exec(patch);
  if (!match) return undefined;
  const line = Number(match[1]);
  return Number.isFinite(line) && line > 0 ? line : undefined;
}
