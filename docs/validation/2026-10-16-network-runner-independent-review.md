# Independent network command lifecycle review — 2026-10-16

The initial read-only production review covered `command_tool` capture/factory, `FrozenExecCommand` identity, `ExecutorCommandRunner`, native network lease/observer ownership, background commands and Monitor. Root owns the runner/process production repairs. The later explicitly delegated RPC boundary repair and its tests are recorded below.

Source references: checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`, `core/src/tools/network_approval.rs` (active/deferred proxy lifetime, original profile/environment snapshot and current reviewer), `tools/orchestrator.rs::run_attempt`, `unified_exec/process_manager.rs` (finish after process exit and late denial), and `exec-server/src/local_process.rs` (policy shutdown on terminate/close).

## Findings delivered to root

| Priority | Concrete path | Minimal reproduction | Required behavior |
| --- | --- | --- | --- |
| P1 | `ExecutorCommandRunner.run` propagates a pump/read error after `cleanup()` returns false without marking that error as pending cleanup. `ExecutorProcess._close` also marked the unconfirmed handle released and removed its original registration. Foreground and background callers key durable recovery on the missing attribute. | An accepted actual `ExecutorProcess` receives `process/failed`; its close is unconfirmed. `test_failed_reader_with_unconfirmed_close_preserves_recovery_receipt`. | Preserve the original error and pending cleanup receipt, original handle/client registration and original network/environment ownership until the same handle confirms exit. |
| P1/P2 | `ExecutorCommandRunner.cleanup` gathers observer tasks before finishing the network registration or closing either lease. A failed deniedMessage RPC aborts the rest of cleanup. Its failed task remains in the set, so retrying repeats the same error. | Process closure succeeds; a native observer RPC fails. `test_observer_error_does_not_prevent_original_registration_and_lease_release`. | Retain the observer error while finishing/releasing the original independent resources once process closure is proven. Do not borrow a new registration or suppress the error. |
| P2 | Cleanup copies the network audit list before `network_lease.aclose`. Native `Execution.close` shuts down the proxy and flushes its audit tasks before the release reply, so final records can arrive after the copy. | The original lease emits a final deny during its close. `test_result_retains_audit_emitted_by_the_original_proxy_release`. | Collect the final audit after the original route's release and account for observers created by that release. |

The first two tests failed in `output/network-runner-independent-review-20261016.xml`; the third failed in `output/network-runner-late-audit-review-20261016.xml`. These are deliberate regression failures proving the pre-repair behavior, not passing validation. The tests use the production `ExecutorProcess` and runner with explicit failed peer/RPC boundaries; they do not claim physical native enforcement. Root was notified immediately and owns the repairs and reruns.

The fixture was then made explicit for root's repaired close protocol: an unavailable original peer raises `ExecutorDisconnected` from its cleanup RPC; an available peer returns concrete `process/terminate` and `process/read` responses. The client owner's `closed` flag is not used to infer peer availability. A fourth testcase checks that a real protocol `closed=True` receipt releases the original resources while retaining the original reader exception and sequence state. The unknown-close testcase also retries cleanup on that exact retained handle once its original peer is available.

Two related consumers also need the complete repair: a cleanup exception currently replaces the original read error's outward message, and `BackgroundCommandManager._execute` copies network audit only after a normal result. An exception with pending cleanup can therefore persist an empty audit even when the original runner has real records. Both were delivered to root with the concrete call chain.

## Reviewed ownership behavior

- `FrozenExecCommand` includes the effective network policy identity in its execution digest. The command factory binds its actual final request and captured policy; policy persistence closes over the original owner.
- A runner clones the original attachment lease. The registration clones the issuing environment snapshot. `PreparedNetworkStep.policy_for` matches the actual environment instance and selection.
- Current review retains the original environment scope, permission profile, trigger and snapshot while using the actual current review turn. The read-through review source is bound to the originating agent session; no new registration lookup was found in this path.
- The ordinary network runtime error result reaches foreground and background lifecycle error fields, Monitor text and runtime metadata. No additional UI hiding bug was established in those normal completion paths.
- Source exec-server terminate cancels its policy-shutdown token before terminating the process. The real native candidate still needs to verify pending review plus timeout/cancel/process exit, late denial audit and cleanup receipts end to end; this audit does not infer a native pass from the deterministic tests.

## Coordinated repair validation and RPC boundary follow-up

Root repaired the runner/process ownership paths. The four coordinated runner regressions then passed in `output/network-runner-independent-review-r2-20261016.xml`. Its separate RPC testcase was deliberately deselected in that run and is not counted as passed or skipped.

A further concrete defect was found at the native inbound RPC boundary: an ordinary exception from the actual network approval consumer propagated through `NativeNetworkRuntime._inbound`, but `RpcConnection._handle_request` caught only `ExecutorRpcError`. Its task ended without answering the original native request ID. Root explicitly delegated `backend/environments/rpc.py` and narrow tests to this agent.

The boundary now follows Source `exec-server/src/rpc.rs::request_with_trace` and `internal_error`: an ordinary handler failure produces a JSON-RPC error for the same ID with `-32603`, the original error message and `data: null`. The log retains the original Python cause/traceback plus request ID and method. Explicit `ExecutorRpcError` codes/data and normal results are unchanged. A response-send task's failure is retrieved and logged with its original method/ID owner instead of becoming an unobserved task exception.

Cancellation follows the distinction in Source `exec-server/src/client_recovery.rs` network request handling: a closed connection ends its original waiter without sending a fabricated response; a cancelled request on a live connection gets an explicit same-ID error while leaving that connection usable. Neither path turns a network denial into user turn cancellation. No retry, alternate owner or fallback decision was introduced.

`backend/tests/test_environments_rpc.py` covers these boundaries with actual local TCP WebSockets, plus a deterministic failed send sink. The real approval consumer → native inbound → original RPC error response is covered by the fifth testcase in `test_network_runner_review.py`.

The first combined run (`output/network-rpc-boundary-20261016.xml`) passed all **12 new/coordinated cases**, plus four of six existing lifecycle cases, for **16 passed / 2 failed** total. The two failures were delivered to root: a missing `ExecutorRpcError` import in its new process-close branch, and using the client owner's `closed` flag to infer that the original cleanup RPC transport was unavailable. Root repaired those production sections; this agent did not patch them.

The R2 narrow run (`output/network-rpc-boundary-r2-20261016.xml`) passed six cases and exposed one remaining stale fixture response: the host-close peer did not implement `process/read`, so it returned an unrelated provider-info object. With root's explicit authorization, that fixture now returns the complete Source closed receipt and verifies the read belongs to the original process ID. No production parsing fallback was added. The last testcase passed in `output/network-rpc-boundary-r3-20261016.xml`.

The final latest-result count is **18 unique tests passed, zero failed and zero skipped**. Reruns are not added to the testcase count. The delivery manifest is `output/network-rpc-boundary-delivery-20261016/manifest.json`. `py_compile` passed for the changed RPC module and both new test modules. RPC production and the related fixtures were frozen and root was notified before its final backend scope run.

Only the explicitly delegated RPC boundary production file was changed by this follow-up. No Git state, runtime data, user draft or editor contents were changed. Final native candidate execution remains with the owning root/runtime agents.
