import { useEffect, useMemo, useState, useCallback, useRef, memo, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  File,
  FileCode,
  FileText,
  Folder,
  FolderOpen,
  Hash,
  Image,
  FileJson,
  FileCog,
  FileArchive,
  FileType,
  FilePlus2,
  FolderPlus,
  RefreshCw,
  Search,
  MoreHorizontal,
} from "lucide-react";
import {
  createWorkspaceDirectory,
  deleteWorkspacePath,
  listWorkspaceTree,
  renameWorkspacePath,
  searchWorkspaceFiles,
  writeWorkspaceFile,
  type WorkspaceTreeNode,
} from "../protocol/workspace";
import { useAppStore } from "../stores";
import { isDesktop, fsListTree, fsSearchFiles, revealPath, desktop, trustWorkspace, type FsEntry } from "../desktop/runtime";
import { withRuntimeToken } from "../protocol/api";
import { workspaceDisplayName } from "../lib/workspace-display";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";

type GitStatus = "modified" | "added" | "untracked" | "deleted" | "staged";

type ExplorerDensity = "compact" | "comfortable";

type FileSearchResult = {
  path: string;
  name: string;
  score?: number;
  kind?: "file" | "folder";
};

const ROW_HEIGHT: Record<ExplorerDensity, number> = {
  compact: 28,
  comfortable: 30,
};

const HIDDEN_TREE_NAMES = new Set([
  ".git",
  ".playwright-mcp",
  ".pytest_cache",
  ".tmp",
  "test-results",
  "node_modules",
  "__pycache__",
  "dist",
  "build",
]);

const isHiddenTreeNode = (node: WorkspaceTreeNode): boolean =>
  HIDDEN_TREE_NAMES.has(node.name)
  || node.name.startsWith(".pytest_tmp_")
  || node.name.endsWith(".tsbuildinfo")
  || /^vite-\d+\.(err|out)\.log$/i.test(node.name)
  || /^backend-\d+\.(err|out)\.log$/i.test(node.name)
  || /^minicode-ui-snapshot/i.test(node.name);

const visibleChildren = (node: WorkspaceTreeNode): WorkspaceTreeNode[] =>
  (node.children ?? []).filter((child) => !isHiddenTreeNode(child));

const nodeMatchesQuery = (node: WorkspaceTreeNode, query: string): boolean => {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  if (node.name.toLowerCase().includes(normalized) || node.path.toLowerCase().includes(normalized)) return true;
  return visibleChildren(node).some((child) => nodeMatchesQuery(child, query));
};

const filteredChildren = (node: WorkspaceTreeNode, query: string): WorkspaceTreeNode[] =>
  visibleChildren(node).filter((child) => nodeMatchesQuery(child, query));

const expandedStorageKey = (workspace: string): string =>
  `minicode.files.expanded:${workspace || "."}`;

const readExpandedPaths = (workspace: string): Set<string> => {
  if (typeof localStorage === "undefined") return new Set();
  try {
    const raw = localStorage.getItem(expandedStorageKey(workspace));
    const items = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(items) ? items.filter((item) => typeof item === "string") : []);
  } catch {
    return new Set();
  }
};

const writeExpandedPaths = (workspace: string, paths: Set<string>) => {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(expandedStorageKey(workspace), JSON.stringify(Array.from(paths).sort()));
};

const sortNodes = (nodes: WorkspaceTreeNode[]): WorkspaceTreeNode[] =>
  nodes.slice().sort((a, b) => {
    if (a.is_dir === b.is_dir) return a.name.localeCompare(b.name);
    return a.is_dir ? -1 : 1;
  });

const nodesFromEntries = (entries: FsEntry[]): WorkspaceTreeNode[] =>
  sortNodes(entries.map((entry) => ({
    name: entry.name || entry.path.split(/[/\\]/).filter(Boolean).pop() || entry.path,
    path: entry.path,
    is_dir: entry.isDirectory,
    size_bytes: entry.sizeBytes,
    modified_at: entry.modifiedAt,
    children: entry.isDirectory ? [] : undefined,
  })));

