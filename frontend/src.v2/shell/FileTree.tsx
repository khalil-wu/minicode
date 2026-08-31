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
import { isDesktop, fsListTree, fsSearchFiles, trustWorkspace } from "../desktop/runtime";
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
import {
  normalizeWorkspaceRoot,
  workspaceFilePathComparisonKey,
  workspaceRootsEqual,
} from "../lib/workspace-path";

export const FileTree = ({ onNavigate }: { onNavigate?: () => void }) => {
  const [tree, setTree] = useState<WorkspaceTreeNode | null>(null);
  const [isListScrolling, setIsListScrolling] = useState(false);
  const scrollFadeTimerRef = useRef<number | null>(null);
  const scrollPersistTimerRef = useRef<number | null>(null);
  const scrollPersistValueRef = useRef(0);
  const scrollPersistKeyRef = useRef("");

  useEffect(() => () => {
    if (scrollFadeTimerRef.current !== null) window.clearTimeout(scrollFadeTimerRef.current);
    // Flush a pending scroll-position write so a fast unmount cannot lose the
    // last scroll offset for the workspace it belongs to.
    if (scrollPersistTimerRef.current !== null) {
      window.clearTimeout(scrollPersistTimerRef.current);
      scrollPersistTimerRef.current = null;
      try {
        localStorage.setItem(scrollPersistKeyRef.current, String(scrollPersistValueRef.current));
      } catch { /* noop */ }
    }
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
  const toolbarTriggerRef = useRef<HTMLButtonElement | null>(null);
  const toolbarMenuRef = useRef<HTMLDivElement | null>(null);
  const density = "compact" as const;
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const fileTreeVersion = useAppStore((s) => s.fileTreeVersion);
  const fileTreeRevealRequests = useAppStore((s) => s.fileTreeRevealRequests);
  const consumeFileTreeRevealRequest = useAppStore((s) => s.consumeFileTreeRevealRequest);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const listRef = useRef<HTMLDivElement | null>(null);
  const restoredScrollKeyRef = useRef<string | null>(null);
  const scrollWorkspaceKey = normalizeWorkspaceRoot(workingDirectory) || ".";
  const scrollKey = `minicode.file-tree.scroll:${scrollWorkspaceKey}`;
  const legacyScrollKey = `minicode.file-tree.scroll:${workingDirectory || "."}`;
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
    const treePath = (path: string) => workspaceFilePathComparisonKey(
      isDesktop() ? joinWorkspacePath(workingDirectory, path) : path,
      workingDirectory,
    );
    for (const file of gitChanges.workingTree) {
      const patch = file.patch ?? "";
      next.set(treePath(file.path), patch.includes("deleted file mode") ? "deleted" : "modified");
    }
    for (const path of gitChanges.untracked) next.set(treePath(path), "untracked");
    for (const file of gitChanges.staged) next.set(treePath(file.path), "staged");
    return next;
  }, [gitChanges, workingDirectory]);

  const refresh = useCallback(async () => {
    const directory = workingDirectory;
    if (!workspaceRootsEqual(directory, useAppStore.getState().workingDirectory)) return;
    const epoch = ++refreshEpochRef.current;
    const isCurrent = () =>
      epoch === refreshEpochRef.current
      && workspaceRootsEqual(directory, useAppStore.getState().workingDirectory);
    setLoadingPaths(new Set());
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
          .filter((path) => !isSameTreePath(path, rootPath, rootPath))
          .sort((a, b) => a.split(/[/\\]/).length - b.split(/[/\\]/).length);
        // Fetch every persisted expanded directory concurrently; the sequential
        // loop used to serialize N+1 round trips on the startup path.
        const settled = await Promise.all(
          persistedExpanded.map((path) =>
            hasLoadedDirectoryNode(nextTree, path)
              ? fsListTree(path)
                .then((children): [string, WorkspaceTreeNode] => [path, { name: path, path, is_dir: true, children: nodesFromEntries(children) }])
                .catch(() => null)
              : Promise.resolve(null),
          ),
        );
        const retainedExpanded = new Set<string>();
        for (const loaded of settled) {
          if (!loaded) continue;
          const [path, node] = loaded;
          nextTree = replaceNodeChildren(nextTree, path, node.children ?? []);
          retainedExpanded.add(path);
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
            .filter((path) => path !== "." && !isSameTreePath(path, workingDirectory, workingDirectory))
            .sort((a, b) => a.split(/[/\\]/).length - b.split(/[/\\]/).length);
          const settled = await Promise.all(
            persistedExpanded.map((path) =>
              listWorkspaceTree(workingDirectory, path)
                .then((node): [string, WorkspaceTreeNode] | null => (node ? [path, node] : null))
                .catch(() => null),
            ),
          );
          for (const loaded of settled) {
            if (!loaded) continue;
            const [path, node] = loaded;
            nextTree = replaceNodeChildren(nextTree, path, sortNodes(node.children ?? []));
          }
          if (isCurrent()) setTree(nextTree);
        } else {
          if (isCurrent()) setError("无法加载文件列表");
        }
      }
    } catch (err) {
      if (!isCurrent()) return;
      if (workingDirectory && isMissingWorkspaceError(err)) {
        setTree(null);
        setError(`工作区文件夹不存在：${workingDirectory}`);
      } else {
        setError(err instanceof Error ? err.message : "无法加载文件列表");
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
    const directory = workingDirectory;
    const epoch = refreshEpochRef.current;
    const isCurrent = () =>
      epoch === refreshEpochRef.current
      && workspaceRootsEqual(directory, useAppStore.getState().workingDirectory);
    if (!directory || !isCurrent()) return;
    setLoadingPaths((current) => new Set(current).add(path));
    try {
      const children = isDesktop()
        ? nodesFromEntries(await fsListTree(path))
        : visibleChildren(await listWorkspaceTree(directory, path) ?? { name: path, path, is_dir: true, children: [] });
      setTree((current) => (
        isCurrent() && current
          ? replaceNodeChildren(current, path, sortNodes(children))
          : current
      ));
    } catch (err) {
      if (isCurrent()) setError(err instanceof Error ? err.message : "无法加载文件列表");
    } finally {
      setLoadingPaths((current) => {
        if (!isCurrent()) return current;
        const next = new Set(current);
        next.delete(path);
        return next;
      });
    }
  }, [workingDirectory]);

  const refreshChangedPaths = useCallback(async (changes: { path: string; event: string; timestamp: number }[]) => {
    const parents = Array.from(new Set(changes.map((change) => parentTreePath(
      isDesktop() ? joinWorkspacePath(workingDirectory, change.path) : change.path,
      workingDirectory,
    ))));
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
      const canonicalValue = localStorage.getItem(scrollKey);
      const savedValue = canonicalValue
        ?? (legacyScrollKey !== scrollKey ? localStorage.getItem(legacyScrollKey) : null);
      savedScrollTop = Number(savedValue || 0);
      if (canonicalValue == null && savedValue != null) {
        localStorage.setItem(scrollKey, savedValue);
      }
    } catch { /* noop */ }
    const frame = window.requestAnimationFrame(() => {
      if (listRef.current) listRef.current.scrollTop = Number.isFinite(savedScrollTop) ? savedScrollTop : 0;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [legacyScrollKey, loading, scrollKey, tree]);

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
      if (!root || !isPathInsideTreeRoot(root, normalizedTarget) || isSameTreePath(root, normalizedTarget, workingDirectory)) return [];
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
    const row = rows.find((item) => isSameTreePath(item.dataset.treePath, path, workingDirectory));
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    row.focus({ preventScroll: true });
  }, [workingDirectory]);

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
      if (!isSameTreePath(folder, workingDirectory, workingDirectory)) {
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
      }, 300);
      return () => clearTimeout(timer);
    }
  }, [fileChanges, refreshChangedPaths]);

  useEffect(() => { if (workingDirectory) requestGitChanges(); }, [requestGitChanges, workingDirectory]);

  useEffect(() => {
    if (!toolbarMenuOpen) return;
    const toolbarMenu = toolbarMenuRef.current;
    const close = () => {
      setToolbarMenuOpen(false);
      toolbarTriggerRef.current?.focus();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const focusFirst = () => {
      window.setTimeout(() => toolbarMenu?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus(), 0);
    };
    focusFirst();
    const onMenuKeyDown = (event: KeyboardEvent) => {
      const items = Array.from(toolbarMenu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
      if (!items.length) return;
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const next = event.key === "ArrowDown" ? (current + 1 + items.length) % items.length : (current - 1 + items.length) % items.length;
        items[next].focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        items[event.key === "Home" ? 0 : items.length - 1].focus();
      }
    };
    window.addEventListener("click", close);
    window.addEventListener("keydown", closeOnEscape);
    toolbarMenu?.addEventListener("keydown", onMenuKeyDown);
    return () => { window.removeEventListener("click", close); window.removeEventListener("keydown", closeOnEscape); toolbarMenu?.removeEventListener("keydown", onMenuKeyDown); };
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
          if (
            epoch === searchEpochRef.current
            && workspaceRootsEqual(directory, useAppStore.getState().workingDirectory)
          ) {
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
    const path = await showPrompt({ title: "新建文件", message: "输入相对工作区的文件路径", placeholder: "src/example.ts" });
    if (!path) return;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (!(await writeWorkspaceFile(targetPath, "", workingDirectory))) {
        await showAlert({ title: "创建失败", message: `无法创建文件：${path}` });
        return;
      }
      void refresh();
    } catch { await showAlert({ title: "创建失败", message: `无法创建文件：${path}` }); }
  };

  const createFolder = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const path = await showPrompt({ title: "新建文件夹", message: "输入相对工作区的文件夹路径", placeholder: "src/components" });
    if (!path) return;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (!(await createWorkspaceDirectory(targetPath, workingDirectory))) {
        await showAlert({ title: "创建失败", message: `无法创建文件夹：${path}` });
        return;
      }
      void refresh();
    } catch { await showAlert({ title: "创建失败", message: `无法创建文件夹：${path}` }); }
  };

  if (loading && !tree) {
    return (
      <div style={{ padding: 12, color: "var(--text-muted)", fontSize: "var(--text-sm)", display: "grid", gap: 8 }}>
        <div>{slowLoading ? "文件较多，仍在加载…" : "正在加载文件…"}</div>
        {slowLoading && (
          <>
            <div style={{ fontSize: "var(--text-xs)", lineHeight: 1.45 }}>
              大型工作区首次扫描会稍慢。你可以继续等待、重试，或切换到较小的文件夹。
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={refresh} style={refreshBtn}>重试</button>
              {isDesktop() && <button onClick={() => void openWorkspaceFolder()} style={refreshBtn}>切换文件夹</button>}
            </div>
          </>
        )}
      </div>
    );
  }

  if (error && !tree) {
    const isMissingWorkspace = /workspace folder is missing|工作区文件夹不存在/i.test(error);
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
        ref={toolbarTriggerRef}
        aria-controls="file-tree-actions-menu" aria-expanded={toolbarMenuOpen} aria-haspopup="menu"
        onClick={(event) => { event.stopPropagation(); setToolbarMenuOpen((open) => !open); }}
        className="mc-icon-button mc-icon-button-compact file-tree-action">
        <MoreHorizontal size={14} />
      </button>
      {toolbarMenuOpen && (
        <div id="file-tree-actions-menu" ref={toolbarMenuRef} className="mc-dropdown-menu" role="menu" style={toolbarMenuStyle} onClick={(event) => event.stopPropagation()}>
          {isDesktop() && (
            <button type="button" role="menuitem" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void openWorkspaceFolder(); }} style={toolbarMenuItemStyle}>
              <FolderOpen size={14} /><span>打开文件夹</span>
            </button>
          )}
          <button type="button" role="menuitem" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void createFile(); }} style={toolbarMenuItemStyle}>
            <FilePlus2 size={14} /><span>新建文件</span>
          </button>
          <button type="button" role="menuitem" className="btn-ghost" onClick={() => { setToolbarMenuOpen(false); void createFolder(); }} style={toolbarMenuItemStyle}>
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
          // localStorage writes are synchronous; persist at most once per
          // 200ms instead of on every scroll tick.
          scrollPersistValueRef.current = event.currentTarget.scrollTop;
          scrollPersistKeyRef.current = scrollKey;
          if (scrollPersistTimerRef.current === null) {
            scrollPersistTimerRef.current = window.setTimeout(() => {
              scrollPersistTimerRef.current = null;
              try {
                localStorage.setItem(scrollKey, String(scrollPersistValueRef.current));
              } catch { /* noop */ }
            }, 200);
          }
        }}
      >
      {hasQuery ? (
        searchLoading ? (
          <div style={fileTreeEmptyStyle}>正在搜索工作区…</div>
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
          <div style={fileTreeEmptyStyle}>没有与“{query.trim()}”匹配的文件。</div>
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
