import { createContext, useContext } from "react";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";

/**
 * Whether the owning right-sidebar panel is the visible tab. Panels stay
 * mounted while hidden so their local state survives; standalone renders
 * default to visible.
 */
export const PanelVisibilityContext = createContext(true);

export const usePanelVisible = (): boolean => useContext(PanelVisibilityContext);

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
