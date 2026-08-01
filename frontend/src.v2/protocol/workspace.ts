import { apiBase, authHeaders } from "./api";

export interface WorkspaceFileResponse {
  path: string;
  content: string;
  content_hash?: string;
  size?: number;
  size_bytes?: number;
  modified_at?: string;
  language_hint?: string;
  mime?: string;
}

export type WorkspaceCompareWriteResult =
  | { ok: true; file: WorkspaceFileResponse }
  | { ok: false; conflict: true; actualHash?: string; message: string }
  | { ok: false; conflict: false; message: string };

export interface WorkspaceTreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes?: number | null;
  modified_at?: string;
  has_children?: boolean;
  children?: WorkspaceTreeNode[];
}

interface WorkspaceTreeEntry {
  name?: string;
  path?: string;
  is_dir?: boolean;
  isDirectory?: boolean;
  size_bytes?: number | null;
  sizeBytes?: number | null;
  modified_at?: string;
  modifiedAt?: string;
  has_children?: boolean;
  hasChildren?: boolean;
}

interface WorkspaceTreePayload {
  name?: string;
  path?: string;
  requested_path?: string;
  requestedPath?: string;
  workspace_root?: string;
  workspaceRoot?: string;
  is_dir?: boolean;
  children?: WorkspaceTreeNode[];
  entries?: WorkspaceTreeEntry[];
}

const ws = (
  path: string,
  workspaceRoot: string,
  params: Record<string, string | number | boolean | undefined> = {},
): string => {
  const root = workspaceRoot.trim();
  if (!root) throw new Error("Workspace folder is missing.");
  const url = new URL(`${apiBase()}/api/workspace${path.startsWith("/") ? path : `/${path}`}`);
  url.searchParams.set("workspace_root", root);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }
  return url.toString();
};

export const readWorkspaceFile = async (path: string, workspaceRoot: string): Promise<WorkspaceFileResponse | null> => {
  try {
    const r = await fetch(ws("/file", workspaceRoot, { path }), { headers: authHeaders() });
    if (!r.ok) {
      const detail = await errorMessageFromWorkspaceResponse(r);
      throw new Error(detail || `Workspace file request failed (${r.status} ${r.statusText || "error"}).`);
    }
    return (await r.json()) as WorkspaceFileResponse;
  } catch (err) {
    if (err instanceof Error && /workspace folder is missing|too large|only utf-8|permission denied|outside workspace/i.test(err.message)) {
      throw err;
    }
    return null;
  }
};

export const writeWorkspaceFile = async (
  path: string,
  content: string,
  workspaceRoot: string,
): Promise<boolean> => {
  try {
    const r = await fetch(ws("/file", workspaceRoot), {
      method: "PUT",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ path, content }),
    });
    return r.ok;
  } catch {
    return false;
  }
};

const conflictPayload = async (response: Response): Promise<WorkspaceCompareWriteResult> => {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (detail && typeof detail === "object") {
      return {
        ok: false,
        conflict: true,
        actualHash: typeof detail.actual_hash === "string" ? detail.actual_hash : undefined,
        message: typeof detail.message === "string" ? detail.message : "File has changed on disk.",
      };
    }
    return {
      ok: false,
      conflict: true,
      message: typeof detail === "string" ? detail : "File has changed on disk.",
    };
  } catch {
    return { ok: false, conflict: true, message: "File has changed on disk." };
  }
};

export const compareWriteWorkspaceFile = async (
  path: string,
  expectedHash: string,
  content: string,
  workspaceRoot: string,
): Promise<WorkspaceCompareWriteResult> => {
  try {
    const r = await fetch(ws("/file/compare-write", workspaceRoot), {
      method: "PUT",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ path, expected_hash: expectedHash, content }),
    });
    if (r.ok) {
      return { ok: true, file: (await r.json()) as WorkspaceFileResponse };
    }
    if (r.status === 409) {
      return conflictPayload(r);
    }
    return { ok: false, conflict: false, message: `Save failed (${r.status})` };
  } catch {
    return { ok: false, conflict: false, message: "Save failed: connection is offline." };
  }
};

