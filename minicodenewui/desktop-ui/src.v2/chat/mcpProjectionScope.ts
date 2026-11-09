import { workspaceRootsEqual } from "../lib/workspace-path";

export const mcpProjectionMatches = (
  event: { conversation_id?: string; workspace_root?: string },
  conversationId: string | null,
  workspaceRoot: string,
): boolean => {
  if (event.conversation_id === undefined && event.workspace_root === undefined) return true;
  return event.conversation_id === (conversationId || "")
    && workspaceRootsEqual(event.workspace_root || "", workspaceRoot);
};
