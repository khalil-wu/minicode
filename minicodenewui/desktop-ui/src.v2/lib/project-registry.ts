import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import type { WorkspaceRecentProjectPayload } from "../protocol/workspace-types";
import { useAppStore } from "../stores";
import type { ConversationMeta, RecentWorkspaceProjection } from "../stores/types";
import type { ProjectAppearance } from "./project-appearance";
import { workspaceRootsEqual } from "./workspace-path";

export const projectSourceFolders = (project: RecentWorkspaceProjection): string[] => project.sourceFolders ?? [project.path];
export const projectWorkspaceRoots = (project: RecentWorkspaceProjection): string[] => {
  const sources = projectSourceFolders(project);
  return sources.length ? sources : [project.managedWorkspaceRoot || project.path];
};
export const projectExecutionRoot = (project: RecentWorkspaceProjection): string => projectWorkspaceRoots(project)[0];
export const projectOwnsWorkspace = (project: RecentWorkspaceProjection, root: string): boolean => projectWorkspaceRoots(project).some(folder => workspaceRootsEqual(folder, root));
export const projectOwnsConversation = (project: RecentWorkspaceProjection, conversation: Pick<ConversationMeta, "projectId" | "worktreePath" | "workspaceRoot">): boolean =>
  conversation.projectId !== undefined && conversation.projectId !== null
    ? workspaceRootsEqual(project.path, conversation.projectId)
    : projectOwnsWorkspace(project, conversation.worktreePath || conversation.workspaceRoot || "");

export const projectProjection = (project: WorkspaceRecentProjectPayload): RecentWorkspaceProjection => ({ path: project.path, name: project.name,
  projectType: project.project_type, lastOpened: project.last_opened,
  ...(project.source_folders !== undefined ? { sourceFolders: project.source_folders } : {}), ...(project.pinned !== undefined ? { pinned: project.pinned } : {}),
  ...(project.managed_workspace_root !== undefined ? { managedWorkspaceRoot: project.managed_workspace_root } : {}),
  ...(project.appearance !== undefined ? { appearance: project.appearance } : {}), ...(project.named !== undefined ? { named: project.named } : {}) });

export async function saveProject(project: { path?: string; name?: string; source_folders?: string[]; pinned?: boolean; appearance?: ProjectAppearance | null }) {
  const response = await fetchWithTimeout(new URL("/api/projects", apiBase()), { method: project.path ? "PATCH" : "POST",
    headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify(project) });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  const payload = await response.json() as { projects: WorkspaceRecentProjectPayload[]; project: WorkspaceRecentProjectPayload };
  useAppStore.getState().setRecentWorkspaces(payload.projects.map(projectProjection));
  return projectProjection(payload.project);
}
