export function workspaceRequestPath(path: string, root: string): string {
  if (path.startsWith("file:")) return path;
  const normalized = path.replaceAll("\\", "/"), base = root.replaceAll("\\", "/").replace(/\/+$/, "");
  const windows = /^[A-Za-z]:/.test(base) || base.startsWith("//");
  const value = windows ? normalized.toLowerCase() : normalized;
  const prefix = windows ? base.toLowerCase() : base;
  return value === prefix ? "." : value.startsWith(prefix + "/") ? normalized.slice(base.length + 1) : path;
}
