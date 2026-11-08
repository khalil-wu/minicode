# Code execution host

`tool_exec` runs JavaScript in a separate Python process containing V8. By default,
ordinary conversation requests do not start that process or load V8 into the backend.
Code sessions on the backend's event loop share a lazy host; each cell has its
own isolate. Desktop conversations, agent threads and SDK sessions retain their
host session across turns. Closing the final owner closes the pipes and waits
for the host process to exit.

Set `features.code_mode_prewarm` to `true` to open the selected idle desktop
conversation's host session during startup preparation. The setting defaults to
`false`. Prewarming opens the process and session without creating a cell,
running JavaScript or calling tools; the next `tool_exec` uses that same session.
An optional prewarm failure is recorded in backend logs, and actual execution
can start a new host. Conversation unload and application shutdown drain startup
and close the owned session through the normal code-store lifecycle.

Code-cell `ALL_TOOLS` entries and nested `tool_search` results include a tool's
namespace guidance before its own description. Set
`features.code_mode_tool_description_first` to `true` to put the tool description
first. The setting defaults to `false` and is captured for the issuing model
step. Ordinary provider function descriptions keep their existing format.

The host evaluates JavaScript and returns tool requests. MiniCode's existing
tool runner still owns permissions, approvals, execution ordering, workspace
access, tool hooks, media provenance and durable tool receipts. The JavaScript
environment has no `process`, `require`, `fetch` or filesystem bindings.
The host uses Python isolated mode and the existing subprocess environment
policy. Process isolation does not replace the sandbox applied to individual
tools.

`tool_wait` retains its existing yield and cancellation behavior. Cancelled
cells drain their nested tools and close the actual V8 isolate. A host failure
fails its live cells, including cells waiting on a tool, and preserves completed
tool outcomes. New cells can start a new host. A cell from the failed host is
never replayed on the new process, so callers must inspect completed writes
before deciding whether to retry. Session JSON values stay with the controller;
successful `store` updates already received by it remain available.

The host transport is a private, versioned stdio protocol with little-endian
length-prefixed JSON frames, a 64 MiB frame limit, request cancellation and
explicit session shutdown. A host permits 128 active isolates and 256 concurrent
requests; the existing four active cells per code store still applies. Each
isolate retains its 128 MiB memory limit and ten-second JavaScript execution
budget. Tool waiting time is outside that execution budget.
The controller negotiates startup within thirty seconds. A host operation that
does not answer within sixty seconds terminates that host and fails its live
cells; an unresponsive process cannot retain the session's cleanup indefinitely.

Startup and process exit are recorded in backend logs as `Code host started`
and `Code host stopped`. This local transport does not expose a network endpoint
or implement Codex's remote gRPC host protocol.
