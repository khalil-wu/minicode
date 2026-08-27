/**
 * Normalize path separators and redundant separators without changing the
 * case of the path.  Case is a filesystem concern and must be applied only
 * by the comparison helpers below.
 */
export const normalizeWorkspacePath = (value: unknown): string => {
  if (typeof value !== "string") return "";
  const raw = value.trim();
  if (!raw) return "";

  const slashed = raw.replace(/\\/g, "/");
  // Keep a UNC prefix distinct from a POSIX root while collapsing repeated
  // separators and lexical aliases.  Besides avoiding duplicate UI identity,
  // resolving `.` and `..` here prevents prefix checks from treating
  // `workspace/../outside` as a workspace child.
  const prefix = slashed.startsWith("//") ? "//" : slashed.startsWith("/") ? "/" : "";
  const driveAbsolute = /^[A-Za-z]:\//.test(slashed);
  const absolute = Boolean(prefix) || driveAbsolute;
  const rawParts = slashed.slice(prefix.length).split(/\/+/).filter(Boolean);
  const protectedParts = prefix === "//" ? Math.min(2, rawParts.length) : driveAbsolute ? 1 : 0;
  const parts: string[] = [];
  for (const part of rawParts) {
    if (part === ".") continue;
    if (part === "..") {
      if (parts.length > protectedParts && parts.at(-1) !== "..") {
        parts.pop();
      } else if (!absolute) {
        parts.push(part);
      }
      continue;
    }
    parts.push(part);
  }
  const normalized = `${prefix}${parts.join("/")}${driveAbsolute && parts.length === 1 ? "/" : ""}`;
  if (normalized === "/" || normalized === "//" || /^[A-Za-z]:\/$/.test(normalized)) {
    return normalized;
  }
  if (!normalized && !absolute) return ".";
  return normalized.replace(/\/+$/, "");
};

export const isWindowsLikeWorkspacePath = (value: unknown): boolean => {
  const normalized = normalizeWorkspacePath(value);
  // Drive-letter and UNC roots identify Windows-style paths, whose filesystem
  // lookup is case-insensitive. POSIX roots remain case-sensitive.
  return /^[A-Za-z]:\//.test(normalized) || normalized.startsWith("//");
};

export const workspacePathComparisonKey = (value: unknown): string => {
  const normalized = normalizeWorkspacePath(value);
  return isWindowsLikeWorkspacePath(normalized) ? normalized.toLowerCase() : normalized;
};

/** Compare a file path using the owning workspace's filesystem semantics. */
export const workspaceFilePathComparisonKey = (value: unknown, workspaceRoot: unknown): string => {
  let normalized = normalizeWorkspacePath(value);
  const root = normalizeWorkspacePath(workspaceRoot);
  if (normalized && root && !isAbsoluteWorkspacePath(normalized)) {
    normalized = normalizeWorkspacePath(root.endsWith("/") ? `${root}${normalized}` : `${root}/${normalized}`);
  }
  return isWindowsLikeWorkspacePath(root) || isWindowsLikeWorkspacePath(normalized)
    ? normalized.toLowerCase()
    : normalized;
};

const isAbsoluteWorkspacePath = (value: string): boolean =>
  value.startsWith("/") || /^[A-Za-z]:\//.test(value);

export const workspaceFilePathsEqual = (
  left: unknown,
  right: unknown,
  workspaceRoot: unknown,
): boolean => {
  const leftKey = workspaceFilePathComparisonKey(left, workspaceRoot);
  const rightKey = workspaceFilePathComparisonKey(right, workspaceRoot);
  return Boolean(leftKey && rightKey && leftKey === rightKey);
};

export const normalizeWorkspaceRoot = (value: unknown): string =>
  workspacePathComparisonKey(value);

/** Compare workspace identities, including the valid "no workspace" state. */
export const workspaceRootsEqual = (left: unknown, right: unknown): boolean =>
  normalizeWorkspaceRoot(left) === normalizeWorkspaceRoot(right);

export const workspacePathsEqual = (left: unknown, right: unknown): boolean => {
  const leftKey = workspacePathComparisonKey(left);
  const rightKey = workspacePathComparisonKey(right);
  return Boolean(leftKey && rightKey && leftKey === rightKey);
};

export const workspacePathWithin = (path: unknown, root: unknown): boolean => {
  const pathKey = workspacePathComparisonKey(path);
  const rootKey = workspacePathComparisonKey(root);
  if (!pathKey || !rootKey) return false;
  if (pathKey === rootKey) return true;
  const prefix = rootKey.endsWith("/") ? rootKey : `${rootKey}/`;
  return pathKey.startsWith(prefix);
};
