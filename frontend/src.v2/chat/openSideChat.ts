import { useAppStore } from "../stores";
import type { PermissionMode } from "../stores/types";

export const newSideChatId = () => `side-${crypto.randomUUID()}`;

/** A header action creates a new ephemeral child; each existing side draft keeps its own owner. */
export function openNewSideChat(owner: { conversationId: string; workspaceRoot: string; permissionMode: PermissionMode }) {
  const state = useAppStore.getState();
  const id = newSideChatId();
  state.ensureSideChat(id, { workspaceRoot: owner.workspaceRoot, permissionMode: owner.permissionMode, parentConversationId: owner.conversationId });
  useAppStore.setState({ activeSideChatId: id });
  state.setRightStackTab("sidechat");
  return id;
}