const entriesToTree = (entries: FsEntry[], rootPath: string, rootName: string): WorkspaceTreeNode => ({
  name: rootName,
  path: rootPath,
  is_dir: true,
  children: nodesFromEntries(entries),
});

const replaceNodeChildren = (
  node: WorkspaceTreeNode,
  path: string,
  children: WorkspaceTreeNode[],
): WorkspaceTreeNode => {
  if (node.path === path) return { ...node, children };
  if (!node.children) return node;
  return {
    ...node,
    children: node.children.map((child) => replaceNodeChildren(child, path, children)),
  };
};

const workspaceLabel = (path: string): string =>
  workspaceDisplayName(path, "Current workspace");

const joinWorkspacePath = (root: string, path: string): string => {
  if (!root || /^[a-zA-Z]:[\\/]/.test(path) || path.startsWith("/") || path.startsWith("\\")) return path;
  return `${root.replace(/[\\/]+$/, "")}/${path.replace(/^[\\/]+/, "")}`;
};

const normalizeChangePath = (path: string): string =>
  path.replace(/\\/g, "/").replace(/\/+/g, "/").replace(/\/$/, "");

const parentTreePath = (path: string, workingDirectory: string): string => {
  const normalized = normalizeChangePath(path);
  const root = normalizeChangePath(workingDirectory || ".");
  if (!normalized || normalized === "." || normalized === root) return root || ".";
  const parts = normalized.split("/");
  if (parts.length <= 1) return ".";
  const parent = parts.slice(0, -1).join("/");
  return parent || root || ".";
};

const mediaTypeForPath = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return "application/pdf";
  if (ext === "png") return "image/png";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  if (ext === "svg") return "image/svg+xml";
  return "application/octet-stream";
};

const isPreviewableFile = (path: string): boolean =>
  /(\.png|\.jpe?g|\.gif|\.webp|\.svg|\.pdf)$/i.test(path);

const previewUrlForPath = (path: string): string => {
  if (!isDesktop()) return withRuntimeToken(`/api/workspace/raw?path=${encodeURIComponent(path)}`);
  const normalized = path.replace(/\\/g, "/");
  const withLeadingSlash = /^[a-zA-Z]:\//.test(normalized) ? `/${normalized}` : normalized;
  return encodeURI(`file://${withLeadingSlash}`);
};

interface ContextMenuState {
  x: number;
  y: number;
  path: string;
  isDir: boolean;
}

