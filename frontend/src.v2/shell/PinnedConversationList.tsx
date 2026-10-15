import { cloneElement, type ReactElement } from "react";
import type { TreeConversation } from "./ConversationsTab";
import type { SessionRowProps } from "./SessionRow";
import { SidebarSortableList, moveSidebarBefore } from "./SidebarSortableList";

export const movePinnedBefore = moveSidebarBefore;
type Props = {
  conversations: TreeConversation[];
  renderRow: (conversation: TreeConversation, scene?: string) => ReactElement<SessionRowProps>;
  onReorder: (movedId: string, beforeId: string | null, visibleIds: string[]) => Promise<boolean>;
  renamingId: string | null;
  onDragStart: () => void;
};
export const PinnedConversationList = ({ conversations, renderRow, onReorder, renamingId, onDragStart }: Props) => {
  const byId = new Map(conversations.map(conversation => [conversation.id, conversation]));
  return <SidebarSortableList identifiers={conversations.map(conversation => conversation.id)} scope="pinned" ariaLabel="已固定聊天"
    renderRow={(identifier, binding) => cloneElement(renderRow(byId.get(identifier)!, "pinned"), binding)}
    renderOverlay={identifier => renderRow(byId.get(identifier)!, "pinned-overlay")}
    onReorder={onReorder} renamingId={renamingId} onDragStart={onDragStart} />;
};
