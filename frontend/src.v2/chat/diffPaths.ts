export function workspaceRelativeDiffPath(path: string, workspaceRoot?: string): string {
  const normalized = normalizePath(path);
  if (!normalized) return "";

  const root = normalizePath(workspaceRoot ?? "");
  if (root) {
    const absolute = relativeFromAbsolutePath(normalized, root);
    if (absolute) return absolute;

    const rootName = basename(root).toLowerCase();
    const parts = normalized.split("/").filter(Boolean);
    if (
      rootName &&
      parts.length > 1 &&
      parts[0].toLowerCase() === rootName &&
      !isAbsolutePath(normalized)
    ) {
      return parts.slice(1).join("/");
    }
  }

  return normalized.replace(/^\.\//, "");
}

function relativeFromAbsolutePath(path: string, root: string): string {
  if (!isAbsolutePath(path) || !isAbsolutePath(root)) return "";
  const pathLower = trimTrailingSlash(path).toLowerCase();
  const rootLower = trimTrailingSlash(root).toLowerCase();
  if (pathLower === rootLower) return "";
  const prefix = `${rootLower}/`;
  if (!pathLower.startsWith(prefix)) return "";
  return trimTrailingSlash(path).slice(prefix.length);
}

function normalizePath(value: string): string {
  return String(value || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/\/+/g, "/");
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
