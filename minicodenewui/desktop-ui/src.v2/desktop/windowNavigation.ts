import { LS, writeLS } from "../stores/shared-helpers";
import { desktop } from "./runtime";

/** A native initial route is applied once, before the renderer connects. */
export function initializeWindowNavigation() {
  const navigation = window.__MINICODE_RUNTIME__?.initialNavigation;
  if (!navigation) return;
  if (!navigation.restoreLastConversation) writeLS(LS.conversation.activeId, navigation.conversationId ?? "");
  desktop()!.windowControls.consumeInitialNavigation();
}
