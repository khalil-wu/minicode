import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import type { WorkspaceIndexSession } from "./useWorkspaceModelIndex";

// The transport can notify mounted consumers without importing Monaco or
// initializing an editor in a window that has never opened one.
export const sharedWorkspaceIndexes = new Map<string, { session: WorkspaceIndexSession; references: number }>();

export function invalidateMountedWorkspaceIndex(workspaceRoot: string, instanceId: string, revision: number): void {
  sharedWorkspaceIndexes.get(normalizeWorkspaceRoot(workspaceRoot))?.session.invalidated(instanceId, revision);
}
