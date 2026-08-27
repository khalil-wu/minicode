import type { ReactNode } from "react";
import { safeJsonParse } from "../lib/safe-parse";
import { Icon } from "@iconify/react";
import type { IconifyIcon } from "@iconify/types";
import defaultFileIcon from "@iconify-icons/vscode-icons/default-file";
import defaultFolderIcon from "@iconify-icons/vscode-icons/default-folder";
import defaultFolderOpenedIcon from "@iconify-icons/vscode-icons/default-folder-opened";
import cssIcon from "@iconify-icons/vscode-icons/file-type-css";
import excelIcon from "@iconify-icons/vscode-icons/file-type-excel";
import gitIcon from "@iconify-icons/vscode-icons/file-type-git";
import htmlIcon from "@iconify-icons/vscode-icons/file-type-html";
import imageIcon from "@iconify-icons/vscode-icons/file-type-image";
import jsIcon from "@iconify-icons/vscode-icons/file-type-js-official";
import jsonIcon from "@iconify-icons/vscode-icons/file-type-json-official";
import markdownIcon from "@iconify-icons/vscode-icons/file-type-markdown";
import npmIcon from "@iconify-icons/vscode-icons/file-type-npm";
import pdfIcon from "@iconify-icons/vscode-icons/file-type-pdf2";
import powerpointIcon from "@iconify-icons/vscode-icons/file-type-powerpoint";
import powershellIcon from "@iconify-icons/vscode-icons/file-type-powershell";
import pythonIcon from "@iconify-icons/vscode-icons/file-type-python";
import reactIcon from "@iconify-icons/vscode-icons/file-type-reactjs";
import shellIcon from "@iconify-icons/vscode-icons/file-type-shell";
import textIcon from "@iconify-icons/vscode-icons/file-type-text";
import tomlIcon from "@iconify-icons/vscode-icons/file-type-toml";
import tsIcon from "@iconify-icons/vscode-icons/file-type-typescript-official";
import wordIcon from "@iconify-icons/vscode-icons/file-type-word";
import yamlIcon from "@iconify-icons/vscode-icons/file-type-yaml";
import zipIcon from "@iconify-icons/vscode-icons/file-type-zip";
import type { WorkspaceTreeNode } from "../protocol/workspace";
import { type FsEntry } from "../desktop/runtime";
import { workspaceDisplayName } from "../lib/workspace-display";
import {
  isWindowsLikeWorkspacePath,
  normalizeWorkspacePath,
  normalizeWorkspaceRoot,
} from "../lib/workspace-path";
import {
  isImagePath,
  isPreviewableMediaPath,
  mediaTypeForPath,
} from "../lib/media-types";
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

