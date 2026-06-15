import { useEffect, useMemo, useState, useCallback, useRef } from "react";
import {
  FilePlus2,
  FolderOpen,
  FolderPlus,
  MoreHorizontal,
  RefreshCw,
  Search,
} from "lucide-react";
import {
  listWorkspaceTree,
  writeWorkspaceFile,
  createWorkspaceDirectory,
  searchWorkspaceFiles,
  type WorkspaceTreeNode,
} from "../protocol/workspace";
import { useAppStore } from "../stores";
import { isDesktop, fsListTree, fsSearchFiles, desktop, trustWorkspace } from "../desktop/runtime";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import {
  type GitStatus,
  type ContextMenuState,
  fileTreeRootStyle,
  fileTreeHeaderStyle,
  fileTreeRootLabelStyle,
  fileTreeToolbarStyle,
  fileTreeListStyle,
  fileTreeSearchStyle,
  fileTreeSearchInputStyle,
  fileTreeIconButtonStyle,
  toolbarMenuWrapStyle,
  toolbarMenuStyle,
  toolbarMenuItemStyle,
  fileTreeEmptyStyle,
  openWorkspaceButtonStyle,
  refreshBtn,
} from "./fileTreeTypes";
import {
  isMissingWorkspaceError,
  isHiddenSearchResult,
  visibleChildren,
  filteredChildren,
  readExpandedPaths,
  writeExpandedPaths,
  sortNodes,
  nodesFromEntries,
  entriesToTree,
  replaceNodeChildren,
  workspaceLabel,
  joinWorkspacePath,
  normalizeDesktopExpandedPaths,
  normalizeChangePath,
  parentTreePath,
  countVisibleNodes,
} from "./fileTreeHelpers";
import { TreeNode } from "./FileTreeNode";
import { FileContextMenu } from "./FileTreeContextMenu";
import { SearchResultRow } from "./FileTreeSearchResult";

