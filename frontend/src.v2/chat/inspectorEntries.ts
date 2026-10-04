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
  const state = useAppStore.getState();
  const rawOwner = entry.conversationId ?? entry.payload.conversation_id ?? entry.payload.conversationId;
  const conversationId = typeof rawOwner === "string" ? rawOwner.trim() : "";
  state.setInspectorFocus({ kind: entry.targetKind, id: entry.targetId, conversationId });
  if (entry.payload.diagnostics_deferred === true) {
    if (!conversationId) return;
    const conversation = state.conversations.find((item) => item.id === conversationId);
    sendClientCommand({
      type: "inspector.focus",
      target_kind: entry.targetKind,
      target_id: entry.targetId,
      conversation_id: conversationId,
      workspace_root: conversation?.worktreePath || conversation?.workspaceRoot
        || (conversationId === state.conversationId ? state.workingDirectory : undefined) || undefined,
    });
  }
}