export const FileTree = () => {
  const [tree, setTree] = useState<WorkspaceTreeNode | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingPaths, setLoadingPaths] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<FileSearchResult[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [toolbarMenuOpen, setToolbarMenuOpen] = useState(false);
  const density: ExplorerDensity = "compact";
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const fileTreeVersion = useAppStore((s) => s.fileTreeVersion);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => readExpandedPaths(workingDirectory || "."));
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
    setLoading(true);
    setError("");
    try {
      if (isDesktop()) {
        const rootPath = workingDirectory || ".";
        if (workingDirectory) {
          await trustWorkspace(rootPath);
        }
        const entries = await fsListTree(rootPath);
        let nextTree = entriesToTree(entries, rootPath, workspaceLabel(rootPath));
        const persistedExpanded = Array.from(readExpandedPaths(rootPath))
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
      setError(err instanceof Error ? err.message : "Could not load file tree");
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

  useEffect(() => {
    refresh();
  }, [refresh, workingDirectory, fileTreeVersion]);

  useEffect(() => {
    setExpandedPaths(readExpandedPaths(workingDirectory || "."));
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

  useEffect(() => {
    requestGitChanges();
  }, [requestGitChanges, workingDirectory]);

  useEffect(() => {
    if (!toolbarMenuOpen) return;
    const close = () => setToolbarMenuOpen(false);
    window.addEventListener("click", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", close);
    };
  }, [toolbarMenuOpen]);

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }
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
    return () => {
      window.clearTimeout(timer);
    };
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
    } catch {
      await showAlert({ title: "Error", message: `Could not create ${path}` });
    }
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
    } catch {
      await showAlert({ title: "Error", message: `Could not create ${path}` });
    }
  };

  if (loading && !tree) {
    return (
      <div style={{ padding: 12, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        Loading...
      </div>
    );
  }

  if (error && !tree) {
    return (
      <div style={{ padding: 12, fontSize: "var(--text-sm)" }}>
        <div style={{ color: "var(--text-muted)", marginBottom: 8 }}>{error}</div>
        <button onClick={refresh} style={refreshBtn}>Retry</button>
      </div>
    );
  }

  if (!tree) return null;
  const hasQuery = query.trim().length > 0;
  const children = hasQuery ? [] : filteredChildren(tree, query);
  const visibleCount = hasQuery ? searchResults.length : countVisibleNodes(children, expandedPaths, query);

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
        <div style={toolbarMenuWrapStyle}>
          <button
            type="button"
            title="More file actions"
            aria-label="More file actions"
            onClick={(event) => {
              event.stopPropagation();
              setToolbarMenuOpen((open) => !open);
            }}
            style={fileTreeIconButtonStyle}
          >
            <MoreHorizontal size={13} />
          </button>
          {toolbarMenuOpen && (
            <div style={toolbarMenuStyle} onClick={(event) => event.stopPropagation()}>
              <button
                type="button"
                onClick={() => {
                  setToolbarMenuOpen(false);
                  void createFile();
                }}
                style={toolbarMenuItemStyle}
              >
                <FilePlus2 size={13} />
                <span>New file</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setToolbarMenuOpen(false);
                  void createFolder();
                }}
                style={toolbarMenuItemStyle}
              >
                <FolderPlus size={13} />
                <span>New folder</span>
              </button>
            </div>
          )}
        </div>
      </div>
      <div style={fileTreeToolbarStyle}>
        <div style={fileTreeSearchStyle}>
          <Search size={12} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search files"
            style={fileTreeSearchInputStyle}
          />
        </div>
        <button type="button" title="Refresh files" aria-label="Refresh files" onClick={() => void refresh()} style={fileTreeIconButtonStyle}>
          <RefreshCw size={13} />
        </button>
        <div style={toolbarMenuWrapStyle}>
          <button
            type="button"
            title="More file actions"
            aria-label="More file actions"
            onClick={(event) => {
              event.stopPropagation();
              setToolbarMenuOpen((open) => !open);
            }}
            style={fileTreeIconButtonStyle}
          >
            <MoreHorizontal size={13} />
          </button>
          {toolbarMenuOpen && (
            <div style={toolbarMenuStyle} onClick={(event) => event.stopPropagation()}>
              <button
                type="button"
                onClick={() => {
                  setToolbarMenuOpen(false);
                  void createFile();
                }}
                style={toolbarMenuItemStyle}
              >
                <FilePlus2 size={13} />
                <span>New file</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setToolbarMenuOpen(false);
                  void createFolder();
                }}
                style={toolbarMenuItemStyle}
              >
                <FolderPlus size={13} />
                <span>New folder</span>
              </button>
            </div>
          )}
        </div>
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
          <div style={fileTreeEmptyStyle}>
            No files match "{query.trim()}".
          </div>
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
          {workingDirectory
              ? "Empty workspace"
              : "Open a workspace folder to browse files."}
          {!workingDirectory && isDesktop() && (
            <button type="button" onClick={() => void openWorkspaceFolder()} style={openWorkspaceButtonStyle}>
              Open folder
            </button>
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

const GIT_STATUS_COLOR: Record<GitStatus, string> = {
  modified: "var(--state-warning, #e5a50a)",
  added: "var(--state-success)",
  untracked: "var(--text-muted)",
  deleted: "var(--state-danger)",
  staged: "var(--state-info)",
};

const GIT_STATUS_LABEL: Record<GitStatus, string> = {
  modified: "M",
  added: "A",
  untracked: "U",
  deleted: "D",
  staged: "S",
};

const countVisibleNodes = (
  nodes: WorkspaceTreeNode[],
  expandedPaths: Set<string>,
  query: string,
): number => {
  const hasQuery = query.trim().length > 0;
  let total = 0;
  for (const node of nodes) {
    total += 1;
    const expanded = expandedPaths.has(node.path) || (hasQuery && nodeMatchesQuery(node, query));
    if (expanded) total += countVisibleNodes(filteredChildren(node, query), expandedPaths, query);
  }
  return total;
};

const normalizeTreePath = (path: string): string => path.replace(/\\/g, "/").replace(/\/+$/, "");

const isSameTreePath = (left?: string | null, right?: string | null): boolean =>
  Boolean(left && right && normalizeTreePath(left) === normalizeTreePath(right));

const isHiddenSearchResult = (result: FileSearchResult): boolean => {
  const parts = result.path.split(/[/\\]/).filter(Boolean);
  return parts.some((part) =>
    HIDDEN_TREE_NAMES.has(part)
    || part.startsWith(".pytest_tmp_")
    || part.endsWith(".tsbuildinfo")
    || /^vite-\d+\.(err|out)\.log$/i.test(part)
    || /^backend-\d+\.(err|out)\.log$/i.test(part)
    || /^minicode-ui-snapshot/i.test(part)
  );
};

const formatFileMeta = (node: WorkspaceTreeNode): string => {
  const bits = [node.path];
  if (!node.is_dir && typeof node.size_bytes === "number") bits.push(formatBytes(node.size_bytes));
  if (node.modified_at) bits.push(new Date(node.modified_at).toLocaleString());
  return bits.join(" • ");
};

const formatBytes = (value: number): string => {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

const FileContextMenu = ({
  menu,
  workingDirectory,
  onRefresh,
  onClose,
}: {
  menu: ContextMenuState;
  workingDirectory: string;
  onRefresh: () => void;
  onClose: () => void;
}) => {
  useEffect(() => {
    const handler = () => onClose();
    document.addEventListener("click", handler);
    document.addEventListener("contextmenu", handler);
    return () => {
      document.removeEventListener("click", handler);
      document.removeEventListener("contextmenu", handler);
    };
  }, [onClose]);

  const copyPath = () => {
    navigator.clipboard.writeText(menu.path);
    onClose();
  };

  const openInEditor = () => {
    useAppStore.getState().openEditorFile(menu.path, menu.path.split(/[/\\]/).pop() ?? menu.path);
    onClose();
  };

  const openPreview = () => {
    const name = menu.path.split(/[/\\]/).pop() ?? menu.path;
    useAppStore.getState().setPreviewArtifact({
      artifactId: menu.path,
      content: "",
      name,
      mediaType: mediaTypeForPath(menu.path),
      url: previewUrlForPath(isDesktop() ? joinWorkspacePath(workingDirectory, menu.path) : menu.path),
      loadedAt: Date.now(),
    });
    useAppStore.getState().setRightStackTab("preview");
    onClose();
  };

  const createChildFile = async () => {
    const { showPrompt } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "New file", message: "File name:", placeholder: "example.ts" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.writeFile(targetPath, "");
      else await writeWorkspaceFile(targetPath, "");
      onRefresh();
    } finally {
      onClose();
    }
  };

  const createChildFolder = async () => {
    const { showPrompt } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "New folder", message: "Folder name:", placeholder: "components" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.createDirectory(targetPath);
      else await createWorkspaceDirectory(targetPath);
      onRefresh();
    } finally {
      onClose();
    }
  };

  const deleteFile = async () => {
    const { showConfirm } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: "Delete",
      message: `Delete ${menu.path}?`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) { onClose(); return; }
    if (isDesktop()) {
      await desktop()?.fs.deletePath(menu.path, menu.isDir);
    } else {
      await deleteWorkspacePath(menu.path, menu.isDir);
    }
    onRefresh();
    onClose();
  };

  const renameFile = async () => {
    const { showPrompt } = await import("../overlays/DialogService");
    const newName = await showPrompt({
      title: "Rename",
      message: "New name:",
      defaultValue: menu.path.split(/[/\\]/).pop() ?? "",
    });
    if (!newName) { onClose(); return; }
    if (/[/\\]/.test(newName) || newName === ".." || newName.startsWith("../") || newName.startsWith("..\\")) {
      const { showAlert } = await import("../overlays/DialogService");
      await showAlert({ title: "Invalid name", message: "File names cannot contain path separators or traversal patterns." });
      onClose();
      return;
    }
    const parent = menu.path.replace(/[/\\][^/\\]+$/, "");
    const newPath = parent ? `${parent}/${newName}` : newName;
    if (isDesktop()) {
      await desktop()?.fs.renamePath(menu.path, newPath);
    } else {
      await renameWorkspacePath(menu.path, newPath);
    }
    onRefresh();
    onClose();
  };

  const revealInExplorer = () => {
    revealPath(menu.path);
    onClose();
  };

  const items = [
    ...(!menu.isDir ? [{ label: "Open in Editor", action: openInEditor }] : []),
    ...(!menu.isDir && isPreviewableFile(menu.path) ? [{ label: "Open in Preview Pane", action: openPreview }] : []),
    ...(menu.isDir ? [
      { label: "New File...", action: createChildFile },
      { label: "New Folder...", action: createChildFolder },
    ] : []),
    ...(isDesktop() ? [{ label: "Reveal in Explorer", action: revealInExplorer }] : []),
    { label: "Copy Path", action: copyPath },
    { label: "Rename...", action: renameFile },
    { label: "Delete", action: deleteFile },
  ];

  return (
    <div
      style={{
        position: "fixed",
        left: menu.x,
        top: menu.y,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 6px)",
        boxShadow: "var(--shadow-md)",
        padding: 4,
        zIndex: 200,
        minWidth: 140,
      }}
    >
      {items.map((item) => (
        <button
          key={item.label}
          onClick={item.action}
          style={{
            display: "block",
            width: "100%",
            textAlign: "left",
            background: "transparent",
            border: 0,
            padding: "5px 10px",
            fontSize: "var(--text-xs)",
            color: item.label === "Delete" ? "var(--state-danger)" : "var(--text-primary)",
            cursor: "pointer",
            borderRadius: "var(--radius-sm, 4px)",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
};

const TreeNode = memo(({
  node,
  depth,
  gitMap,
  loadingPaths,
  expandedPaths,
  query,
  workingDirectory,
  activeEditorPath,
  density,
  onToggleExpanded,
  onContextMenu,
}: {
  node: WorkspaceTreeNode;
  depth: number;
  gitMap: Map<string, GitStatus>;
  loadingPaths: Set<string>;
  expandedPaths: Set<string>;
  query: string;
  workingDirectory: string;
  activeEditorPath: string | null;
  density: ExplorerDensity;
  onToggleExpanded: (path: string, shouldLoad?: boolean) => void;
  onContextMenu: (menu: ContextMenuState) => void;
}) => {
  const hasQuery = query.trim().length > 0;
  const expanded = expandedPaths.has(node.path) || (hasQuery && nodeMatchesQuery(node, query));
  const loading = loadingPaths.has(node.path);
  const selected = !node.is_dir && isSameTreePath(activeEditorPath, node.path);
  const childCount = node.is_dir ? filteredChildren(node, query).length : 0;
  const gitStatus = gitMap.get(node.path);
  const toggle = () => {
    if (node.is_dir) onToggleExpanded(node.path, !expanded && (node.children?.length ?? 0) === 0);
  };

  const openFile = () => {
    if (!node.is_dir) {
      useAppStore.getState().openEditorFile(node.path, node.name);
    }
  };

  const handleContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    onContextMenu({ x: e.clientX, y: e.clientY, path: node.path, isDir: node.is_dir });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      node.is_dir ? toggle() : openFile();
    } else if (e.key === "ArrowRight" && node.is_dir && !expanded) {
      e.preventDefault();
      onToggleExpanded(node.path, (node.children?.length ?? 0) === 0);
    } else if (e.key === "ArrowLeft" && node.is_dir && expanded) {
      e.preventDefault();
      onToggleExpanded(node.path, false);
    }
  };

  return (
    <div>
      <div
        role="treeitem"
        tabIndex={0}
        aria-expanded={node.is_dir ? expanded : undefined}
        aria-selected={selected}
        onClick={node.is_dir ? toggle : openFile}
        onKeyDown={handleKeyDown}
        onContextMenu={handleContextMenu}
        title={formatFileMeta(node)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          height: ROW_HEIGHT[density],
          margin: "1px 8px",
          padding: "0 8px",
          paddingLeft: 8 + depth * 15,
          cursor: "pointer",
          color: selected ? "var(--text-primary)" : node.is_dir ? "var(--text-primary)" : "var(--text-secondary)",
          background: selected ? "var(--surface-active)" : "transparent",
          border: "1px solid transparent",
          borderColor: selected ? "color-mix(in oklch, var(--accent-primary) 32%, transparent)" : "transparent",
          borderRadius: "var(--radius-sm, 6px)",
          boxShadow: selected ? "inset 2px 0 0 var(--accent-primary)" : "none",
          transition: "background 80ms ease, border-color 80ms ease, box-shadow 80ms ease",
        }}
        onMouseEnter={(e) => {
          if (!selected) e.currentTarget.style.background = "var(--surface-hover)";
        }}
        onMouseLeave={(e) => {
          if (!selected) e.currentTarget.style.background = "transparent";
        }}
      >
        <span style={treeChevronStyle}>
          {node.is_dir ? (
            expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />
          ) : (
            <span style={{ width: 14 }} />
          )}
        </span>
        <span style={{ ...treeIconStyle, color: iconColor(node) }}>
          {node.is_dir ? (expanded ? <FolderOpen size={16} /> : <Folder size={16} />) : fileIcon(node.name)}
        </span>
        <span style={{
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontSize: "14px",
          lineHeight: "18px",
          fontFamily: "var(--font-ui)",
          color: gitStatus ? GIT_STATUS_COLOR[gitStatus] : undefined,
          fontWeight: selected ? 650 : node.is_dir ? 560 : 450,
        }}>
          {node.name}
        </span>
        {node.is_dir && childCount > 0 && density === "comfortable" && (
          <span style={folderCountStyle}>{childCount}</span>
        )}
        {typeof node.size_bytes === "number" && !node.is_dir && density === "comfortable" && !gitStatus && (
          <span style={fileMetaStyle}>{formatBytes(node.size_bytes)}</span>
        )}
        {gitStatus && (
          <span style={{ ...gitBadgeStyle, color: GIT_STATUS_COLOR[gitStatus] }}>
            {GIT_STATUS_LABEL[gitStatus]}
          </span>
        )}
        {loading && (
          <span style={{ fontSize: 10, color: "var(--text-muted)", flexShrink: 0 }}>
            ...
          </span>
        )}
      </div>
      {expanded && node.children && (
        <div style={{ position: "relative", marginLeft: depth > 0 ? 0 : 0 }}>
          {depth >= 0 && (
            <span style={{
              position: "absolute",
              left: 21 + depth * 15,
              top: 0,
              bottom: 0,
              width: 1,
              background: "var(--border-subtle)",
              opacity: 0.35,
              pointerEvents: "none",
            }} />
          )}
          {filteredChildren(node, query).map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              gitMap={gitMap}
              loadingPaths={loadingPaths}
              expandedPaths={expandedPaths}
              query={query}
              workingDirectory={workingDirectory}
              activeEditorPath={activeEditorPath}
              density={density}
              onToggleExpanded={onToggleExpanded}
              onContextMenu={onContextMenu}
            />
          ))}
        </div>
      )}
    </div>
  );
});

