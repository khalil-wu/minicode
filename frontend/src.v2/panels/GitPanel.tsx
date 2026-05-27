import { useCallback, useEffect, useMemo, useState } from "react";
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
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [worktree, setWorktree] = useState<WorkspaceGitWorktreeResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [diff, setDiff] = useState("");
  const [loading, setLoading] = useState(false);
  const [worktreeAction, setWorktreeAction] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [nextStatus, nextWorktree] = await Promise.all([
        fetchWorkspaceGitStatus(),
        fetchWorkspaceGitWorktree(),
      ]);
      setStatus(nextStatus);
      setWorktree(nextWorktree);
      const target = selectedFile || "";
      const nextDiff = await fetchWorkspaceGitDiff(target);
      setDiff(nextDiff?.diff ?? "");
    } finally {
      setLoading(false);
    }
  }, [selectedFile]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (activeBottomTab === "git") void refresh();
  }, [activeBottomTab, refresh]);

  useEffect(() => {
    if (!selectedFile) return;
    fetchWorkspaceGitDiff(selectedFile).then((result) => setDiff(result?.diff ?? ""));
  }, [selectedFile]);

  const fileRows = useMemo(() => toFileRows(status), [status]);
  const branch = branchDisplayName(status?.branch || workspaceGit?.branch) || "No branch";

  const switchWorktree = async (path: string) => {
    setWorktreeAction(path);
    try {
      const result = await switchWorkspaceGitWorktree(path);
      if (result?.success) {
        useAppStore.getState().setWorkingDirectory(result.project?.root_path || path);
        await refresh();
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
    <div style={{ height: "100%", display: "grid", gridTemplateColumns: "minmax(220px, 320px) 1fr", minHeight: 0 }}>
      <aside
        style={{
          borderRight: "1px solid var(--border-subtle)",
          overflow: "auto",
          padding: 10,
          fontSize: "var(--text-sm)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <GitBranch size={15} color="var(--accent-primary)" />
          <span title={branch} style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
            {branch}
          </span>
          <button onClick={() => void refresh()} disabled={loading} title="Refresh Git status" aria-label="Refresh Git status" style={iconButtonStyle}>
            <RefreshCw size={13} />
          </button>
        </div>

        {status?.error && (
          <div style={{ color: "var(--state-warning)", fontSize: "var(--text-xs)", marginBottom: 10 }}>
            {status.error}
          </div>
        )}

        <SectionTitle label="Changes" count={fileRows.length} />
        {fileRows.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: "4px 0 12px" }}>
            {loading ? "Loading..." : "Working tree clean"}
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 2, marginBottom: 14 }}>
            {fileRows.map((row) => (
              <button
                key={`${row.group}:${row.path}`}
                onClick={() => setSelectedFile(row.path)}
                title={`${row.group}: ${row.path}`}
                style={{
                  border: 0,
                  borderRadius: "var(--radius-sm, 4px)",
                  background: selectedFile === row.path ? "var(--surface-active)" : "transparent",
                  color: row.color,
                  cursor: "pointer",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                  overflow: "hidden",
                  padding: "4px 6px",
                  textAlign: "left",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {row.path}
              </button>
            ))}
          </div>
        )}

        <SectionTitle label="Workspaces" count={worktree?.worktrees?.length ?? workspaceGit?.worktreeCount ?? 0} />
        {worktree?.error && (
          <div style={{ color: "var(--state-warning)", fontSize: "var(--text-xs)", marginBottom: 8 }}>
            {worktree.error}
          </div>
        )}
        {worktree?.worktrees?.length ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {worktree.worktrees.map((item) => (
              <div
                key={item.path}
                title={item.path}
                style={{
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-sm, 4px)",
                  background: item.is_current ? "var(--surface-active)" : "var(--surface-soft)",
                  padding: "5px 6px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: item.is_current ? "var(--accent-primary)" : "var(--text-muted)",
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-primary)" }}>
                    {branchDisplayName(item.branch) || (item.is_detached ? "detached" : item.is_isolated ? "isolated session" : "unknown")}
                  </span>
                  <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{item.commit}</span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                  <span style={badgeStyle(item.is_current ? "var(--accent-primary)" : "var(--text-muted)")}>
                    {item.is_current ? "current" : item.is_main ? "main" : item.is_isolated ? "isolated" : "linked"}
                  </span>
                  <button
                    onClick={() => void switchWorktree(item.path)}
                    disabled={item.is_current || worktreeAction === item.path}
                    title="Switch workspace"
                    aria-label="Switch workspace"
                    style={miniButtonStyle}
                  >
                    Switch
                  </button>
                  {item.can_remove && (
                    <button
                      onClick={() => void removeWorktree(item.path, item.branch)}
                      disabled={worktreeAction === item.path}
                      title="Remove protected workspace"
                      aria-label="Remove protected workspace"
                      style={dangerIconButtonStyle}
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>
                <div style={{ marginTop: 2, color: "var(--text-muted)", fontSize: "var(--text-xs)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {workspaceDisplayName(item.path, item.is_isolated ? "isolated workspace" : "Current workspace")}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
            No linked workspaces detected.
          </div>
        )}
      </aside>

      <main style={{ minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "7px 10px",
            borderBottom: "1px solid var(--border-subtle)",
            background: "var(--surface-page)",
            fontSize: "var(--text-xs)",
          }}
        >
          <GitCompare size={14} color="var(--text-muted)" />
          <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
            {selectedFile || "Full working tree diff"}
          </span>
          {selectedFile && (
            <button
              onClick={() => useAppStore.getState().openEditorFile(selectedFile, selectedFile.split(/[/\\]/).pop())}
              title="Open file in editor"
              aria-label="Open file in editor"
              style={iconButtonStyle}
            >
              <ExternalLink size={13} />
            </button>
          )}
          {selectedFile && (
            <button onClick={() => setSelectedFile("")} style={clearButtonStyle}>
              Show all
            </button>
          )}
        </div>
        <pre
          style={{
            flex: 1,
            minHeight: 0,
            margin: 0,
            overflow: "auto",
            padding: 12,
            background: "var(--surface-base)",
            color: diff ? "var(--text-secondary)" : "var(--text-muted)",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-xs)",
            lineHeight: 1.55,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {diff || (loading ? "Loading diff..." : "No diff for this selection.")}
        </pre>
      </main>
    </div>
  );
};

const SectionTitle = ({ label, count }: { label: string; count: number }) => (
  <div
    style={{
      color: "var(--text-muted)",
      fontSize: "var(--text-xs)",
      fontWeight: 700,
      margin: "10px 0 6px",
      textTransform: "uppercase",
      letterSpacing: 0,
    }}
  >
    {label} ({count})
  </div>
);

const iconButtonStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
};

const clearButtonStyle: React.CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-muted)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  padding: "2px 8px",
};

const miniButtonStyle: React.CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-muted)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  padding: "1px 7px",
};

const dangerIconButtonStyle: React.CSSProperties = {
  ...iconButtonStyle,
  width: 22,
  height: 22,
  color: "var(--state-danger)",
};

const badgeStyle = (color: string): React.CSSProperties => ({
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color,
  fontSize: "var(--text-xs)",
  padding: "1px 6px",
});
