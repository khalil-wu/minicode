import { canonicalWorkspacePath } from "../lib/workspace-display";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { safeJsonParse } from "../lib/safe-parse";

const STORAGE_PREFIX = "minicode.prompt-history.v1";
export const PROMPT_HISTORY_LIMIT = 100;

const storage = (): Storage | null => {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
};

export const promptHistoryWorkspaceKey = (workspaceRoot: string | null | undefined): string => {
  const canonical = canonicalWorkspacePath(workspaceRoot);
  const normalized = normalizeWorkspaceRoot(canonical);
  return normalized || "global";
};

const keyFor = (workspaceRoot: string | null | undefined) =>
  `${STORAGE_PREFIX}:${promptHistoryWorkspaceKey(workspaceRoot)}`;

export const readPromptHistory = (workspaceRoot: string | null | undefined): string[] => {
  const target = storage();
  if (!target) return [];
  try {
    const parsed = safeJsonParse<unknown>(target.getItem(keyFor(workspaceRoot)) || "[]", []);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).slice(0, PROMPT_HISTORY_LIMIT);
  } catch {
    return [];
  }
};

export const appendPromptHistory = (workspaceRoot: string | null | undefined, prompt: string): string[] => {
  const value = prompt.trim();
  if (!value) return readPromptHistory(workspaceRoot);
  const next = [value, ...readPromptHistory(workspaceRoot).filter((item) => item !== value)].slice(0, PROMPT_HISTORY_LIMIT);
  try {
    storage()?.setItem(keyFor(workspaceRoot), JSON.stringify(next));
  } catch {
    // History is a convenience. Sending must never fail because storage is unavailable.
  }
  return next;
};

export const clearPromptHistory = (workspaceRoot: string | null | undefined): void => {
  try {
    storage()?.removeItem(keyFor(workspaceRoot));
  } catch {
    // Ignore restricted or full storage.
  }
};
