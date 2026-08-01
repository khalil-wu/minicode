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
import type { FileTreeRevealRequest } from "../stores/types";
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
  isPathInsideTreeRoot,
  isSameTreePath,
  normalizeDesktopExpandedPaths,
  normalizeChangePath,
  parentTreePath,
  countVisibleNodes,
  hasLoadedDirectoryNode,
} from "./fileTreeHelpers";
import { TreeNode } from "./FileTreeNode";
import { FileContextMenu } from "./FileTreeContextMenu";
import { SearchResultRow } from "./FileTreeSearchResult";

export const FileTree = ({ onNavigate }: { onNavigate?: () => void }) => {
  const [tree, setTree] = useState<WorkspaceTreeNode | null>(null);
  const [isListScrolling, setIsListScrolling] = useState(false);
  const scrollFadeTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (scrollFadeTimerRef.current !== null) window.clearTimeout(scrollFadeTimerRef.current);
  }, []);
  const [loading, setLoading] = useState(false);
  const [slowLoading, setSlowLoading] = useState(false);
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
  const fileTreeRevealRequests = useAppStore((s) => s.fileTreeRevealRequests);
  const consumeFileTreeRevealRequest = useAppStore((s) => s.consumeFileTreeRevealRequest);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const listRef = useRef<HTMLDivElement | null>(null);
  const restoredScrollKeyRef = useRef<string | null>(null);
  const scrollKey = `minicode.file-tree.scroll:${workingDirectory || "."}`;
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => {
    const stored = readExpandedPaths(workingDirectory || ".");
    return isDesktop() && workingDirectory ? normalizeDesktopExpandedPaths(workingDirectory, stored) : stored;
  });
  const fileChanges = useAppStore((s) => s.fileChanges);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const requestGitChanges = useAppStore((s) => s.requestGitChanges);
  const lastChangeCount = useRef(0);
  const refreshEpochRef = useRef(0);
  const searchEpochRef = useRef(0);
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
    const epoch = ++refreshEpochRef.current;
    const directory = workingDirectory;
    const isCurrent = () => epoch === refreshEpochRef.current && directory === useAppStore.getState().workingDirectory;
    if (!workingDirectory) {
      setTree(null);
      setError("");
      setLoading(false);
      return;
    }
    setLoading(true);
    setSlowLoading(false);
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
        const retainedExpanded = new Set<string>();
        for (const path of persistedExpanded) {
          if (!hasLoadedDirectoryNode(nextTree, path)) continue;
          try {
            nextTree = replaceNodeChildren(nextTree, path, nodesFromEntries(await fsListTree(path)));
            retainedExpanded.add(path);
          } catch {
            /* Ignore folders that disappeared since the last session. */
          }
        }
        if (retainedExpanded.size !== persistedExpanded.length) {
          writeExpandedPaths(rootPath, retainedExpanded);
          setExpandedPaths(retainedExpanded);
        }
        if (isCurrent()) setTree(nextTree);
      } else {
        const result = await listWorkspaceTree(workingDirectory, ".");
        if (result) {
          let nextTree: WorkspaceTreeNode = { ...result, children: sortNodes(result.children ?? []) };
          const persistedExpanded = Array.from(readExpandedPaths(workingDirectory || "."))
            .filter((path) => path !== "." && path !== workingDirectory)
            .sort((a, b) => a.split(/[/\\]/).length - b.split(/[/\\]/).length);
          for (const path of persistedExpanded) {
            try {
              const node = await listWorkspaceTree(workingDirectory, path);
              if (node) nextTree = replaceNodeChildren(nextTree, path, sortNodes(node.children ?? []));
            } catch {
              /* Ignore folders that disappeared since the last session. */
            }
          }
          if (isCurrent()) setTree(nextTree);
        } else {
          if (isCurrent()) setError("Could not load file tree");
        }
      }
    } catch (err) {
      if (!isCurrent()) return;
      if (workingDirectory && isMissingWorkspaceError(err)) {
        setTree(null);
        setError(`Workspace folder is missing: ${workingDirectory}`);
      } else {
        setError(err instanceof Error ? err.message : "Could not load file tree");
      }
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [workingDirectory]);

  useEffect(() => {
    if (!loading) {
      setSlowLoading(false);
      return;
    }
    const id = window.setTimeout(() => setSlowLoading(true), 3500);
    return () => window.clearTimeout(id);
  }, [loading]);

  const loadDirectory = useCallback(async (path: string) => {
    setLoadingPaths((current) => new Set(current).add(path));
    try {
      const children = isDesktop()
        ? nodesFromEntries(await fsListTree(path))
        : visibleChildren(await listWorkspaceTree(workingDirectory, path) ?? { name: path, path, is_dir: true, children: [] });
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
  }, [workingDirectory]);

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

  useEffect(() => {
    if (!tree || loading || !listRef.current || restoredScrollKeyRef.current === scrollKey) return;
    restoredScrollKeyRef.current = scrollKey;
    let savedScrollTop = 0;
    try {
      savedScrollTop = Number(localStorage.getItem(scrollKey) || 0);
    } catch { /* noop */ }
    const frame = window.requestAnimationFrame(() => {
      if (listRef.current) listRef.current.scrollTop = Number.isFinite(savedScrollTop) ? savedScrollTop : 0;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [loading, scrollKey, tree]);

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

  const normalizeRevealFolderPath = useCallback((rawPath: string): string | null => {
    const normalized = normalizeChangePath(rawPath).replace(/^\.\/+/, "");
    if (!normalized || !workingDirectory) return null;
    if (isDesktop()) {
      const target = joinWorkspacePath(workingDirectory, normalized);
      return isPathInsideTreeRoot(workingDirectory, target) ? target : null;
    }
    if (/^[A-Za-z]:\//.test(normalized) || normalized.startsWith("/")) return null;
    return normalized;
  }, [workingDirectory]);

  const folderRevealChain = useCallback((target: string): string[] => {
    if (isDesktop()) {
      const root = normalizeChangePath(workingDirectory);
      const normalizedTarget = normalizeChangePath(target);
      if (!root || !isPathInsideTreeRoot(root, normalizedTarget) || isSameTreePath(root, normalizedTarget)) return [];
      const relative = normalizedTarget.slice(root.length).replace(/^\/+/, "");
      const parts = relative.split("/").filter(Boolean);
      const chain: string[] = [];
      let current = root;
      for (const part of parts) {
        current = `${current.replace(/\/+$/, "")}/${part}`;
        chain.push(current);
      }
      return chain;
    }
    const parts = normalizeChangePath(target).replace(/^\.\/+/, "").split("/").filter(Boolean);
    const chain: string[] = [];
    let current = "";
    for (const part of parts) {
      current = current ? `${current}/${part}` : part;
      chain.push(current);
    }
    return chain;
  }, [workingDirectory]);

  const scrollToTreePath = useCallback((path: string) => {
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>("[data-tree-path]") ?? []);
    const row = rows.find((item) => isSameTreePath(item.dataset.treePath, path));
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    row.focus({ preventScroll: true });
  }, []);

  const revealFolderRequest = useCallback(async (request: FileTreeRevealRequest): Promise<boolean> => {
    const target = normalizeRevealFolderPath(request.path);
    if (!target) return true;
    setQuery("");
    const chain = folderRevealChain(target);
    const foldersToExpand = chain.length ? chain : [target];
    setExpandedPaths((current) => {
      const next = new Set(current);
      for (const folder of foldersToExpand) next.add(folder);
      writeExpandedPaths(workingDirectory || ".", next);
      return next;
    });
    for (const folder of foldersToExpand) {
      if (!isSameTreePath(folder, workingDirectory)) {
        await loadDirectory(folder);
      }
    }
    window.setTimeout(() => scrollToTreePath(target), 80);
    return true;
  }, [folderRevealChain, loadDirectory, normalizeRevealFolderPath, scrollToTreePath, workingDirectory]);

  const pendingChangesRef = useRef<{ path: string; event: string; timestamp: number }[]>([]);

  useEffect(() => {
    if (!tree || loading || fileTreeRevealRequests.length === 0) return;
    let cancelled = false;
    const run = async () => {
      for (const request of fileTreeRevealRequests) {
        const consumed = await revealFolderRequest(request);
        if (!cancelled && consumed) consumeFileTreeRevealRequest(request.id);
      }
    };
    void run();
    return () => { cancelled = true; };
  }, [
    consumeFileTreeRevealRequest,
    fileTreeRevealRequests,
    loading,
    revealFolderRequest,
    tree,
  ]);

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
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("click", close);
    window.addEventListener("keydown", closeOnEscape);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", closeOnEscape); };
  }, [toolbarMenuOpen]);

  useEffect(() => {
    const trimmed = query.trim();
    const epoch = ++searchEpochRef.current;
    const directory = workingDirectory;
    if (!trimmed) { setSearchResults([]); setSearchLoading(false); return; }
    setSearchLoading(true);
    const timer = window.setTimeout(() => {
      const search = isDesktop()
        ? fsSearchFiles(workingDirectory || "", trimmed, 60, "all")
        : searchWorkspaceFiles(workingDirectory, trimmed, 60, "all");
      search
        .then((results) => {
          if (epoch === searchEpochRef.current && directory === useAppStore.getState().workingDirectory) {
            setSearchResults(results.filter((result) => !isHiddenSearchResult(result)));
          }
        })
        .catch(() => {
          if (epoch === searchEpochRef.current) setSearchResults([]);
        })
        .finally(() => {
          if (epoch === searchEpochRef.current) setSearchLoading(false);
        });
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
      else if (!(await writeWorkspaceFile(targetPath, "", workingDirectory))) {
        await showAlert({ title: "Error", message: `Could not create ${path}` });
        return;
      }
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
      else if (!(await createWorkspaceDirectory(targetPath, workingDirectory))) {
        await showAlert({ title: "Error", message: `Could not create ${path}` });
        return;
      }
      void refresh();
    } catch { await showAlert({ title: "Error", message: `Could not create ${path}` }); }
  };

  if (loading && !tree) {
    return (
      <div style={{ padding: 12, color: "var(--text-muted)", fontSize: "var(--text-sm)", display: "grid", gap: 8 }}>
        <div>{slowLoading ? "Still loading file tree..." : "Loading file tree..."}</div>
        {slowLoading && (
          <>
            <div style={{ fontSize: "var(--text-xs)", lineHeight: 1.45 }}>
              Large workspaces can take longer on the first scan. You can wait, retry, or switch to a smaller folder.
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={refresh} style={refreshBtn}>重试</button>
              {isDesktop() && <button onClick={() => void openWorkspaceFolder()} style={refreshBtn}>Switch folder</button>}
            </div>
          </>
        )}
      </div>
    );
  }

  if (error && !tree) {
    const isMissingWorkspace = /workspace folder is missing/i.test(error);
    return (
      <div style={{ padding: 12, fontSize: "var(--text-sm)" }}>
        <div style={{ color: "var(--text-muted)", marginBottom: 8 }}>{error}</div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {isDesktop() && <button onClick={() => void openWorkspaceFolder()} style={refreshBtn}>打开文件夹</button>}
          {!isMissingWorkspace && <button onClick={refresh} style={refreshBtn}>重试</button>}
        </div>
      </div>
    );
  }

  if (!tree) {
    return null;
  }

  const hasQuery = query.trim().length > 0;
  const children = hasQuery ? [] : filteredChildren(tree, query);
  const visibleCount = hasQuery ? searchResults.length : countVisibleNodes(children, expandedPaths, query);

  const renderToolbarMenu = () => (
    <div style={toolbarMenuWrapStyle}>
      <button type="button" title="更多文件操作" aria-label="更多文件操作"
        aria-controls="file-tree-actions-menu" aria-expanded={toolbarMenuOpen}
        onClick={(event) => { event.stopPropagation(); setToolbarMenuOpen((open) => !open); }}
        className="mc-icon-button mc-icon-button-compact file-tree-action">
        <MoreHorizontal size={14} />
      </button>
      {toolbarMenuOpen && (
        <div id="file-tree-actions-menu" style={toolbarMenuStyle} onClick={(event) => event.stopPropagation()}>
          {isDesktop() && (
            <button type="button" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void openWorkspaceFolder(); }} style={toolbarMenuItemStyle}>
              <FolderOpen size={14} /><span>打开文件夹</span>
            </button>
          )}
          <button type="button" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void createFile(); }} style={toolbarMenuItemStyle}>
            <FilePlus2 size={14} /><span>新建文件</span>
          </button>
          <button type="button" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void createFolder(); }} style={toolbarMenuItemStyle}>
            <FolderPlus size={14} /><span>新建文件夹</span>
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
      </div>
      <div style={fileTreeToolbarStyle}>
        <div style={fileTreeSearchStyle}>
          <Search size={14} aria-hidden="true" />
          <input aria-label="搜索工作区文件" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文件" style={fileTreeSearchInputStyle} />
        </div>
        <button type="button" title="刷新文件" aria-label="刷新文件" onClick={() => void refresh()} disabled={loading} className="mc-icon-button mc-icon-button-compact file-tree-action">
          <RefreshCw size={14} className={loading ? "animate-spin" : undefined} />
        </button>
        {renderToolbarMenu()}
      </div>
      <div
        ref={listRef}
        role="tree"
        aria-label="文件资源管理器"
        className={`file-tree-scroll${isListScrolling ? " is-scrolling" : ""}`}
        style={fileTreeListStyle}
        onScroll={(event) => {
          setIsListScrolling(true);
          if (scrollFadeTimerRef.current !== null) window.clearTimeout(scrollFadeTimerRef.current);
          scrollFadeTimerRef.current = window.setTimeout(() => setIsListScrolling(false), 650);
          try {
            localStorage.setItem(scrollKey, String(event.currentTarget.scrollTop));
          } catch { /* noop */ }
        }}
      >
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
              onNavigate={onNavigate}
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
            onNavigate={onNavigate}
          />
        ))
      ) : !hasQuery ? (
        <div style={fileTreeEmptyStyle}>
          {workingDirectory ? "工作区为空" : "打开一个工作区文件夹以浏览文件。"}
          {!workingDirectory && isDesktop() && (
            <button type="button" onClick={() => void openWorkspaceFolder()} style={openWorkspaceButtonStyle}>打开文件夹</button>
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
