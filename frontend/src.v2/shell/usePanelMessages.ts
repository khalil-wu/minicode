import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import { usePanelVisible } from "./panelVisibility";

const HIDDEN_MESSAGES: ChatMessage[] = [];

/**
 * Transcript for a panel that derives heavy state from it. A hidden panel
 * reads one stable empty list, so stream deltas stop rebuilding its derived
 * state until the user shows the tab again, which then renders once.
 */
export function usePanelMessages(): ChatMessage[] {
  const visible = usePanelVisible();
  return useAppStore((state) => (visible ? state.messages : HIDDEN_MESSAGES));
}
