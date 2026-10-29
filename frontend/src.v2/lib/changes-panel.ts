import { useAppStore } from "../stores";
import type { GitReviewRequest, WorkspaceFileOwner } from "../stores/types";
import { draftWindowOwnerId } from "../stores/draft-window-owner";
import { normalizeWorkspacePath } from "./workspace-path";

export function changesWorkbenchOwner(conversationId: string | null, draftId: string | null, workspaceRoot: string): string {
  return conversationId ?? JSON.stringify([draftWindowOwnerId(), draftId ?? "home", normalizeWorkspacePath(workspaceRoot)]);
}

/** A selected local project already owns Git reads before its first Agent turn. */
export function draftChangesReviewRequest(draftId: string | null, workspaceRoot: string): (GitReviewRequest & { fileOwner: WorkspaceFileOwner }) | null {
  if (!workspaceRoot) return null;
  const owner: WorkspaceFileOwner = { scopeId: `home:${normalizeWorkspacePath(workspaceRoot)}`,
    conversationId: null, executionCwd: workspaceRoot, browserRoot: workspaceRoot };
  return { id: `draft-changes:${changesWorkbenchOwner(null, draftId, workspaceRoot)}`, path: "", section: "working",
    workspaceRoot, conversationId: null, fileOwner: owner };
}

export const standaloneChangesRoute = (workbenchOwner: string, owner: WorkspaceFileOwner, comparisonRoot: string): string =>
  `files:${JSON.stringify([workbenchOwner, owner.scopeId, owner.conversationId, owner.environmentId,
    normalizeWorkspacePath(owner.executionCwd), normalizeWorkspacePath(comparisonRoot)])}`;

// Like the existing selection/expansion scopes, explicit open requests survive
// panel remounts. Re-entering a panel must not replay its old file/source request.
const appliedReviewRequests = new Map<string, string>();
export const standaloneChangesRequestPending = (route: string, id: string): boolean => appliedReviewRequests.get(route) !== id;
export function consumeStandaloneChangesRequest(route: string, id: string): boolean {
  if (!standaloneChangesRequestPending(route, id)) return false;
  appliedReviewRequests.delete(route);
  appliedReviewRequests.set(route, id);
  if (appliedReviewRequests.size > 20) appliedReviewRequests.delete(appliedReviewRequests.keys().next().value!);
  return true;
}

export function toggleChangesPanel() {
  const state = useAppStore.getState();
  const maximizedEditor = state.appMode === "code" && state.panelSlots.find(slot => slot.maximized && slot.kind !== "chat");
  if (state.rightPanelOpen && state.rightStackTab === "diff" && !maximizedEditor) {
    state.toggleRightPanel();
    return;
  }
  if (maximizedEditor) state.togglePanelMaximized(maximizedEditor.id);
  state.setRightStackTab("diff");
}
