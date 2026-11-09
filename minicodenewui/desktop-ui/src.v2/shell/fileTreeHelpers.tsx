import { fileGlyphColor, fileGlyphKind, fileIcon, folderIcon } from "../lib/file-icons";
import { safeJsonParse } from "../lib/safe-parse";
import type { WorkspaceTreeNode } from "../protocol/workspace";
import { type FsEntry } from "../desktop/runtime";
import { workspaceDisplayName } from "../lib/workspace-display";
import {
  isWindowsLikeWorkspacePath,
  normalizeWorkspacePath,
  normalizeWorkspaceRoot,
} from "../lib/workspace-path";
import {
  isPreviewableMediaPath,
  mediaTypeForPath,
} from "../lib/media-types";
import { formatBytes } from "../lib/format-bytes";
import {
  type FileSearchResult,
  type ExplorerDensity,
  HIDDEN_TREE_NAMES,
} from "./fileTreeTypes";

// ── Tree helpers ───────────────────────────────────────────────────────

export const isMissingWorkspaceError = (err: unknown): boolean => {
  if (!err) return false;
  const message = err instanceof Error ? err.message : String(err);
  return /workspace folder is missing|path not found|not found|does not exist|not a directory/i.test(message);
};

export const isHiddenTreeNode = (node: WorkspaceTreeNode): boolean =>
  HIDDEN_TREE_NAMES.has(node.name)
  || node.name.startsWith(".pytest_tmp_")
  || node.name.endsWith(".tsbuildinfo")
  || /^vite-\d+\.(err|out)\.log$/i.test(node.name)
  || /^backend-\d+\.(err|out)\.log$/i.test(node.name)
  || /^minicode-ui-snapshot/i.test(node.name);

export const visibleChildren = (node: WorkspaceTreeNode): WorkspaceTreeNode[] =>
  (node.children ?? []).filter((child) => !isHiddenTreeNode(child));

export const nodeMatchesQuery = (node: WorkspaceTreeNode, query: string): boolean => {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  if (node.name.toLowerCase().includes(normalized) || node.path.toLowerCase().includes(normalized)) return true;
  return visibleChildren(node).some((child) => nodeMatchesQuery(child, query));
};

export const filteredChildren = (node: WorkspaceTreeNode, query: string): WorkspaceTreeNode[] =>
  visibleChildren(node).filter((child) => nodeMatchesQuery(child, query));

export const expandedStorageKey = (workspace: string): string =>
  `minicode.files.expanded:${normalizeWorkspaceRoot(workspace) || "."}`;

const legacyExpandedStorageKey = (workspace: string): string =>
  `minicode.files.expanded:${workspace || "."}`;

export const readExpandedPaths = (workspace: string): Set<string> => {
  if (typeof localStorage === "undefined") return new Set();
  try {
    const storageKey = expandedStorageKey(workspace);
    const legacyStorageKey = legacyExpandedStorageKey(workspace);
    const canonicalRaw = localStorage.getItem(storageKey);
    const raw = canonicalRaw
      ?? (legacyStorageKey !== storageKey ? localStorage.getItem(legacyStorageKey) : null);
    if (canonicalRaw == null && raw != null) {
      localStorage.setItem(storageKey, raw);
    }
    const items = raw ? safeJsonParse<unknown>(raw, []) : [];
    return new Set(Array.isArray(items) ? items.filter((item) => typeof item === "string") : []);
  } catch {
    return new Set();
  }
};

export const writeExpandedPaths = (workspace: string, paths: Set<string>) => {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(expandedStorageKey(workspace), JSON.stringify(Array.from(paths).sort()));
};

export const sortNodes = (nodes: WorkspaceTreeNode[]): WorkspaceTreeNode[] =>
  nodes.slice().sort((a, b) => {
    if (a.is_dir === b.is_dir) return a.name.localeCompare(b.name);
    return a.is_dir ? -1 : 1;
  });

export const nodesFromEntries = (entries: FsEntry[]): WorkspaceTreeNode[] =>
  sortNodes(entries.map((entry) => ({
    name: entry.name || entry.path.split(/[/\\]/).filter(Boolean).pop() || entry.path,
    path: entry.path,
    is_dir: entry.isDirectory,
    size_bytes: entry.sizeBytes,
    modified_at: entry.modifiedAt,
    children: entry.isDirectory ? [] : undefined,
  })));

export const entriesToTree = (entries: FsEntry[], rootPath: string, rootName: string): WorkspaceTreeNode => ({
  name: rootName,
  path: rootPath,
  is_dir: true,
  children: nodesFromEntries(entries),
});

export const replaceNodeChildren = (
  node: WorkspaceTreeNode,
  path: string,
  children: WorkspaceTreeNode[],
): WorkspaceTreeNode => {
  if (isSameTreePath(node.path, path)) return { ...node, children };
  if (!node.children) return node;
  return {
    ...node,
    children: node.children.map((child) => replaceNodeChildren(child, path, children)),
  };
};

