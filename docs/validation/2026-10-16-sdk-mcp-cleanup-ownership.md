# SDK MCP cleanup ownership — 2026-10-16

The SDK MCP timeout review established two real lifetime defects. Both now have focused repairs in the existing ownership chain. The final combined SDK run passed **16 tests, zero failures, zero errors and zero skips**. Historical failures and the interrupted diagnostic run remain available; they are not counted as passing validation.

## Original empty-owner defect

`backend/sdk.py::_mcp_callable_for_tool` uses canonical `execute_tool_batch`, but a standalone exported MCP tool has no `RunContext` and defaults to `requires_execution_environment=False`. Its `ToolEnvironmentBindings.bound` is empty. The old batch finalizer nevertheless created an attachment cleanup task whenever any tool cleanup was pending. That wrapper owned no attachment, so the reported two pending tasks were one real tool owner plus one unnecessary empty attachment cleanup wrapper. They were not two real capability retirements.

`ToolEnvironmentBindings.close_after_tools` now closes immediately when it has no bound attachments. The strengthened standalone timeout test proves that the remaining owner is the original `run_tool` task, follows its coroutine chain into the actual `CapabilityRegistry.execute`, and checks that registry's `execution_task` is the exact source task recorded inside the tool body. It does not pretend that the outer task and nested source are identical. After the original source is released, both settle and the existing cleanup map/receipt becomes completed with pending zero. The timeout journal retains its historical pending-one/manual-recovery receipt.

This standalone exported MCP callable is not a public SDK conversation query. The conversation fence is verified separately through actual `create_session` and `query` below.

## Late registry handoff defect

The first empty-owner repair did not establish complete lifetime safety. A deterministic actual SDK query then exposed a later handoff: the registry's bounded cancellation drain transfers the call's `cleanup_tasks_by_call` entry from the outer cancelled wrapper to the actual still-running `invoke` child. The old attachment cleanup had frozen only the outer task. After the wrapper ended, it released the original bound attachment while the concrete tool still ran. The capability retirement could then enter `aclose` too early. The public conversation fence remained owned because it already observed the real dynamic lifecycle set; that fence did not preserve the prematurely closed resource.

`output/ci-repair-20261015/sdk-mcp-final-owner-handoff-before.xml` retains the real failing case and its `late_handoff_state` property: outer wrapper done, actual source not done and registered, binding cleanup done, original attachment released, original capability close entered, original capability lease not yet released, and query fence still owned. That failure was reported before the production fix.

The final binding repair captures the original bound call IDs and the exact supplied context's `cleanup_tasks_by_call` dictionary. It awaits those calls, rechecks the same dictionary after each settlement, and follows the real child handoff until the source settles. It does not dynamically sweep the shared lifecycle set, which also owns attachment cleanup and capability retirements and would introduce self-wait or retirement cycles. Original leases and existing error receipts remain with the original calls.

The coordinated capability repair is owned by the MCP review agent in `backend/mcp/environment_runtime.py`. `PreparedEnvironmentCapabilities.retire` captures the original tool context's existing call dictionary at retirement entry. After its frozen consumers finish, it follows unfinished wrapper/child tasks in that same dictionary before its existing MCP pending-request drain and original `aclose`. It never rereads a later turn's replacement builder or registers a second cleanup owner. No `RunContext` field or public signature was added.

## Actual SDK verification

`backend/tests/test_sdk_environment_cleanup_ownership.py` runs four actual public SDK query scenarios: environment binding required or absent, each with early source settlement or late registry handoff. It shortens the real registry cancellation drain and gates its start after the public query has returned; the actual execution threshold stays unchanged. The actual registry drain and actual tool body still run.

The capability retirement spy reads the call dictionary actually captured by the original retirement task's coroutine closure. The test compares it by object identity with the original tool context's dictionary. The tool captures the exact `PreparedEnvironmentCapabilities` instance during execution; checks use that instance and its original snapshot attachment, not the current turn's replacement capabilities. A real competing public `query` confirms `conversation_busy` while cleanup is outstanding.

The late-handoff suite properties establish:

| Observation while actual source is still running | Bound | Unbound |
| --- | --- | --- |
| Original outer wrapper completed with original `CancelledError` | yes | yes |
| Actual `invoke` source still running in original call dictionary | yes | yes |
| Binding cleanup still pending and original bound lease held | yes | no binding exists |
| Original capability `aclose` not entered and original lease held | yes | yes |
| Original conversation query fence still owned | yes | yes |

The unbound case explicitly has no binding cleanup task, so it independently verifies the capability retirement repair rather than relying on a binding waiter to hold the capability owner indirectly.

After releasing the source, the tool body returns its original result and the outer task retains its cancellation outcome. For every scenario, the test then holds the original capability's `aclose` at a separate barrier: the bound attachment has released where applicable, but the original capability lease and public conversation fence are still held. It then calls the original `aclose`, verifies the actual snapshot close task's successful result, pending-zero/completed receipts, an empty original call dictionary, no lifecycle owners or waiters, and release of the original conversation fence. No resources or result outcomes are fabricated by the spies.

## Final validation and production boundary

The final run is `output/ci-repair-20261015/sdk-mcp-final-owner-handoff-after.xml` and its paired log. It includes the complete files: MCP execution boundary (4), actual SDK environment cleanup ownership (4), SDK session ownership (3), SDK admission (2), and conversation query guard (3), for **16 unique tests passed**. `py_compile` also passed for the changed binding module and SDK test module; the capability owner verified its own module separately.

The production file changed by this review is `backend/agent/tool_environments.py`, frozen at SHA256 `1f98255749b445ed2ad2bb2bf4a5b340cd5dcf28ec4cb090fc71f1ec4944bbd3`. The coordinated capability file at this SDK run is `backend/mcp/environment_runtime.py`, SHA256 `6c446f0bddff6a4d4e7ba98bbc35f9a3bab53ba922fb1a1479e9462033403052`. These identify the source boundary for rebuilding the final wheel; earlier wheel evidence was explicitly invalidated before either fix and must not be reused.

The machine-readable manifest is `output/ci-repair-20261015/sdk-mcp-final-owner-delivery.json`. This review made no Git operation and did not modify user profiles, user drafts or runtime data. Tests use isolated temporary SDK runtimes and close their actual sessions.

## Retained diagnostic history

The initial `sdk-mcp-final-owner-review.log` run was interrupted after three passes because a new diagnostic assertion failed before its release barrier. There is no completed JUnit XML for that run. `sdk-mcp-final-owner-review-interrupted.json` records the uniquely identified test process and termination; it is not a production hang or a suite pass.

`sdk-mcp-final-owner-review-diagnostic.xml` retains the incorrect test assumption that the registry source and wrapper were one task. R2 passed 12 tests but failed the new fixture's unsupported assumption that SDK supplied a `TaskManager`. R3, R4 and R5 each retain one fixture failure: unrelated legitimate earlier capability retirements, the original outer `CancelledError`, and an invalid `.closed` snapshot attribute respectively. R6 passed only the then-unparameterized early bound scenario. None of those preliminary results is generalized to full capability lifetime. The later pre-fix handoff failure is a genuine production regression, and the final 16-test run verifies the repaired full chain.
