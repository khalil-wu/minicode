import { useEffect, useMemo, useState } from "react";
import { ExternalLink, GitBranch, GitCompare, RefreshCw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import {
  fetchWorkspaceGitStatus,
  fetchWorkspaceGitWorktree,
  removeWorkspaceGitWorktree,
  type WorkspaceGitWorktreeResponse,
} from "../protocol/workspace";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";
import { normalizeWorkspaceRoot, workspaceFilePathsEqual, workspaceRootsEqual } from "../lib/workspace-path";
import { activateWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { EmptyState } from "../components/EmptyState";
import "./GitPanel.css";

interface GitStatus {
  is_git_repo?: boolean;
  branch: string;
  modified: string[];
  staged: string[];
  untracked: string[];
  error?: string;
}

const toFileRows = (status: GitStatus | null) => {
  if (!status) return [];
  return [
    ...status.staged.map((path) => ({ path, section: "staged" as const, group: "已暂存", color: "var(--state-success)" })),
    ...status.modified.map((path) => ({ path, section: "working" as const, group: "已修改", color: "var(--state-warning)" })),
    ...status.untracked.map((path) => ({ path, section: "untracked" as const, group: "未跟踪", color: "var(--text-muted)" })),
  ];
};

export const GitPanel = ({ onEditorOpened, active = true }: { onEditorOpened?: () => void; active?: boolean } = {}) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  return <WorkspaceGitPanel key={normalizeWorkspaceRoot(workingDirectory)} workingDirectory={workingDirectory} onEditorOpened={onEditorOpened} active={active} />;
};

const WorkspaceGitPanel = ({ workingDirectory, onEditorOpened, active }: { workingDirectory: string; onEditorOpened?: () => void; active: boolean }) => {
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [worktree, setWorktree] = useState<WorkspaceGitWorktreeResponse | null>(null);
  const [selected, setSelected] = useState<{ path: string; section: "staged" | "working" | "untracked"; group: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [repoError, setRepoError] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [worktreeAction, setWorktreeAction] = useState("");
  const refresh = () => setRefreshVersion((version) => version + 1);

  useEffect(() => {
    if (!active || !workingDirectory) return;
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
      setSelected((file) => file && toFileRows(nextStatus).some((row) => row.section === file.section && workspaceFilePathsEqual(file.path, row.path, workingDirectory)) ? file : null);
    }).catch((error: unknown) => {
      if (!isCurrent()) return;
      setStatus(null);
      setWorktree(null);
      setSelected(null);
      setRepoError(error instanceof Error ? error.message : "无法读取 Git 仓库状态。");
    }).finally(() => {
      if (isCurrent()) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workingDirectory, refreshVersion, activeBottomTab, active]);

  const fileRows = useMemo(() => toFileRows(status), [status]);
  const branch = branchDisplayName(status?.branch || workspaceGit?.branch) || "无分支";
  const openReview = (file: NonNullable<typeof selected>) => {
    setSelected(file);
    useAppStore.getState().openGitReview({
      path: file.path,
      section: file.section,
      workspaceRoot: workingDirectory,
      conversationId: useAppStore.getState().conversationId,
    });
  };

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

  if (!loading && status?.is_git_repo === false && !repoError) {
    return (
      <div className="h-full grid place-items-center p-4">
        <EmptyState
          icon={<GitBranch size={20} />}
          title="当前文件夹未启用 Git"
          hint="可以继续使用文件编辑和聊天功能。"
          action={<button type="button" onClick={refresh}>刷新 Git 状态</button>}
        />
      </div>
    );
  }

  return (
    <div className="mc-git-overview h-full grid min-h-0" style={{ gridTemplateColumns: "minmax(220px, 320px) minmax(0, 1fr)" }}>
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
          <GitErrorDetails message={status.error} />
        )}
        {repoError && <GitErrorDetails message={repoError} />}

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
                onClick={() => openReview(row)}
                aria-label={`${row.group} ${row.path}`}
                title={`${row.group}: ${row.path}`}
                className="border-0 bg-transparent cursor-pointer overflow-hidden p-1 px-1.5 text-left truncate whitespace-nowrap"
                style={{
                  borderRadius: "var(--radius-sm, 4px)",
                    background: selected?.section === row.section && workspaceFilePathsEqual(selected.path, row.path, workingDirectory)
                      ? "var(--surface-active)"
                      : "transparent",
                  color: row.color,
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                }}
              >
                <span style={{ marginRight: 8, fontFamily: "var(--font-ui)", color: "var(--text-muted)" }}>{row.group}</span>{row.path}
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
            {selected ? `${selected.group} · ${selected.path}` : "工作区审阅"}
          </span>
          {selected && (
            <button
              onClick={() => {
                useAppStore.getState().openEditorFile(selected.path, selected.path.split(/[/\\]/).pop(), { exact: true });
                onEditorOpened?.();
              }}
              title="在编辑器中打开文件"
              aria-label="在编辑器中打开文件"
              className="w-6 h-6 border rounded inline-flex items-center justify-center p-0 bg-transparent cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)" }}
            >
              <ExternalLink size={14} />
            </button>
          )}
          {selected && (
            <button onClick={() => openReview(selected)} className="bg-transparent border rounded cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "2px 8px" }}>
              返回审阅
            </button>
          )}
        </div>
        <div className="flex-1 min-h-0 grid place-items-center p-4">
          <EmptyState compact icon={<GitCompare size={20} />} title={selected ? `${selected.group}差异已在审阅中打开` : "选择文件开始审阅"} hint="在审阅中查看真实行号、切换行内或分栏，并把代码行加入对话。" />
        </div>
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

const GitErrorDetails = ({ message }: { message: string }) => <div role="alert" className="mb-2.5" style={{ color: "var(--state-danger)", fontSize: "var(--text-xs)" }}>
  <div>无法读取 Git 状态。刷新后可以重新检查当前工作区。</div>
  <details style={{ marginTop: 5 }}><summary style={{ cursor: "pointer" }}>错误详情</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "6px 0", fontSize: "var(--text-xxs)" }}>{message}</pre></details>
</div>;

const badgeStyle = (color: string): React.CSSProperties => ({
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color,
  fontSize: "var(--text-xs)",
  padding: "1px 6px",
});