const SearchResultRow = ({
  result,
  gitMap,
  activeEditorPath,
  workingDirectory,
  onContextMenu,
}: {
  result: FileSearchResult;
  gitMap: Map<string, GitStatus>;
  activeEditorPath: string | null;
  workingDirectory: string;
  onContextMenu: (menu: ContextMenuState) => void;
}) => {
  const selected = isSameTreePath(activeEditorPath, result.path);
  const isDir = result.kind === "folder";
  const gitStatus = gitMap.get(result.path);
  const parent = result.path.replace(/\\/g, "/").split("/").slice(0, -1).join("/");
  const openResult = () => {
    if (isDir) return;
    useAppStore.getState().openEditorFile(result.path, result.name);
  };
  return (
    <div
      role="treeitem"
      tabIndex={0}
      aria-selected={selected}
      title={result.path}
      onClick={openResult}
      onKeyDown={(event) => {
        if ((event.key === "Enter" || event.key === " ") && !isDir) {
          event.preventDefault();
          openResult();
        }
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onContextMenu({ x: event.clientX, y: event.clientY, path: result.path, isDir });
      }}
      style={{
        ...searchResultRowStyle,
        background: selected ? "var(--surface-active)" : "transparent",
        borderColor: selected ? "color-mix(in oklch, var(--accent-primary) 32%, transparent)" : "transparent",
        boxShadow: selected ? "inset 2px 0 0 var(--accent-primary)" : "none",
        cursor: isDir ? "default" : "pointer",
      }}
    >
      <span style={{ ...treeIconStyle, color: isDir ? "var(--accent-primary)" : iconColor({ name: result.name, path: result.path, is_dir: false }) }}>
        {isDir ? <Folder size={14} /> : fileIcon(result.name)}
      </span>
      <span style={{ minWidth: 0, flex: 1 }}>
        <span style={searchResultNameStyle}>{result.name}</span>
        <span style={searchResultPathStyle}>{parent || workspaceLabel(workingDirectory)}</span>
      </span>
      {gitStatus && (
        <span style={{ ...gitBadgeStyle, color: GIT_STATUS_COLOR[gitStatus] }}>
          {GIT_STATUS_LABEL[gitStatus]}
        </span>
      )}
    </div>
  );
};