export const createWorkspaceDirectory = async (path: string, workspaceRoot: string): Promise<boolean> => {
  try {
    const r = await fetch(ws("/directory", workspaceRoot), {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ path }),
    });
    return r.ok;
  } catch {
    return false;
  }
};

export const renameWorkspacePath = async (
  path: string,
  newPath: string,
  workspaceRoot: string,
): Promise<boolean> => {
  try {
    const r = await fetch(ws("/rename", workspaceRoot), {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ path, new_path: newPath }),
    });
    return r.ok;
  } catch {
    return false;
  }
};

export const deleteWorkspacePath = async (
  path: string,
  workspaceRoot: string,
  recursive = false,
): Promise<boolean> => {
  try {
    const r = await fetch(
      ws("/path", workspaceRoot, { path, recursive }),
      { method: "DELETE", headers: authHeaders() },
    );
    return r.ok;
  } catch {
    return false;
  }
};

export const listWorkspaceTree = async (
  workspaceRoot: string,
  path: string = ".",
): Promise<WorkspaceTreeNode | null> => {
  try {
    const r = await fetch(ws("/tree", workspaceRoot, { path }), { headers: authHeaders() });
    if (!r.ok) {
      const detail = await errorMessageFromWorkspaceResponse(r);
      throw new Error(detail || `Workspace tree request failed (${r.status} ${r.statusText || "error"}).`);
    }
    const tree = normalizeWorkspaceTree(await r.json(), path);
    if (!tree) throw new Error("Workspace API returned an invalid tree payload.");
    return tree;
  } catch (err) {
    if (err instanceof Error) {
      if (err.name === "TypeError") {
        throw new Error(`Could not load file tree: API request failed. ${err.message}`);
      }
      if (/^Could not load file tree:/i.test(err.message)) throw err;
      throw new Error(`Could not load file tree: ${err.message}`);
    }
    throw new Error("Could not load file tree: API request failed.");
  }
};

const errorMessageFromWorkspaceResponse = async (response: Response): Promise<string> => {
  const fallback = `Workspace tree request failed (${response.status} ${response.statusText || "error"}).`;
  const text = await response.text().catch(() => "");
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  try {
    const payload = JSON.parse(trimmed) as unknown;
    if (payload && typeof payload === "object") {
      const detail = (payload as { detail?: unknown; message?: unknown; error?: unknown }).detail
        ?? (payload as { message?: unknown }).message
        ?? (payload as { error?: unknown }).error;
      if (Array.isArray(detail)) {
        const messages = detail.map((item) => {
          if (item && typeof item === "object" && "msg" in item) return String((item as { msg?: unknown }).msg);
          return String(item);
        }).filter(Boolean);
        return messages.join("; ") || fallback;
      }
      if (detail != null) return String(detail);
    }
  } catch {
    /* not json */
  }
  return trimmed || fallback;
};

const normalizeWorkspaceTree = (payload: unknown, fallbackPath: string): WorkspaceTreeNode | null => {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as WorkspaceTreePayload;
  const nodePath = value.path ?? value.requested_path ?? value.requestedPath ?? fallbackPath;
  const nodeName = value.name ?? nodePath.split(/[/\\]/).filter(Boolean).pop() ?? nodePath;
  if (Array.isArray(value.children)) {
    return {
      name: nodeName,
      path: nodePath,
      is_dir: value.is_dir ?? true,
      children: value.children,
    };
  }
  if (Array.isArray(value.entries)) {
    return {
      name: nodeName,
      path: nodePath,
      is_dir: true,
      children: value.entries.map((entry) => {
        const entryPath = entry.path ?? entry.name ?? "";
        return {
          name: entry.name ?? entryPath.split(/[/\\]/).filter(Boolean).pop() ?? entryPath,
          path: entryPath,
          is_dir: Boolean(entry.is_dir ?? entry.isDirectory),
          size_bytes: entry.size_bytes ?? entry.sizeBytes,
          modified_at: entry.modified_at ?? entry.modifiedAt,
          has_children: Boolean(entry.has_children ?? entry.hasChildren),
          children: entry.is_dir || entry.isDirectory ? [] : undefined,
        };
      }),
    };
  }
  return {
    name: nodeName,
    path: nodePath,
    is_dir: value.is_dir ?? true,
    children: [],
  };
};