export const workspaceLabel = (path: string): string =>
  workspaceDisplayName(path, "Current workspace");

// ── Path helpers ───────────────────────────────────────────────────────

export const joinWorkspacePath = (root: string, path: string): string => {
  if (!root || /^[a-zA-Z]:[\\/]/.test(path) || path.startsWith("/") || path.startsWith("\\")) return path;
  return `${root.replace(/[\\/]+$/, "")}/${path.replace(/^[\\/]+/, "")}`;
};

const treePathKey = (path: string, workspaceRoot = ""): string => {
  const normalized = normalizeWorkspacePath(path);
  const driveMatch = normalized.match(/^([A-Za-z]:)(?:\/|$)/);
  const prefix = driveMatch
    ? `${driveMatch[1]}/`
    : normalized.startsWith("//")
      ? "//"
      : normalized.startsWith("/")
        ? "/"
        : "";
  const body = driveMatch ? normalized.slice(driveMatch[0].length) : normalized.slice(prefix.length);
  const parts: string[] = [];
  for (const part of body.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (parts.length > 0 && parts[parts.length - 1] !== "..") parts.pop();
      else parts.push(part);
      continue;
    }
    parts.push(part);
  }
  const canonical = `${prefix}${parts.join("/")}`.replace(/\/+$/, "");
  return isWindowsLikeWorkspacePath(canonical) || isWindowsLikeWorkspacePath(workspaceRoot)
    ? canonical.toLowerCase()
    : canonical;
};

export const isPathInsideTreeRoot = (root: string, path: string): boolean => {
  const rootKey = treePathKey(root);
  const pathKey = treePathKey(path);
  return Boolean(rootKey && (pathKey === rootKey || pathKey.startsWith(`${rootKey}/`)));
};

export const normalizeDesktopExpandedPaths = (workspace: string, paths: Iterable<string>): Set<string> => {
  const normalizedWorkspace = normalizeTreePath(workspace);
  const next = new Set<string>();
  for (const rawPath of paths) {
    const normalizedPath = joinWorkspacePath(workspace, rawPath);
    if (isPathInsideTreeRoot(normalizedWorkspace, normalizedPath)) {
      next.add(normalizedPath);
    }
  }
  return next;
};

export const normalizeChangePath = normalizeWorkspacePath;

export const parentTreePath = (path: string, workingDirectory: string): string => {
  const normalized = normalizeChangePath(path);
  const root = normalizeChangePath(workingDirectory || ".");
  if (!normalized || normalized === "." || normalized === root) return root || ".";
  const parts = normalized.split("/");
  if (parts.length <= 1) return ".";
  const parent = parts.slice(0, -1).join("/");
  return parent || root || ".";
};

export const normalizeTreePath = (path: string): string => path.replace(/\\/g, "/").replace(/\/+$/, "");

export const isSameTreePath = (
  left?: string | null,
  right?: string | null,
  workspaceRoot = "",
): boolean => Boolean(left && right && treePathKey(left, workspaceRoot) === treePathKey(right, workspaceRoot));

export const findTreeNode = (
  node: WorkspaceTreeNode | null | undefined,
  path: string,
): WorkspaceTreeNode | null => {
  if (!node) return null;
  if (isSameTreePath(node.path, path)) return node;
  for (const child of node.children ?? []) {
    const found = findTreeNode(child, path);
    if (found) return found;
  }
  return null;
};

export const hasLoadedDirectoryNode = (
  node: WorkspaceTreeNode | null | undefined,
  path: string,
): boolean => {
  const found = findTreeNode(node, path);
  return Boolean(found?.is_dir);
};

// ── Search / preview helpers ────────────────────────────────────────────

export { mediaTypeForPath };

export const isPreviewableFile = (path: string): boolean =>
  isPreviewableMediaPath(path);

export const isHiddenSearchResult = (result: FileSearchResult): boolean => {
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

export const countVisibleNodes = (
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

// ── Formatting helpers ─────────────────────────────────────────────────

export const formatFileMeta = (node: WorkspaceTreeNode): string => {
  const bits = [node.path];
  if (!node.is_dir && typeof node.size_bytes === "number") bits.push(formatBytes(node.size_bytes));
  if (node.modified_at) bits.push(new Date(node.modified_at).toLocaleString());
  return bits.join(" \u2022 ");
};

export { formatBytes };

// ── Shared file icon catalog ───────────────────────────────────────────
export { fileGlyphColor, fileGlyphKind, fileIcon, folderIcon };
export type { FileGlyphKind } from "../lib/file-icons";

export const iconColor = (node: WorkspaceTreeNode): string =>
  node.is_dir ? "var(--mc-icon-folder, var(--text-secondary))" : fileGlyphColor(node.name);