const iconColor = (node: WorkspaceTreeNode): string => {
  if (node.is_dir) return "var(--accent-primary)";
  const ext = node.name.split(".").pop()?.toLowerCase() ?? "";
  if (["ts", "tsx"].includes(ext)) return "var(--icon-ts, #3178c6)";
  if (["js", "jsx", "mjs", "cjs"].includes(ext)) return "var(--icon-js, #d6b84f)";
  if (["py"].includes(ext)) return "var(--icon-py, #4b8bbe)";
  if (["html", "xml"].includes(ext)) return "var(--icon-html, #e44d26)";
  if (["json", "yaml", "yml", "toml"].includes(ext)) return "var(--state-warning)";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "var(--state-success)";
  if (["zip", "gz", "tar", "rar", "7z"].includes(ext)) return "var(--text-muted)";
  if (["md", "txt", "pdf"].includes(ext)) return "var(--text-muted)";
  if (["css", "scss"].includes(ext)) return "var(--accent-primary)";
  return "var(--text-muted)";
};

const fileIcon = (name: string): ReactNode => {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const lower = name.toLowerCase();
  if (lower.includes("config") || lower.startsWith(".env")) return <FileCog size={16} />;
  if (["ts", "tsx", "js", "jsx", "py", "html", "go", "rs", "java", "c", "cpp"].includes(ext)) return <FileCode size={16} />;
  if (["json"].includes(ext)) return <FileJson size={16} />;
  if (["yaml", "yml", "toml"].includes(ext)) return <FileCog size={16} />;
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return <Image size={16} />;
  if (["zip", "gz", "tar", "rar", "7z"].includes(ext)) return <FileArchive size={16} />;
  if (["woff", "woff2", "ttf", "otf"].includes(ext)) return <FileType size={16} />;
  if (["md", "txt", "pdf"].includes(ext)) return <FileText size={16} />;
  if (["css", "scss"].includes(ext)) return <Hash size={16} />;
  return <File size={16} />;
};

const refreshBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "4px 10px",
  fontSize: "var(--text-xs)",
  color: "var(--text-secondary)",
  cursor: "pointer",
};

const fileTreeRootStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  fontSize: "var(--text-sm)",
  background: "var(--surface-sidebar)",
};

const fileTreeHeaderStyle: React.CSSProperties = {
  minHeight: 36,
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 12px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

const fileTreeRootLabelStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 600,
};

const fileTreeToolbarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 12px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

const fileTreeListStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: "auto",
  padding: "8px 0",
};

const fileTreeSearchStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  height: 30,
  display: "flex",
  alignItems: "center",
  gap: 5,
  padding: "0 9px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--surface-page)",
  color: "var(--text-muted)",
};

const fileTreeSearchInputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  border: 0,
  outline: 0,
  background: "transparent",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
};

const fileTreeIconButtonStyle: React.CSSProperties = {
  width: 26,
  height: 26,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
  padding: 0,
};

const toolbarMenuWrapStyle: React.CSSProperties = {
  position: "relative",
  flexShrink: 0,
};

const toolbarMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: 34,
  right: 0,
  zIndex: 50,
  minWidth: 150,
  padding: 4,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--surface-raised)",
  boxShadow: "var(--shadow-md)",
};

const toolbarMenuItemStyle: React.CSSProperties = {
  width: "100%",
  height: 30,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 8px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: "transparent",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  textAlign: "left",
};

