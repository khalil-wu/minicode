# External thread interactions

An external helper can keep a loaded thread's command wait budgets paused while
the user answers an approval or another interaction. The existing WebSocket
command transport accepts:

```json
{"type":"thread/increment_elicitation","threadId":"conv_example"}
{"type":"thread/decrement_elicitation","threadId":"conv_example"}
```

Each successful command returns a `command.result` whose `data` contains
`conversation_id`, `count`, and `paused`. `count` is the number of outstanding
external interactions; `paused` is `count > 0`. Decrementing zero returns an
error. The thread must already be loaded. A loaded subagent uses its own counter.

Command output waits and explicit command execution timeouts exclude the paused
time. Processes continue running, and completed output is retained. Code Mode
holds its result until all external interactions and the thread's normal
approval/MCP interactions finish. Stop can still cancel a wait. The configured
overall turn budget remains an independent limit.

Counters belong to the loaded thread, including across a window reconnect.
Unloading the thread clears its external count. Counter requests and their
responses are live operations and are not replayed from durable command or
event logs after restart. These commands use MiniCode's WebSocket envelope;
they do not constitute a complete JSON-RPC app-server endpoint.