export const formatBytes = (value: number): string => {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

// ── Icon helpers ───────────────────────────────────────────────────────

export type FileGlyphKind =
  | "archive"
  | "code"
  | "config"
  | "data"
  | "database"
  | "document"
  | "font"
  | "generic"
  | "git"
  | "image"
  | "lock"
  | "package"
  | "pdf"
  | "style"
  | "terminal";

const CODE_EXTENSIONS = new Set([
  "c", "cc", "cjs", "cpp", "cs", "dart", "ex", "exs", "go", "h", "hpp", "html", "java", "js", "jsx", "kt", "lua", "mjs", "php", "py", "r", "rb", "rs", "scala", "svelte", "swift", "ts", "tsx", "vue",
]);
const DATA_EXTENSIONS = new Set(["csv", "ini", "json", "toml", "xml", "yaml", "yml"]);
const DATABASE_EXTENSIONS = new Set(["db", "sqlite", "sqlite3", "sql"]);
const DOCUMENT_EXTENSIONS = new Set(["doc", "docx", "md", "mdx", "rst", "txt"]);
const ARCHIVE_EXTENSIONS = new Set(["7z", "gz", "rar", "tar", "tgz", "zip"]);
const FONT_EXTENSIONS = new Set(["otf", "ttf", "woff", "woff2"]);
const STYLE_EXTENSIONS = new Set(["css", "less", "sass", "scss"]);
const TERMINAL_EXTENSIONS = new Set(["bat", "cmd", "ps1", "sh", "zsh"]);

export const fileGlyphKind = (name: string): FileGlyphKind => {
  const lower = name.toLowerCase();
  const base = lower.includes("/") || lower.includes("\\")
    ? (lower.split(/[/\\]/).pop() ?? lower)
    : lower;
  const ext = base.includes(".") ? base.split(".").pop() ?? "" : "";
  if (base.endsWith(".lock") || base.includes("-lock.") || base === "lockfile") return "lock";
  if (base.startsWith(".git") || base === ".gitattributes" || base === ".gitignore") return "git";
  if (
    base === "package.json"
    || base === "package-lock.json"
    || base === "pnpm-lock.yaml"
    || base === "yarn.lock"
    || base === "cargo.toml"
    || base.startsWith("requirements")
  ) return "package";
  if (
    base.startsWith(".env")
    || base.includes("config")
    || ["dockerfile", "makefile", "pyproject.toml", "compose.yml", "compose.yaml", "tsconfig.json", "jsconfig.json"].includes(base)
  ) return "config";
  if (/^(readme|license|changelog|contributing)(\.|$)/.test(base)) return "document";
  if (ext === "pdf") return "pdf";
  if (CODE_EXTENSIONS.has(ext)) return "code";
  if (STYLE_EXTENSIONS.has(ext)) return "style";
  if (DATA_EXTENSIONS.has(ext)) return "data";
  if (DATABASE_EXTENSIONS.has(ext)) return "database";
  if (DOCUMENT_EXTENSIONS.has(ext)) return "document";
  if (isImagePath(base)) return "image";
  if (ARCHIVE_EXTENSIONS.has(ext)) return "archive";
  if (FONT_EXTENSIONS.has(ext)) return "font";
  if (TERMINAL_EXTENSIONS.has(ext)) return "terminal";
  return "generic";
};

const officialFileIcon = (name: string): IconifyIcon => {
  const lower = name.toLowerCase();
  const ext = lower.includes(".") ? lower.split(".").pop() ?? "" : "";
  if (lower === "package.json" || lower === "package-lock.json") return npmIcon;
  if (lower.startsWith(".git") || lower === "gitignore") return gitIcon;
  if (ext === "pdf") return pdfIcon;
  if (ext === "doc" || ext === "docx") return wordIcon;
  if (["xls", "xlsx", "csv"].includes(ext)) return excelIcon;
  if (["ppt", "pptx"].includes(ext)) return powerpointIcon;
  if (ext === "py") return pythonIcon;
  if (ext === "ts") return tsIcon;
  if (ext === "tsx" || ext === "jsx") return reactIcon;
  if (ext === "js") return jsIcon;
  if (ext === "json") return jsonIcon;
  if (ext === "md" || ext === "mdx") return markdownIcon;
  if (ext === "html") return htmlIcon;
  if (ext === "css" || ext === "scss") return cssIcon;
  if (ext === "ps1") return powershellIcon;
  if (["sh", "bash", "zsh"].includes(ext)) return shellIcon;
  if (["yaml", "yml"].includes(ext)) return yamlIcon;
  if (ext === "toml") return tomlIcon;
  if (["zip", "7z", "rar", "tar", "gz"].includes(ext)) return zipIcon;
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "ico"].includes(ext)) return imageIcon;
  if (["txt", "log"].includes(ext) || lower === "license" || lower === "readme") return textIcon;
  return defaultFileIcon;
};

const FILE_GLYPH_COLORS: Record<FileGlyphKind, string> = {
  archive: "var(--mc-icon-archive, var(--text-muted))",
  code: "var(--mc-icon-code, var(--state-info))",
  config: "var(--mc-icon-config, var(--state-warning))",
  data: "var(--mc-icon-data, var(--accent-primary))",
  database: "var(--mc-icon-database, var(--accent-primary))",
  document: "var(--mc-icon-document, var(--text-muted))",
  font: "var(--mc-icon-font, var(--text-secondary))",
  generic: "var(--mc-icon-generic, var(--text-muted))",
  git: "var(--mc-icon-git, var(--text-muted))",
  image: "var(--mc-icon-media, var(--state-success))",
  lock: "var(--mc-icon-config, var(--state-warning))",
  package: "var(--mc-icon-package, var(--state-warning))",
  pdf: "var(--mc-icon-pdf, var(--state-danger))",
  style: "var(--mc-icon-style, var(--accent-primary))",
  terminal: "var(--mc-icon-terminal, var(--text-secondary))",
};

export const fileGlyphColor = (name: string): string =>
  FILE_GLYPH_COLORS[fileGlyphKind(name)];

export const iconColor = (node: WorkspaceTreeNode): string => {
  if (node.is_dir) return "var(--mc-icon-folder, var(--text-secondary))";
  return fileGlyphColor(node.name);
};

export const fileIcon = (
  name: string,
  options: { size?: number; className?: string } = {},
): ReactNode => {
  const kind = fileGlyphKind(name);
  const size = options.size ?? 16;
  const className = options.className ?? "file-tree-file-icon";
  return (
    <Icon
      icon={officialFileIcon(name)}
      width={size}
      height={size}
      className={className}
      data-file-kind={kind}
      aria-hidden="true"
    />
  );
};

export const folderIcon = (expanded: boolean, size = 16): ReactNode => (
  <Icon
    icon={expanded ? defaultFolderOpenedIcon : defaultFolderIcon}
    width={size}
    height={size}
    className="file-tree-folder-icon"
    aria-hidden="true"
  />
);
