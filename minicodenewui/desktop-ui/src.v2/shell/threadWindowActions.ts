import { desktop, isDesktop } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import type { ConversationMeta } from "../stores/types";

export const canOpenThreadWindow = (conversation: Pick<ConversationMeta, "archived" | "conversationType" | "environment">) =>
  isDesktop() && typeof desktop()?.windowControls?.openConversation === "function"
    && !conversation.archived && conversation.conversationType !== "side_chat"
    && (!conversation.environment || conversation.environment === "local");

export const threadWindowModifier = (event: { ctrlKey: boolean; metaKey: boolean }) =>
  desktop()!.platformInfo.platform === "darwin" ? event.metaKey : event.ctrlKey;

export async function openThreadWindow(conversationId: string) {
  try { await desktop()!.windowControls.openConversation(conversationId); }
  catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
}
