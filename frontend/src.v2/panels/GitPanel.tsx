import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, GitBranch, GitCompare, RefreshCw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import {
  fetchWorkspaceGitDiff,
  fetchWorkspaceGitStatus,
  fetchWorkspaceGitWorktree,
  removeWorkspaceGitWorktree,
  switchWorkspaceGitWorktree,
  type WorkspaceGitWorktreeResponse,
} from "../protocol/workspace";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";

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
    ...status.staged.map((path) => ({ path, group: "Staged", color: "var(--state-success)" })),
    ...status.modified.map((path) => ({ path, group: "Modified", color: "var(--state-warning)" })),
    ...status.untracked.map((path) => ({ path, group: "Untracked", color: "var(--text-muted)" })),
  ];
};

export const GitPanel = () => {
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [worktree, setWorktree] = useState<WorkspaceGitWorktreeResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [diff, setDiff] = useState("");
  const [loading, setLoading] = useState(false);
  const [repoError, setRepoError] = useState("");
  const [diffError, setDiffError] = useState("");
  const repoEpochRef = useRef(0);
  const diffEpochRef = useRef(0);
  const [worktreeAction, setWorktreeAction] = useState("");

  const refresh = useCallback(async () => {
    const epoch = ++repoEpochRef.current;
    const directory = workingDirectory;
    setLoading(true);
    setRepoError("");
    try {
      const [nextStatus, nextWorktree] = await Promise.all([
        fetchWorkspaceGitStatus(workingDirectory),
        fetchWorkspaceGitWorktree(workingDirectory),
      ]);
      if (epoch !== repoEpochRef.current || directory !== useAppStore.getState().workingDirectory) return;
      if (!nextStatus || !nextWorktree) {
        setRepoError("Could not load Git repository status.");
        return;
      }
      setStatus(nextStatus);
      setWorktree(nextWorktree);
    } finally {
      if (epoch === repoEpochRef.current) setLoading(false);
    }
  }, [workingDirectory]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (activeBottomTab === "git") void refresh();
  }, [activeBottomTab, refresh]);

  useEffect(() => {
    const epoch = ++diffEpochRef.current;
    const file = selectedFile;
    const directory = workingDirectory;
    setDiffError("");
    void fetchWorkspaceGitDiff(file, directory).then((result) => {
      if (epoch !== diffEpochRef.current || file !== selectedFile || directory !== useAppStore.getState().workingDirectory) return;
      if (!result) {
        setDiffError("Could not load Git diff.");
        return;
      }
      setDiff(result.diff ?? "");
    });
  }, [selectedFile, workingDirectory]);

  const fileRows = useMemo(() => toFileRows(status), [status]);
  const branch = branchDisplayName(status?.branch || workspaceGit?.branch) || "No branch";

  const switchWorktree = async (path: string) => {
    setWorktreeAction(path);
    try {
      const result = await switchWorkspaceGitWorktree(path);
      if (result?.success) {
        useAppStore.getState().setWorkingDirectory(result.project?.root_path || path);
        await refresh();
      } else {
        const { showAlert } = await import("../overlays/DialogService");
        await showAlert({ title: "Switch failed", message: result?.error || "Could not switch workspace." });
      }
    } finally {
      setWorktreeAction("");
    }
  };

  const removeWorktree = async (path: string, branchName?: string | null) => {
    const { showConfirm, showAlert } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: "Remove protected workspace",
      message: `Remove isolated session ${branchDisplayName(branchName) || workspaceDisplayName(path, "Current workspace")}?`,
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    setWorktreeAction(path);
    try {
      const result = await removeWorkspaceGitWorktree(path);
      if (!result?.removed) {
        await showAlert({ title: "Error", message: result?.error || "Failed to remove worktree." });
      }
      await refresh();
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
          <button onClick={() => void refresh()} disabled={loading} title="Refresh Git status" aria-label="Refresh Git status" className="w-6 h-6 border rounded inline-flex items-center justify-center p-0 bg-transparent cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--text-muted)" }}>
            <RefreshCw size={13} />
          </button>
        </div>

        {status?.error && (
          <div className="mb-2.5" style={{ color: "var(--state-warning)", fontSize: "var(--text-xs)" }}>
            {status.error}
          </div>
        )}
        {repoError && <div role="alert" className="mb-2.5" style={{ color: "var(--state-danger)", fontSize: "var(--text-xs)" }}>{repoError}</div>}

        <SectionTitle label="Changes" count={fileRows.length} />
        {fileRows.length === 0 ? (
          <div className="py-1 pb-3" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            {loading ? "Loading..." : repoError ? "Git status unavailable" : "Working tree clean"}
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
                  background: selectedFile === row.path ? "var(--surface-active)" : "transparent",
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

        <SectionTitle label="Workspaces" count={worktree?.worktrees?.length ?? workspaceGit?.worktreeCount ?? 0} />
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
                    {branchDisplayName(item.branch) || (item.is_detached ? "detached" : item.is_isolated ? "isolated session" : "unknown")}
                  </span>
                  <span className="font-mono" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{item.commit}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-1">
                  <span style={badgeStyle(item.is_current ? "var(--accent-primary)" : "var(--text-muted)")}>
                    {item.is_current ? "current" : item.is_main ? "main" : item.is_isolated ? "isolated" : "linked"}
                  </span>
                  <button
                    onClick={() => void switchWorktree(item.path)}
                    disabled={item.is_current || worktreeAction === item.path}
                    title="Switch workspace"
                    aria-label="Switch workspace"
                    className="bg-transparent border rounded cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "1px 7px" }}
                  >
                    Switch
                  </button>
                  {item.can_remove && (
                    <button
                      onClick={() => void removeWorktree(item.path, item.branch)}
                      disabled={worktreeAction === item.path}
                      title="Remove protected workspace"
                      aria-label="Remove protected workspace"
                      className="inline-flex items-center justify-center p-0 bg-transparent cursor-pointer border rounded" style={{ width: 22, height: 22, borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--state-danger)" }}
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>
                <div className="mt-0.5 overflow-hidden truncate whitespace-nowrap" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
                  {workspaceDisplayName(item.path, item.is_isolated ? "isolated workspace" : "Current workspace")}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            {repoError ? "Workspace list unavailable." : "No linked workspaces detected."}
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
            {selectedFile || "Full working tree diff"}
          </span>
          {selectedFile && (
            <button
              onClick={() => useAppStore.getState().openEditorFile(selectedFile, selectedFile.split(/[/\\]/).pop())}
              title="Open file in editor"
              aria-label="Open file in editor"
              className="w-6 h-6 border rounded inline-flex items-center justify-center p-0 bg-transparent cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--text-muted)" }}
            >
              <ExternalLink size={13} />
            </button>
          )}
          {selectedFile && (
            <button onClick={() => setSelectedFile("")} className="bg-transparent border rounded cursor-pointer" style={{ borderColor: "var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "2px 8px" }}>
              Show all
            </button>
          )}
        </div>
        <pre
          className="flex-1 min-h-0 m-0 overflow-auto p-3 whitespace-pre-wrap break-words"
          style={{
            background: "var(--surface-base)",
            color: diff ? "var(--text-secondary)" : "var(--text-muted)",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-xs)",
            lineHeight: 1.55,
          }}
        >
          {diffError || diff || (loading ? "Loading diff..." : "No diff for this selection.")}
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
