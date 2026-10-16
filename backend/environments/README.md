# Executor services (R1)

This package implements the configured executor service boundary from Codex
Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`. It does not select a thread's
environment, convert MiniCode permissions, route existing tools, or add desktop
SSH/WSL hosts. Those receiving-thread and tool boundaries belong to R0/R2.

`EnvironmentManager(config_dir, *, local_backend, environ, client_name,
inbound_request)` reads only `config_dir / "environments.toml"`. A missing file
uses Source's `CODEX_EXEC_SERVER_URL` provider; a present invalid file remains
an error. `default`, `include_local`, `none`, required skills, WebSocket URL /
bearer token and stdio `program/args/env/cwd` follow that provider's rules.
Relative launcher cwd is relative to the configuration directory; executor
filesystem paths are never resolved on this host.

Construct a manager inside the host's running asyncio loop. URL entries start
connecting; stdio entries remain lazy. `manager.start()` is idempotent. If the
snapshot includes `local`, the embedding host must explicitly supply its real
`LocalEnvironmentBackend` (filesystem, process, async info/close). There is no
remote-failure fallback to that backend.

## Thread and SDK ownership

`get_environment(id)` returns the registered object or None for inspection.
Receiving threads must retain `manager.borrow(id)`. Its `.environment` is the
actual selected object. `lease.clone()` or `environment.borrow()` retains that
same object for an SDK or another original borrower; neither looks up the
possibly replaced registry entry.

```python
thread_lease = manager.borrow(selected_environment_id)
environment = thread_lease.environment
sdk_lease = thread_lease.clone()

# R0 supplies the captured Source wire sandbox explicitly.
process = await environment.process.start(
    argv, cwd_uri, sandbox=wire_sandbox, pipe_stdin=True,
)
status = await process.write(b"input\n", write_id=original_write_id)
state = await process.read(after_seq=last_delivered_seq, wait_ms=1000)

await process.aclose()
await sdk_lease.aclose()
await thread_lease.aclose()
```

`upsert(spec_mapping)` replaces only the registry object, preserving the
manager's configured default. Existing thread/SDK leases, process handles,
file handles, pending RPCs and retained cancellation cleanup keep the original
connection. A retired environment closes when its last borrower releases it;
an unused replaced connection closes without waiting for host shutdown.

`manager.close()` is for the host's final shutdown, after its thread lifecycle
has stopped admission. It is not a conversation switch or lease release. It
closes the original resources and transports, retains cancellation cleanup,
and exposes actual failures instead of asserting that an unconfirmed request
succeeded. Repeated close awaits the same owned close task.

## Environment and operation API

- `environment.info()` can start/connect and force-fetch real peer metadata.
  `environment.status()` only observes or probes an existing initialized
  connection. Manager status additionally returns unknown for an unregistered
  ID. Registry state and the peer's ready-only status are separate.
- `filesystem.read_file/write_file/create_directory/get_metadata/canonicalize/
  read_directory/walk/remove/copy/open` take executor-native absolute `file:`
  URIs and a **required keyword-only `sandbox`**. Explicit None is the caller's
  already-authorized bypass intent; omission or error never becomes None.
  File bytes use `dataBase64`; stream ByteChunks use `chunk` base64.
- `filesystem.open(..., mode="read"|"replace")` returns an original-session
  file handle. `read_block`, `write_block`, `chunks` and `aclose` retain the
  sandbox authority captured at open; Source's handle RPCs have no new sandbox
  field. Replacement streams require the actual `fileWriteStreaming` capability.
- `process.start(argv, cwd_uri, *, sandbox, ...)` returns `ExecutorProcess`
  with the actual process ID and reported sandbox type. `read` returns
  `ProcessRead` and typed `ProcessOutputChunk.chunk` bytes. `write/signal/
  terminate` target that same original process/session. Their Source wire
  methods have no extra sandbox field; start captured that authority.
- `subscribe_events().recv()` supplies independent bounded replay/live
  receivers. `next_event()` provides a lazy default receiver. Event dictionaries
  use actual `process/output`, `process/exited`, `process/closed`; output has
  decoded byte `chunk`. Exited does not imply closed. `ExecutorEventsLagged`
  requires recovery through `process.read(after_seq=last_delivered_seq)`;
  consumers keep their own delivered sequence cursor.

The client uses one reader and correlated pending requests. WebSocket recovery
resumes the exact initialized session and recovers process event gaps through
read; it never retries a sent start/write/edit operation. Stdio has no session
reconnect strategy. Cancellation after start/open keeps the pending resource
receipt and cleans its original logical handle if accepted later. Host shutdown
retires ordinary waiters while preserving cleanup RPCs and closes the actual
owned transport. Transport receipts and `client.resource_receipts` retain real
exit/forced/closed/failure facts; they do not establish an agent turn outcome.

Source limits are 64 MiB transport messages, 1024 ordinary RPC slots plus a
cleanup slot, 8 KiB inbound requests, 2 MiB streamed notifications, 256 process
reorder/history events and 1 MiB retained/reordered process bytes. Source
timeouts are 10 seconds connect/initialize/status, 30 seconds force info,
25 seconds session recovery with 100 ms retry spacing, and a 2 second stdio
termination grace. Configured connect/initialize values override their defaults.
Only Source's transient error classes trigger recovery; malformed protocol,
authentication/TLS, a missing launcher and permission failures are not treated
as interchangeable network failures.

## Validation boundary

The formal local batch uses the project's bundled Python 3.11 via
`output/ci-repair-20261015/pytest-python311.py`. The Source native interop test
requires an explicit `MINICODE_SOURCE_EXECUTOR` executable and launches only an
owned hidden stdio child with an isolated child `CODEX_HOME`. If unavailable,
that specific test is skipped and must not be counted as passed. URL transport,
replacement, recovery, cancellation and teardown use controlled WebSocket peers.

No R0/R2 bootstrap/permissions/tool dispatch, R3 MCP/Skill integration or R4
desktop SSH/WSL UI is claimed by these tests. The local harness execution host
and a selected remote executor attachment remain different objects; H4's host
classification must not be derived from an attachment's URL.
