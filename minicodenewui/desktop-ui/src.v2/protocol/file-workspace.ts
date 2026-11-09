import { getWebSocket } from "../hooks/useWebSocket";
import type { AppStore, GitChangeFile, GitComparisonState, GitReviewSelection, WorkspaceFileOwner, WorkspaceFileTab } from "../stores/types";
import type { GitReviewOptions } from "../chat/diff/git-review-options";
import { normalizeWorkspacePath, workspaceRootsEqual } from "../lib/workspace-path";
import { ApiError, apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import { workspaceRequestPath } from "./workspace-request-path";
import type { ConversationBrowserTree } from "./conversation-browser";
import type { WorkspaceFilePreviewResponse, WorkspaceGitStatusResponse } from "./workspace";

export function fileWorkspaceScope(state: Pick<AppStore, "conversationId" | "workingDirectory">): string {
  return state.conversationId ? `conversation:${state.conversationId}` : `home:${normalizeWorkspacePath(state.workingDirectory)}`;
}

export function currentFileWorkspaceOwner(state: AppStore): WorkspaceFileOwner {
  return fileWorkspaceOwner(state, state.conversationId);
}

export function fileWorkspaceOwner(state: Pick<AppStore, "conversations" | "workingDirectory">,
  conversationId: string | null, executionCwd?: string): WorkspaceFileOwner {
  const conversation = state.conversations.find(value => value.id === conversationId);
  const bound = conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot;
  const cwd = executionCwd ?? (bound || state.workingDirectory);
  return { scopeId: conversationId ? `conversation:${conversationId}` : `home:${normalizeWorkspacePath(cwd)}`,
    conversationId, executionCwd: cwd,
    browserRoot: conversation?.projectlessBrowserRoot && workspaceRootsEqual(cwd, bound)
      ? conversation.projectlessBrowserRoot : cwd };
}

export function workspaceFileAbsolutePath(owner: WorkspaceFileOwner, path: string): string {
  if (path.startsWith("file:")) return path;
  const value = normalizeWorkspacePath(path);
  if (value.startsWith("/") || /^[a-z]:\//i.test(value)) return value;
  const root = owner.browserRoot.replace(/[\\/]+$/, "");
  if (root.startsWith("file:")) {
    return new URL(value.split("/").map(encodeURIComponent).join("/"), `${root}/`).href;
  }
  return normalizeWorkspacePath(`${root}/${value}`);
}

/** Refresh only an authoritative browser root, retaining the exact execution owner and file. */
export function reconcileWorkspaceFileReference(owner: WorkspaceFileOwner, path: string,
  state: Pick<AppStore, "conversations" | "workingDirectory">): { owner: WorkspaceFileOwner; path: string } {
  const conversation = state.conversations.find(value => value.id === owner.conversationId);
  if (!owner.conversationId || !conversation?.projectlessBrowserRoot) return { owner, path };
  const canonical = fileWorkspaceOwner(state, owner.conversationId);
  if (!workspaceRootsEqual(canonical.executionCwd, owner.executionCwd)
    || workspaceRootsEqual(canonical.browserRoot, owner.browserRoot)) return { owner, path };
  const absolute = workspaceFileAbsolutePath(owner, path);
  return { owner: { ...owner, browserRoot: canonical.browserRoot },
    path: workspaceRequestPath(absolute, canonical.browserRoot).replaceAll("\\", "/") };
}

export function reconcileWorkspaceFileTab(tab: WorkspaceFileTab, state: Pick<AppStore, "conversations" | "workingDirectory">): WorkspaceFileTab {
  const reference = reconcileWorkspaceFileReference(tab.owner, tab.path, state);
  return reference.owner === tab.owner ? tab : { ...tab, ...reference };
}

function fileUrl(owner: WorkspaceFileOwner, method: string, path?: string, params: Record<string, string> = {}) {
  const browserMethod = ["tree", "preview", "raw", "search"].includes(method) && owner.conversationId;
  const url = new URL(`${apiBase()}/api/workspace/${browserMethod ? `browser-${method}` : method}`);
  if (owner.conversationId) {
    const sessionId = getWebSocket()?.sessionId;
    if (!sessionId) throw new Error("文件工作区需要已连接的会话。");
    url.searchParams.set("session_id", sessionId);
    url.searchParams.set("conversation_id", owner.conversationId);
    if (owner.environmentId) url.searchParams.set("environment_id", owner.environmentId);
  }
  url.searchParams.set(browserMethod ? "execution_cwd" : "workspace_root", owner.conversationId ? owner.executionCwd : owner.browserRoot);
  if (path !== undefined) url.searchParams.set("path", workspaceRequestPath(path, owner.browserRoot));
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return url;
}

async function response(owner: WorkspaceFileOwner, method: string, path?: string, signal?: AbortSignal, params?: Record<string, string>) {
  const result = await fetchWithTimeout(fileUrl(owner, method, path, params), { headers: authHeaders(), signal });
  if (!result.ok) throw new ApiError(result.status, errorMessageFromResponseText(await result.text(), result.statusText));
  return result;
}

export async function readFileWorkspaceTree(owner: WorkspaceFileOwner, path = ".", signal?: AbortSignal): Promise<ConversationBrowserTree> {
  return (await response(owner, "tree", path, signal)).json();
}
export async function searchFileWorkspace(owner: WorkspaceFileOwner, query: string, signal?: AbortSignal): Promise<{ results: { path: string; name?: string; is_dir?: boolean }[] }> {
  return (await response(owner, "search", undefined, signal, { query, limit: "100" })).json();
}
export async function readFileWorkspacePreview(owner: WorkspaceFileOwner, path: string, signal?: AbortSignal): Promise<WorkspaceFilePreviewResponse> {
  return (await response(owner, "preview", path, signal)).json();
}
export async function readFileWorkspaceOriginal(owner: WorkspaceFileOwner, path: string, signal?: AbortSignal): Promise<Blob> {
  return (await response(owner, "raw", path, signal)).blob();
}
export type FileWorkspaceBranches = GitReviewOptions;
export async function readFileWorkspaceBranches(owner: WorkspaceFileOwner, signal?: AbortSignal, baseBranch?: string, comparisonRoot = owner.executionCwd): Promise<FileWorkspaceBranches> {
  const params: Record<string, string> = {};
  if (baseBranch) params.base_branch = baseBranch;
  if (comparisonRoot !== owner.executionCwd) params.path = comparisonRoot;
  return (await response(owner, "git/branches", undefined, signal, params)).json();
}
export async function readFileWorkspaceGitStatus(owner: WorkspaceFileOwner, signal?: AbortSignal): Promise<WorkspaceGitStatusResponse> {
  return (await response(owner, "git/status", undefined, signal)).json();
}
export async function readFileWorkspaceDiff(owner: WorkspaceFileOwner, path: string, baseRef: string, signal?: AbortSignal): Promise<{ diff: string; is_git_repo?: boolean; error?: string }> {
  const file = workspaceRequestPath(workspaceFileAbsolutePath(owner, path), owner.executionCwd);
  const value = await (await response(owner, "git/diff", undefined, signal, { file, base_ref: baseRef })).json();
  if (value.error) throw new Error(value.error);
  return value;
}

export interface FileWorkspaceComparison extends GitComparisonState { isGitRepo: boolean; diff: string }
export async function readFileWorkspaceChanges(owner: WorkspaceFileOwner, selection: GitReviewSelection, hideWhitespace: boolean, signal?: AbortSignal, comparisonRoot = owner.executionCwd): Promise<FileWorkspaceComparison> {
  const params: Record<string, string> = { source: selection.source, hide_whitespace: String(hideWhitespace) };
  if (selection.baseBranch) params.base_branch = selection.baseBranch;
  if (selection.commitSha) params.commit_sha = selection.commitSha;
  if (comparisonRoot !== owner.executionCwd) params.path = comparisonRoot;
  const value = await (await response(owner, "git/diff", undefined, signal, params)).json();
  return { ...selection, loading: false, isGitRepo: value.is_git_repo, status: value.status, reason: value.reason,
    comparison: value.comparison, diff: value.diff,
    files: value.files.map((file: { path: string; old_path?: string; is_binary?: boolean; old_mode?: string; new_mode?: string; old_oid?: string; new_oid?: string } & GitChangeFile) => ({
      ...file, oldPath: file.old_path, isBinary: file.is_binary, oldMode: file.old_mode, newMode: file.new_mode,
      oldOid: file.old_oid, newOid: file.new_oid,
    })) };
}

export async function readFileWorkspaceApplyPatch(owner: WorkspaceFileOwner, selection: GitReviewSelection, comparisonRoot = owner.executionCwd): Promise<string> {
  const params: Record<string, string> = { source: selection.source, for_apply: "true" };
  if (selection.baseBranch) params.base_branch = selection.baseBranch;
  if (selection.commitSha) params.commit_sha = selection.commitSha;
  if (comparisonRoot !== owner.executionCwd) params.path = comparisonRoot;
  return (await (await response(owner, "git/diff", undefined, undefined, params)).json()).diff;
}
