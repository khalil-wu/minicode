# Thread samples

`thread/sample` runs one generation on a private fork of a loaded persistent
root conversation. It inherits the selected model, permissions, tool schemas,
Skills, MCP configuration and model history, and uses the normal QueryEngine
preparation. Automatic compaction is disabled for this operation.

MiniCode exposes these commands through its WebSocket protocol. This is not a
JSON-RPC app-server endpoint. The request identity is the typed `requestId`
field; it is independent of the transport's optional `client_command_id`.

```json
{
  "type": "thread/sample",
  "requestId": 7,
  "threadId": "conversation-id",
  "expectedTurnId": "latest-finished-turn-id",
  "prompt": "Suggest a short title for this conversation."
}
```

The source must be idle and unarchived, and `expectedTurnId` must identify its
latest finished turn. The prompt must be nonempty and at most 4,000 UTF-8 bytes.
The operation times out after 30 seconds; the operation owner retains startup
and cleanup even if the connection has already timed out or closed.

Optional fields follow the Source sample contract:

- `effort` applies only to the OpenAI provider and models that declare support
  for cache-preserving effort updates. Other providers keep inherited effort.
- `threadSource` defaults to `feature:thread_sample`.
- `turnTrigger` and `responsesapiClientMetadata` supply request provenance.
- `disableMcp` disables MCP only when the inherited window has incremental
  `additional_tools` items and its model uses Responses Lite.

The result is a `command.result` for `thread/sample`. Its `data` contains
`requestId`, `conversation_id`, `sourceTurnId`, `responseId`, `text`,
`inputTokens`, `cachedInputTokens`, and `outputTokens`. Missing usage is `null`.
`responseId` is the actual provider identifier (an empty string if the provider
does not supply one). Commentary and reasoning do not contribute to `text`.
Tool attempts, nontext output, and empty responses produce `text: null`.
Returned text is bounded to 64 KiB of UTF-8.

Tools remain advertised, but no sampled model tool call executes. The first
response is captured before dispatch, retries or Stop hooks. Required client
interaction during startup fails the sample. A successful response is returned
only after the fork and its private runtime files have been closed and removed,
and the source history has been checked again. The sample creates no public
conversation or assistant message and does not append to the source transcript.

```json
{"type": "thread/sample/cancel", "requestId": 7}
```

Cancellation applies only to a matching sample on that connection. Integer
`7` and string `"7"` are different IDs. Unknown and repeated cancellation is
harmless and returns empty result data. Disconnect cancels the old connection's
samples, and a reconnect cannot receive their results. Requests and results
are live only and are never replayed from the durable command or event logs.
