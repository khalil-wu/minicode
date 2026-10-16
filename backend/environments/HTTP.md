# Executor HTTP capability (R1 increment)

`Environment.http` is an `ExecutorHttpClient`. It forwards requests over that
environment's original exec-server RPC connection; it never opens a host HTTP
client or reads a host bearer environment variable.

```python
from backend.environments import HttpHeader, HttpRequestParams

params = HttpRequestParams(
    method="POST", url=server_url,
    headers=(HttpHeader("authorization", "Bearer ", "MY_EXECUTOR_TOKEN"),),
    body=request_bytes, timeout_ms=None, redirect_policy="follow",
)
response = await environment.http.http_request(params)
response, body = await environment.http.http_request_stream(params)
try:
    async for chunk in body:
        consume(chunk)
finally:
    await body.aclose()
```

`HttpResponse` has integer `status`, ordered `tuple[HttpHeader, ...]` headers
(including duplicate names), and decoded byte `body`. Buffered requests send
the caller's `request_id`; streams replace it with a unique client-owned
`http-N`. Routes are installed before sending so a body delta can arrive before
the header response. `HttpBodyStream.recv()` returns bytes or None at EOF;
sequence gaps and terminal peer errors remain real protocol errors.

The request fields follow Source `HttpRequestParams`: method, URL, ordered
headers, bodyBase64, timeoutMs, redirectPolicy, requestId and streamResponse.
None timeout is omitted and means no HTTP deadline; literal body bytes and
header prefixes are preserved. A `HttpHeader.value_env_var` is sent only if the
actual executor metadata advertises `httpHeaderEnvVars`. The name is resolved
by the executor, whose own denylist remains authoritative. R3 still decides
MCP credential provenance, environment selection, ordinary env_http_headers
and headers-helper policy. This generic capability does not implicitly borrow
host credentials or override R3's source restrictions.

Source has `http/request` and `http/request/bodyDelta`; there is **no
`http/cancel` RPC**. Cancelling before headers or during recv, closing the body,
or dropping the consumer removes its original route and releases its queued
bytes/borrower. This does not prove a remote POST was withdrawn. Disconnect
fails existing HTTP streams and may recover the shared executor connection;
it never replays POST or resumes those HTTP bodies on the replacement socket.

Each frame is limited to Source's 1 MiB decoded bytes, each stream queues at
most 256 frames, and the logical client shares a 16 MiB queued-byte budget.
Backpressure fails the affected body and leaves the RPC reader available.
An HTTP stream holds its original environment lease to EOF/close; SDKs must
also hold their own cloned lease while retaining an environment capability.
`manager.close()` is still host final shutdown. Closed/known-EOF streams keep
their terminal outcome, and cleanup never waits on that same stream through
an environment-close cycle.

The stdio launcher now builds its environment with the existing public
`shell_subprocess_env(extra=spec.env,
allow_process_control_overrides=True, vault_scope=None)` and then removes
Source's exact non-inheritable set. The helper's defaults preserve ordinary
ambient and explicit user execution variables, remove MiniCode's precise
runtime/browser/eval/audit context, and retain its existing opt-in Actions
scrub. No suffix-based secret scan or vault injection was added. This trusted
launcher configuration still uses the helper's existing override validation.

Formal validation uses project bundled Python 3.11: 51/51, comprising the
unchanged 35-case R1 baseline, 12 HTTP cases and 4 actual launcher-child cases.
The installed Source stdio exec-server performed real buffered/streamed HTTP
against an owned loopback endpoint, resolving a deliberately different child
environment header value and exiting normally. URL recovery/cancellation
uses controlled WebSocket peers; this does not claim native Source WS or an
external SSH/WSL host was tested. Original baseline evidence remains frozen in
`output/remote-executor-r1-delivery-20261015`.
