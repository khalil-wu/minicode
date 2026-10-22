import type { SessionSnapshotPayload } from "../src.v2/protocol/common-types";

// Canonical empty-session fields emitted by build_session_synced_payload.
// A transport OPEN alone deliberately does not establish this snapshot.
export const initialSessionSyncSnapshot = (
  sessionId: string,
  commandId: string | undefined,
  session: SessionSnapshotPayload = {
    active_conversation_id: null, active_stream_conversation_ids: [], selected_model: "fixture-model",
  },
) => ({
  type: "session.synced", session_id: sessionId, synced: true, protocol_version: "1.0.0",
  client_command_id: commandId, active_conversation_id: session.active_conversation_id,
  current_model: session.selected_model, model: session.selected_model,
  current_seq: 0, last_seq: 0, replayed_events: 0, event_log_gap: false,
  snapshot_required: false, cursor_reset: false, requested_last_seq: 0, session,
});
