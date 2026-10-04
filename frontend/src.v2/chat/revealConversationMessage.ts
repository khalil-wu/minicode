import { useAppStore } from "../stores";
import { uniqueMessageId } from "../stores/shared-helpers";
import { loadEarlierConversationMessages } from "./historyPagination";
import type { ChatSlice } from "../stores/types";

export function revealConversationMessage(conversationId: string, messageId: string): void {
  const state = useAppStore.getState();
  useAppStore.setState({ messageRevealTarget: { conversationId, messageId, requestId: uniqueMessageId("reveal") } });
  if (state.conversationId !== conversationId) state.requestConversationSwitch(conversationId);
  const chat = useAppStore.getState().panelSlots.find((slot) => slot.kind === "chat");
  if (chat) useAppStore.getState().focusPanel(chat.id);
}

/** Follow the existing history cursor until the actual producing message is loaded. */
export async function loadRevealMessage(target: NonNullable<ChatSlice["messageRevealTarget"]>): Promise<"found" | "missing" | "cancelled" | "failed"> {
  for (;;) {
    const state = useAppStore.getState();
    if (state.messageRevealTarget?.requestId !== target.requestId || state.conversationId !== target.conversationId) return "cancelled";
    if (state.messages.some((message) => message.id === target.messageId)) return "found";
    const page = state.conversationHistoryPages[target.conversationId];
    if (!page?.hasMore) return "missing";
    await loadEarlierConversationMessages(target.conversationId);
    const current = useAppStore.getState();
    if (current.messageRevealTarget?.requestId !== target.requestId || current.conversationId !== target.conversationId) return "cancelled";
    if (current.messages.some((message) => message.id === target.messageId)) return "found";
    if (current.conversationHistoryPages[target.conversationId]?.beforeMessageId === page.beforeMessageId) return "failed";
  }
}