const treeChevronStyle: React.CSSProperties = {
  width: 16,
  display: "inline-flex",
  justifyContent: "center",
  color: "var(--text-muted)",
  flexShrink: 0,
};

const treeIconStyle: React.CSSProperties = {
  width: 18,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
};

const gitBadgeStyle: React.CSSProperties = {
  minWidth: 16,
  height: 16,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 10,
  fontWeight: 750,
  borderRadius: 4,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  flexShrink: 0,
};

const folderCountStyle: React.CSSProperties = {
  color: "var(--text-tertiary)",
  fontSize: 10,
  flexShrink: 0,
};

const fileMetaStyle: React.CSSProperties = {
  color: "var(--text-tertiary)",
  fontSize: 10,
  flexShrink: 0,
};

const fileTreeEmptyStyle: React.CSSProperties = {
  margin: 8,
  padding: "12px 10px",
  display: "grid",
  gap: 10,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const openWorkspaceButtonStyle: React.CSSProperties = {
  justifySelf: "start",
  height: 28,
  padding: "0 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const searchResultRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 40,
  margin: "2px 8px",
  padding: "5px 9px",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
};

const searchResultNameStyle: React.CSSProperties = {
  display: "block",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "14px",
  lineHeight: "18px",
};

const searchResultPathStyle: React.CSSProperties = {
  display: "block",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "11px",
  lineHeight: "14px",
};
