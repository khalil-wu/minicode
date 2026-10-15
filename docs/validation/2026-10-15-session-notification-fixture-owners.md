# Session notification fixture owners, 2026-10-15

The GitHub backend run for `3b184a29` completed 6517 tests with 6487 passes, 3 failures and 27 skips. All three failures reported `RuntimeError: Event loop is closed` while a session run manager delivered a real subagent activity update. These were shared fixture lifetime failures; this report does not convert the skips into passes or claim whole-product equivalence.

## Exact source of the failure

`SessionRunManager` subscribes to both parent-notification enqueue events and subagent activity events. Its real `shutdown_notification_wakes()` method calls `stop_notification_wake_intake()`, unregisters both subscriptions, and drains its owned wake tasks. A full `SessionLifecycle.shutdown()` invokes the same lifecycle while the session's event loop is still running.

The two delivery-fence tests in `backend/tests/test_harness_crash_recovery.py` manually unregistered only the older parent-notification subscriber. They closed their pytest event loops while their subagent-activity callbacks remained registered. Weak references do not close an owned subscription: a session still retained by its existing object graph can receive the next real runtime event before garbage collection.

The original failure reproduces without instrumentation in just two nodes:

1. `test_harness_crash_recovery.py::test_delivery_fences_remain_bounded_and_keep_old_completion_until_cleanup`
2. `test_task_runtime_delta.py::test_parent_final_keeps_global_detached_pending_and_cleanup_work_in_runtime_snapshot`

`output/ci-repair-20261015/runmanager-minimal-before.xml` records 1 pass and 1 failure with the same closed-loop stack as GitHub. The identical ordinary pytest invocation after repairing the producer records 2 passes in `runmanager-minimal-after.xml`. Neither victim test nor the production callback changed.

## Other instances of the same missing owner lifecycle

An isolated diagnostic launcher recorded construction locations and callback errors through weak references only. It did not retain managers, clear subscriptions, suppress exceptions or alter authorization. The 122-case candidate slice reproduced all three GitHub victim failures. The actual raising callback in all three came from a closed-loop lightweight runner session; the record included `loop_closed=true` and `wakes_closed=false`.

The same audit observed 7 attachment sessions, 1 real fork-route session, 34 lightweight runner sessions and 1 turn-input manager still subscribed after their loops closed. The trace is `runmanager-fixture-audit-before.jsonl`; the associated XML retains 119 passes and the 3 real failures.

The producer fixtures now close their actual owners:

- Crash-recovery delivery-fence tests await `shutdown_notification_wakes()` in their existing `finally` blocks.
- Attachment-input and real fork-route tests await the full session lifecycle shutdown before their loops exit.
- Lightweight runner scenarios require an explicit session argument and shut down its notification owner in the same loop. The compaction-only scenario explicitly passes no session. The consecutive-turn adapter test runs both turns in one loop and closes its owner only after both complete.
- `test_conversation_agent_mode_chain.py` passes that explicit owner through all five shared-helper calls. Intentional notification disabling uses the real synchronous intake-stop API, which detaches its subscriptions, rather than setting an internal boolean.
- The three asynchronous turn-input manager scenarios use an explicit local owner context that drains notification wakes and closes the durable lease. The recreated manager is also owned and closed.

No production closed-loop check, global dictionary reset, swallowed callback exception or change to the three victim tests was introduced. Their original runtime, detached-child, cleanup, unload and replacement-owner assertions remain intact.

## Final validation

The unified ordinary pytest run of all six changed fixture files plus the three exact GitHub victim nodes is recorded in `runmanager-owner-after.xml` and its log under `output/ci-repair-20261015/`: **151 passed, 0 failures, 0 errors, 0 skips**, in 209.861 seconds. The original two-node reproduction also passes without instrumentation. Counts from overlapping runs are not added.

The six exact changed paths and their frozen SHA-256 hashes are in `notification-owner-frozen-files.json`. `git diff --check` passes. Production runtime/manager files and the victim files have no diff. The 6517- and 8311-case full suites were not repeated by this worker; the latest complete GitHub result remains a separate gate.
