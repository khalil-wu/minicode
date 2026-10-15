# File watcher fixture ownership, 2026-10-15

Scope: the eight closed-loop watcher failures in the Linux root run for `2b59e6ab`. The separate ASGI transport failures are outside this repair.

## Proven source

`tests/test_regressions_websocket.py::test_user_message_workspace_switch_waits_for_workspace_activation` created a real session, activated a real workspace and started its watcher through `SessionLifecycle.restart_file_watcher`. The scenario returned from `asyncio.run()` without closing that session. Its existing file-level fixture stopped notification intake and closed durable queues, leaving the workspace watcher subscribed to the shared project index after its owning loop closed.

`ProjectIndexRuntime` retains actual mounted roots and their listeners. A subsequent full-access tool mutation correctly invalidates mounted views. The stale watcher then calls `call_soon_threadsafe` on its closed loop. This can turn an otherwise correct command result, including a real exit-7 process result, into a projection error.

The uninstrumented two-node reproduction is:

1. `tests/test_regressions_websocket.py::test_user_message_workspace_switch_waits_for_workspace_activation`
2. `tests/test_runtime_architecture.py::test_native_command_keeps_nonzero_process_exit_in_successful_tool_response`

`watcher-minimal-before.xml` records 1 pass and 1 failure with `RuntimeError: Event loop is closed`. `watcher-minimal-after.xml` records 2 passes with the same ordinary pytest invocation.

An isolated output-only launcher traced watcher creation, start, stop and invalidation using weak references. The whole source file produced exactly one observed closed-loop subscribed watcher, created by the activation test above. Its recorded callback later raised in the exit-7 victim. `watcher-owner-before.xml` retains 45 passing source cases and that one failing victim; `watcher-owner-before.jsonl` contains the exact creation stack and owner state. The diagnostic did not suppress errors, retain watchers or alter index subscriptions.

## Repair

The activation scenario now invokes the real `session.session_lifecycle.shutdown()` in its own `finally`, while its event loop remains open. That lifecycle stops notification intake, drains owned work and calls the watcher's actual `stop()`. Watcher stop unregisters its index listener and releases its mounted workspace before shutting down the observer.

No watcher/index production code, closed-loop fallback, global reset or victim assertion changed. The original activation-order and submitted-message assertions remain intact. The other transport worker's manager, execution-lifetime and conversation files were not edited.

## Validation

Evidence is under `output/ci-repair-20261015/`:

- `watcher-minimal-before.xml`: 1 passed, 1 failed, no skips.
- `watcher-owner-before.xml`: 45 passed, 1 failed, no skips; output-only lifecycle tracing.
- `watcher-minimal-after.xml`: 2 passed, no failures or skips; no instrumentation.
- `watcher-owner-after.xml`: **53 collected, 52 passed, 0 failures, 0 errors, 1 existing skip**, 26.595 seconds; no instrumentation. This covers the complete source file and all eight original victim nodes.

The one skip is the existing Windows restricted-token cwd test, whose Temp parent requires elevated ACL access on this host. It is not counted as passing. The remaining seven Linux victim cases pass locally. The latest complete Linux CI run remains the authoritative platform validation.

The single frozen changed path and SHA-256 hash are in `watcher-owner-frozen-files.json`, using an array of `path`/`sha256` records. Coordination snapshots for the manager, execution-lifetime module and exercised test modules are in `watcher-after-observed-source.json`; none changed during validation. `git diff --check` passes. This worker performed no staging, commits, resets or full-suite reruns.
