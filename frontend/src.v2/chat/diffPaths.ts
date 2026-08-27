import {
  isWindowsLikeWorkspacePath,
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

    const rootName = basename(root);
    const parts = normalized.split("/").filter(Boolean);
    const compare = (value: string): string =>
      isWindowsLikeWorkspacePath(root) ? value.toLowerCase() : value;
    if (
      rootName &&
      parts.length > 1 &&
      compare(parts[0]) === compare(rootName) &&
      !isAbsolutePath(normalized)
    ) {
      return parts.slice(1).join("/");
    }
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

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function basename(value: string): string {
  return trimTrailingSlash(value).split("/").filter(Boolean).pop() ?? "";
}

function isAbsolutePath(value: string): boolean {
  return /^[A-Za-z]:\//.test(value) || value.startsWith("/");
}
