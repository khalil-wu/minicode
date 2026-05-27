const CONV_SEGMENT = /^conv_[a-z0-9]+$/i;

export const isInternalConversationName = (value: string): boolean =>
  CONV_SEGMENT.test(value.trim());

export const basename = (path: string): string =>
  path.split(/[/\\]/).filter(Boolean).pop() || path;

export const workspaceDisplayName = (
  path: string | null | undefined,
  fallback = "Current workspace",
): string => {
  const name = basename(path || "");
  if (!name || name === "." || isInternalConversationName(name)) return fallback;
  return name;
};

export const branchDisplayName = (branch: string | null | undefined): string => {
  const value = (branch || "").trim();
  if (!value) return "";
  const normalized = value.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length >= 2 && parts[0] === "minicode" && isInternalConversationName(parts[1])) {
    return "isolated session";
  }
  return value;
};

export const canonicalWorkspacePath = (path: string | null | undefined): string => {
  const value = (path || "").trim();
  if (!value) return "";
  const normalized = value.replace(/\\/g, "/");
  const match = normalized.match(/^(.*)\/\.claude\/worktrees\/conv_[a-z0-9]+$/i);
  if (!match) return value;
  const base = match[1] || "";
  if (!base) return value;
  return value.includes("\\") ? base.replace(/\//g, "\\") : base;
};
