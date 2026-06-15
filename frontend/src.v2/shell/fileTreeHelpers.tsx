import type { ReactNode } from "react";
import {
  File,
  FileCode,
  FileText,
  Folder,
  Hash,
  Image,
  FileJson,
  FileCog,
  FileArchive,
  FileType,
} from "lucide-react";
import type { WorkspaceTreeNode } from "../protocol/workspace";
import { isDesktop, type FsEntry } from "../desktop/runtime";
import { withRuntimeToken } from "../protocol/api";
import { workspaceDisplayName } from "../lib/workspace-display";
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
  `minicode.files.expanded:${workspace || "."}`;

export const readExpandedPaths = (workspace: string): Set<string> => {
  if (typeof localStorage === "undefined") return new Set();
  try {
    const raw = localStorage.getItem(expandedStorageKey(workspace));
    const items = raw ? JSON.parse(raw) : [];
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
  if (node.path === path) return { ...node, children };
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

export const normalizeDesktopExpandedPaths = (workspace: string, paths: Iterable<string>): Set<string> =>
  new Set(Array.from(paths, (path) => joinWorkspacePath(workspace, path)));

export const normalizeChangePath = (path: string): string =>
  path.replace(/\\/g, "/").replace(/\/+/g, "/").replace(/\/$/, "");

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

export const isSameTreePath = (left?: string | null, right?: string | null): boolean =>
  Boolean(left && right && normalizeTreePath(left) === normalizeTreePath(right));

// ── Search / preview helpers ────────────────────────────────────────────

export const mediaTypeForPath = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return "application/pdf";
  if (ext === "png") return "image/png";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  if (ext === "svg") return "image/svg+xml";
  return "application/octet-stream";
};

export const isPreviewableFile = (path: string): boolean =>
  /(\.png|\.jpe?g|\.gif|\.webp|\.svg|\.pdf)$/i.test(path);

export const previewUrlForPath = (path: string): string => {
  if (!isDesktop()) return withRuntimeToken(`/api/workspace/raw?path=${encodeURIComponent(path)}`);
  const normalized = path.replace(/\\/g, "/");
  const withLeadingSlash = /^[a-zA-Z]:\//.test(normalized) ? `/${normalized}` : normalized;
  return encodeURI(`file://${withLeadingSlash}`);
};

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

export const formatBytes = (value: number): string => {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

// ── Icon helpers ───────────────────────────────────────────────────────

export const iconColor = (node: WorkspaceTreeNode): string => {
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

export const fileIcon = (name: string): ReactNode => {
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
