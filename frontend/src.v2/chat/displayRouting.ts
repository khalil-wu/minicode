import { useAppStore } from "../stores";
import type { RightStackTab } from "../stores/types";
import { displayScopeOf, panelHintOf, requiresAttention, type DisplayRoutable } from "../lib/display-intent";

export { normalizePanelHint } from "../lib/display-intent";

export function maybeAutoRoutePanel(event: DisplayRoutable, fallback?: RightStackTab) {
  const state = useAppStore.getState();
  if (state.rightStackTabLocked) return;
  const tab = panelHintOf(event) ?? fallback ?? null;
  if (!tab) return;
  const scope = displayScopeOf(event);
  const shouldOpen =
    requiresAttention(event) ||
    scope === "agents" ||
    scope === "inspector" ||
    tab === "diff";
  if (shouldOpen) {
    state.setRightStackTab(tab, { automatic: true });
  }
}

export function addInspectorPayload(
  targetKind: "message" | "tool_call" | "artifact" | "file" | "diff" | "subagent" | "budget",
  targetId: string,
  payload: Record<string, unknown>,
) {
  if (!targetId) return;
  useAppStore.getState().addInspectorEntry({
    targetKind,
    targetId,
    payload,
    timestamp: Date.now(),
  });
}
