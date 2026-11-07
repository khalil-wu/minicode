# Thread read state

Conversation list and read projections include `read_state`:

```json
{
  "firstUnread": { "type": "turn", "turnId": "run-id" },
  "revision": "opaque-receipt-version"
}
```

`firstUnread` is `null` for a read conversation, `{ "type": "threadStart" }` for an explicit unread mark, or a turn position for the earliest result needing attention. New terminal results advance the revision without moving an existing unread position. Reads do not create a stored receipt; the initial version is `initial:<conversation-id>`.

The WebSocket command `thread/readState/update` accepts `threadId`, `operation: { "type": "read" | "unread" }`, and `expectedRevision`. It supports durable local user conversations. The desktop command `conversation.read_state.set` accepts `conversation_id`, `has_unread_turn`, and `expected_revision`, and also retains MiniCode's automation notification policy.

Both commands return `readState` in their command result. A stale revision returns an error with `reason: "readStateConflict"` and the current `readState`; the client must wait for a new user action, not retry the edit with a substituted revision. Successful edits emit `thread/readState/changed` with `threadId` and `readState`. MiniCode also includes its numeric conversation `revision` to order these events with existing directory projections.

The durable manifest stores the receipt separately from history checkpoints. Marks do not load or rewrite the transcript, change the conversation activity time, or move its directory position. History hydration and journal replay do not publish attention. Reverting a turn retires its unread position only when the captured receipt is still current. Receipt deduplication survives acknowledgement, restart, index rebuilding, and checkpoint recovery.

These commands use MiniCode's WebSocket command envelope; they do not constitute a full Codex JSON-RPC app-server implementation.
