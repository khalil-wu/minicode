# Frozen Source approval ownership, 2026-10-15

## Reproduced defect

A real MCP request was frozen and emitted by `ToolBatchRunner`, converted to a session `control_request`, and left awaiting its original response. Updating session defaults to Auto or Full access and adding a local allow could make the generic pending-prompt path approve that already-issued request. The pending request's original thread, turn, item, digest and issuing profile did not authorize that new response.

The committed resolver from `7c4672c9caa5052412dff65fb42c5212cac64955` reproduces both failures without replacing any shared source file:

- `source-frozen-approval-before.xml`: 4 expected failures. The old MCP resolver accepts a generic `auto_approved=True` response for the actual frozen pending request.
- `source-frozen-approval-mode-before.xml`: 4 expected failures. With Source's actual Auto MCP policy and an unannotated tool, the real mode-switch path returns `['frozen-call']` as automatically approved. These cases fail at the mode-switch assertion before any explicit user response.

The second probe changes only the test process's MCP fixture from configured Prompt to Source Auto. Both policies produce real initial approvals; no captured native decision or approval response is fabricated. The isolated launchers and full logs are under `output/ci-repair-20261015/`.

## Source authority

- `.tmp/codex-src/codex-rs/core/src/state/turn.rs`: `TurnState` stores pending approval senders by their original approval key. `insert_pending_approval` and `remove_pending_approval` own that one-shot lifecycle.
- `core/src/session/mod.rs`: native execution approval creates the response channel and registers it on the active turn before emitting the request.
- `core/src/session/step_activation.rs`: applying new step settings does not bulk-resolve old pending approval channels. A new default is not a response to an already-issued action.
- `core/src/mcp_tool_call.rs`: Source Auto checks destructive=true first, then readOnly=true, then the explicit destructive=false/openWorld=false combination. A read-only tool with openWorld=true does not acquire an additional invented URL approval. Never preapproval uses the actual issuing permission profile.
- `core/src/exec_policy.rs`: native command policy evaluates the actual parsed argv and trusted rules. A local remembered allow or retired sandbox boolean does not replace that producer.

## Production repair

`backend/ws/approval_runtime.py` retains frozen native command requests, identified by their actual `available_decisions`, and MCP requests, identified by their actual `mcp_approval`, when session defaults change. The generic pending-prompt evaluator does not reauthorize either request. The MCP resolver also rejects generic automatic or session-cache responses; explicit Source responses and the originating MCP owner's own remember behavior remain available.

The local prompt path remains exercised separately. Existing thread/turn/item owner validation and exact request digest checks remain authoritative. No missing-capture fallback or command-string classifier was introduced.

## Test producer repairs

- Direct command permission cases use the actual model/config/registry/exec-policy owners and final authorization producer. Dangerous examples are authorized only and are never executed. Native prompt/forbidden rules are generated from the actual Source `parsed_commands`, so platform parsing remains visible rather than being replaced by guessed argv.
- Permission defaults come from `permission_settings_from_config`, preserving builtin routing versus explicit user policy. Explicit native prompts, hard denials, MCP asks, protected files and restricted Never profiles remain separately asserted.
- Command template replay uses the accepted input's actual submitting queue and assistant identity. Workspace fixtures bind the thread's real `execution_cwd`; a project label alone does not change its execution directory.
- Skill read-root refresh uses the real `SkillLoader`. The Stop fixture invokes real cancellation, notification-wake shutdown and owned cleanup rather than clearing task dictionaries. Queue steering waits for the execution-command task owner.
- The asynchronous hook deadline regression verifies that the real recorded child PID has exited and cannot write its natural-completion marker, while retaining the precise 0.05-second timeout result. It no longer treats an unrelated 1.5-second host scheduling bound as proof of process termination.
- Prompt admission, concrete cancellation-resistant output workers, plan templates, fork rollback, exact command diff preservation and streaming-block materialization retain their actual production owners and public representations.

## Verification

All paths below are relative to `output/ci-repair-20261015/`. Counts are not added across overlapping runs.

| Evidence | Result |
| --- | --- |
| `backend-root-owned-source-final.xml` | **244 passed, 0 failed, 0 errors, 0 skipped**, 72.870 seconds. Covers all 21 failures from the preceding 277-case diagnostic batch, the 8 real command/MCP ownership combinations, the four assigned root files and the complete hook file. |
| `source-frozen-approval-mode-after.xml` | **4 passed, 0 failed/errors/skipped**, actual Source Auto variant of the MCP ownership flow. |
| `source-frozen-approval-before.xml` | **4 expected failures** against the committed resolver; generic automatic responses are accepted incorrectly. |
| `source-frozen-approval-mode-before.xml` | **4 expected failures** against the committed resolver; mode changes incorrectly approve the old Source Auto request. |
| `backend-owned-native-queue.xml` | The complete diagnostic batch had 106 passes and 4 failures. The five prompt/plan/worker/fork files account for **57 passing cases**; its remaining 4 failures were repaired and covered by later batches. |
| `backend-owned-source-permissions.xml` | Earlier diagnostic batch: 256 passed, 21 failed, no errors/skips. The 21 failures are all covered by the final 244-case passing batch; its other passing scopes include the remaining five assigned runtime permission cases, Source strict grants, Skill refresh, Stop, task-runtime notifications and the complete existing MCP producer file. |

The ownership flow uses `ToolBatchRunner` to emit the approval, the real session payload builder and waiter, a session default change, owner/digest rejection checks, and an explicit response through the original request. Approved commands actually execute their harmless original output. Approved MCP calls reach the real test transport. Native abort and MCP rejection produce their respective real terminal records. The accepted result retains the original digest and the pending waiters drain.

Production and test files were frozen before the extra isolated before/after probe. Their exact 21-file list and SHA-256 hashes are in `harness-frozen-files.json`. `git diff --check` passes for the owned files. No commit, staging, reset or live application restart was performed by this worker.

The earlier 8311-case Windows snapshot was interrupted after it reached a confirmed stale, unbound cancellation fixture; it is not a passing full run. `backend-root-python311-full-interrupted.json` records 7551 completed outcomes: 7292 passed, 203 failed, 53 skipped and 3 errors. These old loaded tests predate later repairs. Current GitHub full-suite results remain a separate requirement, and this scoped verification does not claim complete desktop equivalence.