export interface WorkspaceSearchResult {
  path: string;
  name: string;
  score: number;
  kind?: "file" | "folder";
}

export const searchWorkspaceFiles = async (
  workspaceRoot: string,
  query: string,
  limit: number = 20,
  kind: "file" | "folder" | "all" = "file",
): Promise<WorkspaceSearchResult[]> => {
  try {
    const r = await fetch(
      ws("/search", workspaceRoot, { query, limit, kind }),
      { headers: authHeaders() },
    );
    if (!r.ok) return [];
    const data = await r.json();
    return data.results ?? [];
  } catch {
    return [];
  }
};

export interface WorkspaceGitWorktreeResponse {
  current_path: string;
  current_branch?: string | null;
  is_worktree?: boolean;
  main_repo_path?: string | null;
  common_git_dir?: string | null;
  worktree_count?: number;
  worktrees?: {
    path: string;
    branch?: string | null;
    commit?: string;
    is_main?: boolean;
    is_current?: boolean;
    is_detached?: boolean;
    is_isolated?: boolean;
    can_remove?: boolean;
  }[];
  error?: string;
}

export const fetchWorkspaceGitWorktree = async (workspaceRoot: string, path = ""): Promise<WorkspaceGitWorktreeResponse | null> => {
  try {
    const r = await fetch(ws("/git/worktree", workspaceRoot, { path }), { headers: authHeaders() });
    if (!r.ok) return null;
    return (await r.json()) as WorkspaceGitWorktreeResponse;
  } catch {
    return null;
  }
};

export interface WorkspaceGitStatusResponse {
  branch: string;
  modified: string[];
  staged: string[];
  untracked: string[];
  error?: string;
}

export const fetchWorkspaceGitStatus = async (workspaceRoot: string, path = ""): Promise<WorkspaceGitStatusResponse | null> => {
  try {
    const r = await fetch(ws("/git/status", workspaceRoot, { path }), { headers: authHeaders() });
    if (!r.ok) return null;
    return (await r.json()) as WorkspaceGitStatusResponse;
  } catch {
    return null;
  }
};

export const fetchWorkspaceGitDiff = async (workspaceRoot: string, file = "", path = ""): Promise<{ diff: string; error?: string } | null> => {
  try {
    const r = await fetch(
      ws("/git/diff", workspaceRoot, { file, path }),
      { headers: authHeaders() },
    );
    if (!r.ok) return null;
    return (await r.json()) as { diff: string; error?: string };
  } catch {
    return null;
  }
};

export const switchWorkspaceGitWorktree = async (
  workspaceRoot: string,
  path: string,
): Promise<{ success: boolean; project?: { root_path?: string; name?: string }; error?: string } | null> => {
  try {
    const r = await fetch(ws("/git/worktree/switch", workspaceRoot), {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ path }),
    });
    if (!r.ok) return null;
    return (await r.json()) as { success: boolean; project?: { root_path?: string; name?: string }; error?: string };
  } catch {
    return null;
  }
};

export const removeWorkspaceGitWorktree = async (
  workspaceRoot: string,
  path: string,
  force = false,
): Promise<{ removed: boolean; path: string; branch?: string; error?: string } | null> => {
  try {
    const r = await fetch(
      ws("/git/worktree", workspaceRoot, { path, force }),
      { method: "DELETE", headers: authHeaders() },
    );
    if (!r.ok) return null;
    return (await r.json()) as { removed: boolean; path: string; branch?: string; error?: string };
  } catch {
    return null;
  }
};
