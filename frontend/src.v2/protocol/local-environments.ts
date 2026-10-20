import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import { appendWorkspaceOwner, workspaceRequestOwner } from "./workspace-owner";

export type EnvironmentPlatform = "darwin" | "linux" | "win32";
export type EnvironmentScript = { script: string; darwin?: { script: string }; linux?: { script: string }; win32?: { script: string } };
export type LocalEnvironmentAction = { name: string; icon?: "tool" | "run" | "debug" | "test" | null; command: string; platform?: EnvironmentPlatform };
export type LocalEnvironment = { version: number; name: string; setup: EnvironmentScript; cleanup: EnvironmentScript; actions: LocalEnvironmentAction[] };
export type EnvironmentDocument = { config_path: string; revision: string; environment: LocalEnvironment | null };
export type ProjectEnvironments = { workspace_root: string; selected_config_path: string; default_config_path: string; configs: EnvironmentDocument[]; worktree_path: string };
export type ActiveLocalEnvironment = EnvironmentDocument & { actions: Array<LocalEnvironmentAction & { index: number }> };

export async function environmentRequest<T>(query: Record<string, string> = {}, body?: unknown, path = "", signal?: AbortSignal): Promise<T> {
  const url = new URL(`/api/settings/local-environments${path}`, apiBase());
  for (const [key, value] of Object.entries(query)) if (value) url.searchParams.set(key, value);
  const response = await fetchWithTimeout(url, { method: body === undefined ? "GET" : "PUT", headers: authHeaders(body === undefined ? undefined : { "content-type": "application/json" }), body: body === undefined ? undefined : JSON.stringify(body), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<T>;
}

export async function activeLocalEnvironment(workspaceRoot: string, signal: AbortSignal): Promise<ActiveLocalEnvironment> {
  const url = new URL("/api/workspace/local-environment", apiBase());
  url.searchParams.set("workspace_root", workspaceRoot);
  appendWorkspaceOwner(url, workspaceRequestOwner());
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<ActiveLocalEnvironment>;
}
