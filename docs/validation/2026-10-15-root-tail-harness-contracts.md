# Root tail harness contracts, 2026-10-15

Scope: `tests/test_sandbox.py`, `tests/test_sandbox_alignment.py` and `tests/test_subagent_lifecycle.py`. This batch changes tests only. The previous Source approval files remain frozen at their committed revision.

## Source producer corrections

The two named-teammate mailbox failures were caused by expecting `send_message` to start an idle agent. Source's `tools/handlers/multi_agents_v2/send_message.rs` uses `MessageDeliveryMode::QueueOnly`; `followup_task.rs` uses `TriggerTurn`. `agent/control/delivery.rs` retains that distinction in the actual communication, and the MiniCode idle owner claims only trigger-turn messages.

The corrected test waits for the real idle projection, sends a normal message, verifies its durable QueueOnly mode and mailbox epoch, verifies that no trigger-turn claim exists, and then sends a real `FollowupTaskTool` request. Both fresh and legacy cases still assert two completed query-run IDs, one stable named identity, epoch 1, matching agent paths, the revised checkpoint, exactly one start/done event, and the unchanged old legacy run. The model fixture records the complete model input rather than only its last message, so queued context is represented accurately.

The sandbox cases use `native_command_harness` and real final authorization rather than a partial fake context or an unbound class permission hook:

- A direct unbound command cannot launch either foreground or background execution. A separately authorized outside cwd is captured with the original explicit filesystem profile: workspace writes remain allowed and the outside directory gains no write authority. Source's `tools/handlers/unified_exec/exec_command.rs` resolves an explicit workdir against the issuing environment; the captured sandbox enforces permissions.
- Retired `autoAllowCommandsIfSandboxed` and `excludedCommands` settings do not substitute for the actual native decision. Non-dangerous commands stay within the real restricted profile; requested escalation produces a real approval requirement. Source `core/src/exec_policy.rs::render_decision_for_unmatched_command_for_platform` owns that distinction.
- The managed no-unsandboxed case executes the real `ToolBatchRunner` path: native authorization, a real approval request, an explicit approval response and the bound request. Its frozen managed policy then rejects the override before either launch implementation is entered. The failure record retains the actual `execution_error` evidence.

## Verification

Evidence is under `output/ci-repair-20261015/`:

- `root-tail-failures-before.xml` retains the original five failures in this scope, within the 24-case failing tail diagnostic. No 670- or 8311-case baseline was repeated.
- `root-tail-harness-owned-final.xml`: 48 collected, 44 passed, 1 failed, 3 skipped, 0 errors. The sole failure was a new test expecting the literal status `error`; the actual canonical failure status is `failed`.
- `root-tail-managed-launch-final.xml`: that corrected complete managed launch-prevention path passes, 1/1, with no errors or skips. The other already-passing cases were not repeated.

The three existing Windows sandbox skips remain unverified. Their reported reason is insufficient effective `:root` read access for the elevated backend, no built sandbox container image, and no equivalent isolated fallback on this host. They cover network denial, outside-root write enforcement, and system-path reads. No skip or relaxed execution guard was added.

Exact frozen file paths and SHA-256 hashes are recorded in `root-tail-harness-frozen-files.json`. `git diff --check` passes. No production changes, staging, commits, resets or live application restarts were made by this worker.
