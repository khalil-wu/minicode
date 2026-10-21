import { realtimeController } from "../composer/realtime-controller";
import { isReplayedEvent, type ServerEvent } from "../protocol/events";
import { isRealtimeEvent } from "../protocol/realtime-types";
import type { RealtimeItem } from "../protocol/realtime-types";
import { useAppStore } from "../stores";
import { updateMessagesForConversation } from "../stores/shared-helpers";
import type { ChatMessage } from "../stores/types";

const projectItem = (item: RealtimeItem, completed: boolean, existing?: ChatMessage): ChatMessage => ({
  ...existing, id: item.id,
  role: item.type === "transcriptSegment" ? item.role : "system",
  content: item.type === "transcriptSegment" ? item.text : "",
  realtimeItem: item, realtimeCompleted: completed,
  isStreaming: !completed, artifacts: [], timestamp: existing?.timestamp ?? Date.now(), blocks: [],
});

export const handleRealtimeEvent = (event: ServerEvent): boolean => {
  if (!isRealtimeEvent(event)) return false;
  if (!isReplayedEvent(event)) realtimeController.handleEvent(event);
  if (event.type === "thread/realtime/item/started" || event.type === "thread/realtime/item/completed") {
    const completed = event.type === "thread/realtime/item/completed";
    useAppStore.setState(state => updateMessagesForConversation(state, event.conversation_id, messages => {
      const index = messages.findIndex(message => message.id === event.item.id);
      if (index < 0) return [...messages, projectItem(event.item, completed)];
      if (!completed && messages[index].realtimeCompleted) return null;
      const next = [...messages];
      next[index] = projectItem(event.item, completed, messages[index]);
      return next;
    }));
  } else if (event.type === "thread/realtime/item/transcript/delta") {
    useAppStore.setState(state => updateMessagesForConversation(state, event.conversation_id, messages => {
      const index = messages.findIndex(message => message.id === event.itemId);
      const current = messages[index];
      if (current?.realtimeItem?.type !== "transcriptSegment" || current.realtimeCompleted) return null;
      const next = [...messages];
      next[index] = projectItem({ ...current.realtimeItem, text: current.realtimeItem.text + event.delta }, false, current);
      return next;
    }));
  }
  return true;
};