export const FileTree = () => {
  const [tree, setTree] = useState<WorkspaceTreeNode | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingPaths, setLoadingPaths] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<{ path: string; name: string; score?: number; kind?: "file" | "folder" }[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [toolbarMenuOpen, setToolbarMenuOpen] = useState(false);
  const density = "compact" as const;
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const fileTreeVersion = useAppStore((s) => s.fileTreeVersion);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => {
    const stored = readExpandedPaths(workingDirectory || ".");
    return isDesktop() && workingDirectory ? normalizeDesktopExpandedPaths(workingDirectory, stored) : stored;
  });
  const fileChanges = useAppStore((s) => s.fileChanges);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const requestGitChanges = useAppStore((s) => s.requestGitChanges);
  const lastChangeCount = useRef(0);
  const gitMap = useMemo(() => {
    const next = new Map<string, GitStatus>();
    for (const file of gitChanges.workingTree) {
      const patch = file.patch ?? "";
      next.set(file.path, patch.includes("deleted file mode") ? "deleted" : "modified");
    }
    for (const path of gitChanges.untracked) next.set(path, "untracked");
    for (const file of gitChanges.staged) next.set(file.path, "staged");
    return next;
  }, [gitChanges]);

  const refresh = useCallback(async () => {
    if (!workingDirectory) {
      setTree(null);
      setError("");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      if (isDesktop()) {
        const rootPath = workingDirectory;
        await trustWorkspace(rootPath);
        const entries = await fsListTree(rootPath);
        let nextTree = entriesToTree(entries, rootPath, workspaceLabel(rootPath));
        const persistedExpanded = Array.from(normalizeDesktopExpandedPaths(rootPath, readExpandedPaths(rootPath)))
          .filter((path) => path !== rootPath)
          .sort((a, b) => a.split(/[/\\]/).length - b.split(/[/\\]/).length);
        for (const path of persistedExpanded) {
          try {
            nextTree = replaceNodeChildren(nextTree, path, nodesFromEntries(await fsListTree(path)));
          } catch {
            /* Ignore folders that disappeared since the last session. */
          }
        }
        setTree(nextTree);
      } else {
        const result = await listWorkspaceTree(".");
        if (result) {
          let nextTree: WorkspaceTreeNode = { ...result, children: sortNodes(result.children ?? []) };
          const persistedExpanded = Array.from(readExpandedPaths(workingDirectory || "."))
            .filter((path) => path !== "." && path !== workingDirectory)
            .sort((a, b) => a.split(/[/\\]/).length - b.split(/[/\\]/).length);
          for (const path of persistedExpanded) {
            try {
              const node = await listWorkspaceTree(path);
              if (node) nextTree = replaceNodeChildren(nextTree, path, sortNodes(node.children ?? []));
            } catch {
              /* Ignore folders that disappeared since the last session. */
            }
          }
          setTree(nextTree);
        } else {
          setError("Could not load file tree");
        }
      }
    } catch (err) {
      if (workingDirectory && isMissingWorkspaceError(err)) {
        setTree(null);
        setError(`Workspace folder is missing: ${workingDirectory}`);
      } else {
        setError(err instanceof Error ? err.message : "Could not load file tree");
      }
    } finally {
      setLoading(false);
    }
  }, [workingDirectory]);

  const loadDirectory = useCallback(async (path: string) => {
    setLoadingPaths((current) => new Set(current).add(path));
    try {
      const children = isDesktop()
        ? nodesFromEntries(await fsListTree(path))
        : visibleChildren(await listWorkspaceTree(path) ?? { name: path, path, is_dir: true, children: [] });
      setTree((current) => current ? replaceNodeChildren(current, path, sortNodes(children)) : current);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load file tree");
    } finally {
      setLoadingPaths((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
    }
  }, []);

  const refreshChangedPaths = useCallback(async (changes: { path: string; event: string; timestamp: number }[]) => {
    const parents = Array.from(new Set(changes.map((change) => parentTreePath(change.path, workingDirectory))));
    if (!parents.length) return;
    try {
      await Promise.all(parents.map((parent) => (
        parent === "." || parent === normalizeChangePath(workingDirectory || ".")
          ? refresh()
          : loadDirectory(parent)
      )));
    } catch {
      await refresh();
    }
  }, [loadDirectory, refresh, workingDirectory]);

  useEffect(() => { refresh(); }, [refresh, workingDirectory, fileTreeVersion]);
  useEffect(() => {
    const stored = readExpandedPaths(workingDirectory || ".");
    setExpandedPaths(isDesktop() && workingDirectory ? normalizeDesktopExpandedPaths(workingDirectory, stored) : stored);
  }, [workingDirectory]);

  const toggleExpanded = useCallback((path: string, shouldLoad = false) => {
    setExpandedPaths((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      writeExpandedPaths(workingDirectory || ".", next);
      return next;
    });
    if (shouldLoad) void loadDirectory(path);
  }, [loadDirectory, workingDirectory]);

  const pendingChangesRef = useRef<{ path: string; event: string; timestamp: number }[]>([]);

  useEffect(() => {
    if (fileChanges.length > lastChangeCount.current) {
      const changes = fileChanges.slice(lastChangeCount.current);
      lastChangeCount.current = fileChanges.length;
      pendingChangesRef.current = [...pendingChangesRef.current, ...changes];
      const timer = window.setTimeout(() => {
        const batch = pendingChangesRef.current;
        pendingChangesRef.current = [];
        void refreshChangedPaths(batch);
      }, 80);
      return () => clearTimeout(timer);
    }
  }, [fileChanges, refreshChangedPaths]);

  useEffect(() => { if (workingDirectory) requestGitChanges(); }, [requestGitChanges, workingDirectory]);

  useEffect(() => {
    if (!toolbarMenuOpen) return;
    const close = () => setToolbarMenuOpen(false);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", close); };
  }, [toolbarMenuOpen]);

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) { setSearchResults([]); setSearchLoading(false); return; }
    setSearchLoading(true);
    const timer = window.setTimeout(() => {
      const search = isDesktop()
        ? fsSearchFiles(workingDirectory || "", trimmed, 60, "all")
        : searchWorkspaceFiles(trimmed, 60, "all");
      search
        .then((results) => setSearchResults(results.filter((result) => !isHiddenSearchResult(result))))
        .catch(() => setSearchResults([]))
        .finally(() => setSearchLoading(false));
    }, 160);
    return () => { window.clearTimeout(timer); };
  }, [query, workingDirectory]);

  const createFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const path = await showPrompt({ title: "New file", message: "File path:", placeholder: "src/example.ts" });
    if (!path) return;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.writeFile(targetPath, "");
      else if (!(await writeWorkspaceFile(targetPath, ""))) return;
      void refresh();
    } catch { await showAlert({ title: "Error", message: `Could not create ${path}` }); }
  };

  const createFolder = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const path = await showPrompt({ title: "New folder", message: "Folder path:", placeholder: "src/components" });
    if (!path) return;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.createDirectory(targetPath);
      else if (!(await createWorkspaceDirectory(targetPath))) return;
      void refresh();
    } catch { await showAlert({ title: "Error", message: `Could not create ${path}` }); }
  };

  if (loading && !tree) {
    return <div style={{ padding: 12, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>Loading...</div>;
  }

  if (error && !tree) {
    const isMissingWorkspace = /workspace folder is missing/i.test(error);
    return (
      <div style={{ padding: 12, fontSize: "var(--text-sm)" }}>
        <div style={{ color: "var(--text-muted)", marginBottom: 8 }}>{error}</div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {isDesktop() && <button onClick={() => void openWorkspaceFolder()} style={refreshBtn}>Open folder</button>}
          {!isMissingWorkspace && <button onClick={refresh} style={refreshBtn}>Retry</button>}
        </div>
      </div>
    );
  }

  if (!tree) {
    return (
      <div style={fileTreeEmptyStyle}>
        No workspace folder is open.
        {isDesktop() && (
          <button type="button" onClick={() => void openWorkspaceFolder()} style={openWorkspaceButtonStyle}>Open code folder</button>
        )}
      </div>
    );
  }

  const hasQuery = query.trim().length > 0;
  const children = hasQuery ? [] : filteredChildren(tree, query);
  const visibleCount = hasQuery ? searchResults.length : countVisibleNodes(children, expandedPaths, query);

  const renderToolbarMenu = () => (
    <div style={toolbarMenuWrapStyle}>
      <button type="button" title="More file actions" aria-label="More file actions"
        onClick={(event) => { event.stopPropagation(); setToolbarMenuOpen((open) => !open); }}
        style={fileTreeIconButtonStyle}>
        <MoreHorizontal size={13} />
      </button>
      {toolbarMenuOpen && (
        <div style={toolbarMenuStyle} onClick={(event) => event.stopPropagation()}>
          <button type="button" onClick={() => { setToolbarMenuOpen(false); void createFile(); }} style={toolbarMenuItemStyle}>
            <FilePlus2 size={13} /><span>New file</span>
          </button>
          <button type="button" onClick={() => { setToolbarMenuOpen(false); void createFolder(); }} style={toolbarMenuItemStyle}>
            <FolderPlus size={13} /><span>New folder</span>
          </button>
        </div>
      )}
    </div>
  );

  return (
    <div style={fileTreeRootStyle}>
      <div style={fileTreeHeaderStyle}>
        <div title={workingDirectory || tree.path} style={fileTreeRootLabelStyle}>
          {workspaceLabel(workingDirectory || tree.path)}
        </div>
        <span style={{ flex: 1 }} />
        {isDesktop() && (
          <button type="button" title="Open folder" aria-label="Open folder" onClick={() => void openWorkspaceFolder()} style={fileTreeIconButtonStyle}>
            <FolderOpen size={13} />
          </button>
        )}
        <button type="button" title="Refresh files" aria-label="Refresh files" onClick={() => void refresh()} style={fileTreeIconButtonStyle}>
          <RefreshCw size={13} />
        </button>
      </div>
      <div style={fileTreeToolbarStyle}>
        <div style={fileTreeSearchStyle}>
          <Search size={12} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search files" style={fileTreeSearchInputStyle} />
        </div>
        <button type="button" title="Refresh files" aria-label="Refresh files" onClick={() => void refresh()} style={fileTreeIconButtonStyle}>
          <RefreshCw size={13} />
        </button>
        {renderToolbarMenu()}
      </div>
      <div role="tree" aria-label="File explorer" style={fileTreeListStyle}>
      {hasQuery ? (
        searchLoading ? (
          <div style={fileTreeEmptyStyle}>Searching workspace...</div>
        ) : searchResults.length > 0 ? (
          searchResults.map((result) => (
            <SearchResultRow
              key={result.path}
              result={result}
              gitMap={gitMap}
              activeEditorPath={activeEditorPath}
              workingDirectory={workingDirectory}
              onContextMenu={setContextMenu}
            />
          ))
        ) : (
          <div style={fileTreeEmptyStyle}>No files match "{query.trim()}".</div>
        )
      ) : children.length > 0 ? (
        children.map((node) => (
          <TreeNode
            key={node.path}
            node={node}
            depth={0}
            gitMap={gitMap}
            loadingPaths={loadingPaths}
            expandedPaths={expandedPaths}
            query={query}
            workingDirectory={workingDirectory}
            activeEditorPath={activeEditorPath}
            density={density}
            onToggleExpanded={toggleExpanded}
            onContextMenu={setContextMenu}
          />
        ))
      ) : !hasQuery ? (
        <div style={fileTreeEmptyStyle}>
          {workingDirectory ? "Empty workspace" : "Open a workspace folder to browse files."}
          {!workingDirectory && isDesktop() && (
            <button type="button" onClick={() => void openWorkspaceFolder()} style={openWorkspaceButtonStyle}>Open folder</button>
          )}
        </div>
      ) : null}
      {contextMenu && (
        <FileContextMenu
          menu={contextMenu}
          workingDirectory={workingDirectory}
          onRefresh={() => void refresh()}
          onClose={() => setContextMenu(null)}
        />
      )}
      </div>
    </div>
  );
};
