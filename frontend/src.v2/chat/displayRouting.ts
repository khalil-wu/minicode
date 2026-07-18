import { useAppStore } from "../stores";
import type { InspectorTargetKind, RightStackTab } from "../stores/types";
import { displayScopeOf, panelHintOf, requiresAttention, type DisplayRoutable } from "../lib/display-intent";

export { normalizePanelHint } from "../lib/display-intent";

export function maybeAutoRoutePanel(event: DisplayRoutable, fallback?: RightStackTab) {
  const state = useAppStore.getState();
  if (state.rightStackTabLocked) return;
  const tab = panelHintOf(event) ?? fallback ?? null;
  if (!tab) return;
  const scope = displayScopeOf(event);
  const attention = requiresAttention(event);
  if ((tab === "subagents" || scope === "agents") && !attention) {
    return;
  }
  const shouldOpen =
    attention ||
    scope === "inspector";
  if (shouldOpen) {
    state.setRightStackTab(tab, { automatic: true });
  }
}

export function addInspectorPayload(
  targetKind: InspectorTargetKind,
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
