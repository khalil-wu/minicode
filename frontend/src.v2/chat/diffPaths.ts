import {
  normalizeWorkspacePath,
  workspacePathWithin,
  workspacePathsEqual,
} from "../lib/workspace-path";

export function workspaceRelativeDiffPath(path: string, workspaceRoot?: string): string {
  const normalized = normalizePath(path);
  if (!normalized) return "";

  const root = normalizePath(workspaceRoot ?? "");
  if (root) {
    const absolute = relativeFromAbsolutePath(normalized, root);
    if (absolute) return absolute;

  }

  return normalized.replace(/^\.\//, "");
}

function relativeFromAbsolutePath(path: string, root: string): string {
  if (!isAbsolutePath(path) || !isAbsolutePath(root)) return "";
  const normalizedPath = normalizeWorkspacePath(path);
  const normalizedRoot = normalizeWorkspacePath(root);
  if (!workspacePathWithin(normalizedPath, normalizedRoot)) return "";
  if (workspacePathsEqual(normalizedPath, normalizedRoot)) return "";
  const prefixLength = normalizedRoot.endsWith("/")
    ? normalizedRoot.length
    : normalizedRoot.length + 1;
  return normalizedPath.slice(prefixLength);
}

function normalizePath(value: string): string {
  return normalizeWorkspacePath(value);
}

function isAbsolutePath(value: string): boolean {
  return /^[A-Za-z]:\//.test(value) || value.startsWith("/");
}
