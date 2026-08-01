import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { InspectorEntry, InspectorTargetKind } from "../stores/types";

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

export function focusInspectorEntry(entry: InspectorEntry) {
  useAppStore.getState().setInspectorFocus({ kind: entry.targetKind, id: entry.targetId });
  if (entry.payload.diagnostics_deferred === true) {
    sendClientCommand({
      type: "inspector.focus",
      target_kind: entry.targetKind,
      target_id: entry.targetId,
    });
  }
}
