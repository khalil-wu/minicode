import { useEffect, useMemo, useState } from "react";
import { ExternalLink, GitBranch, GitCompare, RefreshCw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import {
  fetchWorkspaceGitDiff,
  fetchWorkspaceGitStatus,
  fetchWorkspaceGitWorktree,
  removeWorkspaceGitWorktree,
  type WorkspaceGitWorktreeResponse,
} from "../protocol/workspace";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";
import { normalizeWorkspaceRoot, workspaceFilePathsEqual, workspaceRootsEqual } from "../lib/workspace-path";
import { activateWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { parseUnifiedDiffLines } from "../lib/unified-diff";

interface GitStatus {
  branch: string;
  modified: string[];
  staged: string[];
  untracked: string[];
  error?: string;
}

const toFileRows = (status: GitStatus | null) => {
  if (!status) return [];
  return [
    ...status.staged.map((path) => ({ path, group: "已暂存", color: "var(--state-success)" })),
    ...status.modified.map((path) => ({ path, group: "已修改", color: "var(--state-warning)" })),
    ...status.untracked.map((path) => ({ path, group: "未跟踪", color: "var(--text-muted)" })),
  ];
};

const DIFF_TONE_STYLE: Record<string, { background?: string; color: string }> = {
  add: { background: "var(--diff-add-bg)", color: "var(--diff-add-text)" },
  del: { background: "var(--diff-del-bg)", color: "var(--diff-del-text)" },
  hunk: { color: "var(--accent-primary)" },
  meta: { color: "var(--text-muted)" },
  marker: { color: "var(--text-muted)" },
  context: { color: "var(--text-secondary)" },
};

export const GitPanel = () => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  return <WorkspaceGitPanel key={normalizeWorkspaceRoot(workingDirectory)} workingDirectory={workingDirectory} />;
};

const WorkspaceGitPanel = ({ workingDirectory }: { workingDirectory: string }) => {
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [worktree, setWorktree] = useState<WorkspaceGitWorktreeResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [diff, setDiff] = useState("");
  const [loading, setLoading] = useState(false);
  const [diffLoading, setDiffLoading] = useState(false);
  const [repoError, setRepoError] = useState("");
  const [diffError, setDiffError] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [worktreeAction, setWorktreeAction] = useState("");
  const refresh = () => setRefreshVersion((version) => version + 1);

  useEffect(() => {
    if (!workingDirectory) return;
    let cancelled = false;
    const isCurrent = () => !cancelled && workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory);
    setLoading(true);
    setRepoError("");
    void Promise.all([
      fetchWorkspaceGitStatus(workingDirectory),
      fetchWorkspaceGitWorktree(workingDirectory),
    ]).then(([nextStatus, nextWorktree]) => {
      if (!isCurrent()) return;
      setStatus(nextStatus);
      setWorktree(nextWorktree);
      setSelectedFile((file) => toFileRows(nextStatus).some((row) => workspaceFilePathsEqual(file, row.path, workingDirectory)) ? file : "");
    }).catch((error: unknown) => {
      if (!isCurrent()) return;
      setStatus(null);
      setWorktree(null);
      setSelectedFile("");
      setRepoError(error instanceof Error ? error.message : "无法读取 Git 仓库状态。");
    }).finally(() => {
      if (isCurrent()) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workingDirectory, refreshVersion, activeBottomTab]);

  useEffect(() => {
    if (!workingDirectory) return;
    let cancelled = false;
    const isCurrent = () => !cancelled && workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory);
    setDiff("");
    setDiffError("");
    setDiffLoading(true);
    void fetchWorkspaceGitDiff(workingDirectory, selectedFile).then((result) => {
      if (isCurrent()) setDiff(result.diff);
    }).catch((error: unknown) => {
      if (isCurrent()) setDiffError(error instanceof Error ? error.message : "无法读取 Git 差异。");
    }).finally(() => {
      if (isCurrent()) setDiffLoading(false);
    });
    return () => { cancelled = true; };
  }, [selectedFile, workingDirectory, refreshVersion, activeBottomTab]);

  const fileRows = useMemo(() => toFileRows(status), [status]);
  const branch = branchDisplayName(status?.branch || workspaceGit?.branch) || "无分支";

  const switchWorktree = async (path: string) => {
    setWorktreeAction(path);
    try {
      // The websocket activation also rebinds the conversation and its runtime.
      // The REST import endpoint only changes the process-wide default folder.
      await activateWorkspaceFolder(path);
    } finally {
      setWorktreeAction("");
    }
  };

  const removeWorktree = async (path: string, branchName?: string | null) => {
    const { showConfirm, showAlert } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: "移除隔离工作区",
      message: `确定移除 ${branchDisplayName(branchName) || workspaceDisplayName(path, "当前工作区")}？`,
      confirmLabel: "移除",
      danger: true,
    });
    if (!ok) return;
    if (!workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory)) return;
    setWorktreeAction(path);
    try {
      const result = await removeWorkspaceGitWorktree(workingDirectory, path);
      if (!result?.removed) {
        await showAlert({ title: "移除失败", message: result?.error || "无法移除工作树。" });
      }
      if (workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory)) refresh();
    } catch (error: unknown) {
      await showAlert({ title: "移除失败", message: error instanceof Error ? error.message : "无法移除工作树。" });
    } finally {
      setWorktreeAction("");
    }
  };

  return (
    <div className="h-full grid min-h-0" style={{ gridTemplateColumns: "minmax(220px, 320px) 1fr" }}>
      <aside className="border-r overflow-auto p-2.5" style={{ borderColor: "var(--border-subtle)", fontSize: "var(--text-sm)" }}>
        <div className="flex items-center gap-2 mb-2.5">
          <GitBranch size={15} color="var(--accent-primary)" />
          <span title={branch} className="flex-1 min-w-0 overflow-hidden truncate whitespace-nowrap font-mono" style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
            {branch}
          </span>
          <button onClick={() => void refresh()} disabled={loading} title="刷新 Git 状态" aria-label="刷新 Git 状态" className="w-6 h-6 border rounded inline-flex items-center justify-center p-0 bg-transparent cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)" }}>
            <RefreshCw size={14} />
          </button>
        </div>

        {status?.error && (
          <div className="mb-2.5" style={{ color: "var(--state-warning)", fontSize: "var(--text-xs)" }}>
            {status.error}
          </div>
        )}
        {repoError && <div role="alert" className="mb-2.5" style={{ color: "var(--state-danger)", fontSize: "var(--text-xs)" }}>{repoError}</div>}

        <SectionTitle label="变更" count={fileRows.length} />
        {fileRows.length === 0 ? (
          <div className="py-1 pb-3" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            {!workingDirectory ? "请先打开工作区。" : loading ? "正在加载…" : repoError ? "Git 状态不可用" : "工作树干净"}
          </div>
        ) : (
          <div className="flex flex-col gap-0.5 mb-3.5">
            {fileRows.map((row) => (
              <button
                key={`${row.group}:${row.path}`}
                onClick={() => setSelectedFile(row.path)}
                title={`${row.group}: ${row.path}`}
                className="border-0 bg-transparent cursor-pointer overflow-hidden p-1 px-1.5 text-left truncate whitespace-nowrap"
                style={{
                  borderRadius: "var(--radius-sm, 4px)",
                    background: workspaceFilePathsEqual(selectedFile, row.path, workingDirectory)
                      ? "var(--surface-active)"
                      : "transparent",
                  color: row.color,
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                }}
              >
                {row.path}
              </button>
            ))}
          </div>
        )}

        <SectionTitle label="工作区" count={worktree?.worktrees?.length ?? workspaceGit?.worktreeCount ?? 0} />
        {worktree?.error && (
          <div className="mb-2" style={{ color: "var(--state-warning)", fontSize: "var(--text-xs)" }}>
            {worktree.error}
          </div>
        )}
        {!repoError && worktree?.worktrees?.length ? (
          <div className="flex flex-col gap-1">
            {worktree.worktrees.map((item) => (
              <div
                key={item.path}
                title={item.path}
                className="border rounded p-1.5"
                style={{
                  borderColor: "var(--border-subtle)",
                  borderRadius: "var(--radius-sm, 4px)",
                  background: item.is_current ? "var(--surface-active)" : "var(--surface-soft)",
                }}
              >
                <div className="flex items-center gap-1.5">
                  <span
                    className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                    style={{
                      background: item.is_current ? "var(--accent-primary)" : "var(--text-muted)",
                    }}
                  />
                  <span className="flex-1 min-w-0 overflow-hidden truncate whitespace-nowrap" style={{ color: "var(--text-primary)" }}>
                    {branchDisplayName(item.branch) || (item.is_detached ? "游离状态" : item.is_isolated ? "隔离任务" : "未知")}
                  </span>
                  <span className="font-mono" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{item.commit}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-1">
                  <span style={badgeStyle(item.is_current ? "var(--accent-primary)" : "var(--text-muted)")}>
                    {item.is_current ? "当前" : item.is_main ? "主工作区" : item.is_isolated ? "隔离" : "已连接"}
                  </span>
                  <button
                    onClick={() => void switchWorktree(item.path)}
                    disabled={item.is_current || Boolean(worktreeAction)}
                    title="切换工作区"
                    aria-label="切换工作区"
                    className="bg-transparent border rounded cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "1px 7px" }}
                  >
                    切换
                  </button>
                  {item.can_remove && (
                    <button
                      onClick={() => void removeWorktree(item.path, item.branch)}
                      disabled={Boolean(worktreeAction)}
                      title="移除隔离工作区"
                      aria-label="移除隔离工作区"
                      className="inline-flex items-center justify-center p-0 bg-transparent cursor-pointer border rounded" style={{ width: 22, height: 22, borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--state-danger)" }}
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
                <div className="mt-0.5 overflow-hidden truncate whitespace-nowrap" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
                  {workspaceDisplayName(item.path, item.is_isolated ? "隔离工作区" : "当前工作区")}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            {repoError ? "工作区列表不可用。" : "没有检测到关联工作区。"}
          </div>
        )}
      </aside>

      <main className="min-w-0 min-h-0 flex flex-col">
        <div
          className="flex items-center gap-2 border-b"
          style={{
            padding: "7px 10px",
            borderColor: "var(--border-subtle)",
            background: "var(--surface-page)",
            fontSize: "var(--text-xs)",
          }}
        >
          <GitCompare size={14} color="var(--text-muted)" />
          <span className="flex-1 min-w-0 overflow-hidden truncate whitespace-nowrap font-mono" style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
            {selectedFile || "完整工作树差异"}
          </span>
          {selectedFile && (
            <button
              onClick={() => useAppStore.getState().openEditorFile(selectedFile, selectedFile.split(/[/\\]/).pop())}
              title="在编辑器中打开文件"
              aria-label="在编辑器中打开文件"
              className="w-6 h-6 border rounded inline-flex items-center justify-center p-0 bg-transparent cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)" }}
            >
              <ExternalLink size={14} />
            </button>
          )}
          {selectedFile && (
            <button onClick={() => setSelectedFile("")} className="bg-transparent border rounded cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "2px 8px" }}>
              显示全部
            </button>
          )}
        </div>
        <pre
          className="flex-1 min-h-0 m-0 overflow-auto p-3 whitespace-pre-wrap break-words"
          style={{
            background: "var(--surface-base)",
            color: "var(--text-muted)",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-xs)",
            lineHeight: 1.55,
          }}
        >
          {diff && !diffError
            ? parseUnifiedDiffLines(diff).map((line, index) => (
                <span
                  key={`${index}-${line.text}`}
                  style={{ display: "block", ...DIFF_TONE_STYLE[line.kind] }}
                >
                  {line.text || " "}
                </span>
              ))
            : diffError ? <span role="alert">{diffError}</span>
            : diffLoading ? "正在加载差异…" : "当前选择没有差异。"}
        </pre>
      </main>
    </div>
  );
};

const SectionTitle = ({ label, count }: { label: string; count: number }) => (
  <div
    className="font-bold uppercase"
    style={{
      color: "var(--text-muted)",
      fontSize: "var(--text-xs)",
      margin: "10px 0 6px",
      letterSpacing: 0,
    }}
  >
    {label} ({count})
  </div>
);

const badgeStyle = (color: string): React.CSSProperties => ({
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color,
  fontSize: "var(--text-xs)",
  padding: "1px 6px",
});
