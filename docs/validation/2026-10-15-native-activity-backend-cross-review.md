# Native command activity: independent backend cross-review, 2026-10-15

Status: **read-only review**. No production edit, test execution, Source app-server run or GUI QA was performed by this reviewer. Desktop owns the implementation and subsequent necessary batch verification.

## Source evidence actually inspected

`output/playwright/codex-control-audit-20261013/source-native-command-events-run01/evidence.json` contains three real installed Source app-server scenarios against an isolated Source home/workspace and a controlled local provider. Binary: `codex-cli 0.162.0-alpha.17.2`, desktop package `.9446`; pinned Source checkout: `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

| Actual scenario | Same-ID sequence | Result |
| --- | --- | --- |
| allow | command `source-audit-allow` item/started and separate requestApproval at `1792050295099`; item/completed at `1792050295906` | Real process ID, stdout and exit 0; duration 584ms. The item existed 807ms, showing why presentation lifetime and actual execution duration are separate. |
| decline | `source-audit-decline` item/started and requestApproval at `1792050296639`; completed at `1792050296655` | Command declined, no process/output/exit/duration; enclosing turn completed. This direct RPC audit does not prove a visible extra decline menu option. |
| abort | `source-audit-abort` item/started and requestApproval at `1792050297397`; completed at `1792050297461` | Command declined, no process/output/exit/duration; enclosing turn interrupted. |

The extracted Source functions in `source/native-command-approval-process-projection.json` and its formatted pending-turn/activity-summary companions show the planned exec remains in agent items, while the separate request-derived exec with approvalRequestId becomes the approval item. The initial incomplete interpretation remains separately archived.

Corresponding pinned calls inspected:

- `app-server/src/bespoke_event_handling.rs` starts planned command execution items before approval and maps actual command approval Cancel to `ReviewDecision::Abort`; invalid approval RPC content produces a denied/failed result, not a user abort.
- `core/src/tools/approvals.rs:464` distinguishes Denied/TimedOut tool rejection from Abort/TurnAborted.
- Guardian assessment terminal status maps to a real declined/failed command item without a fictitious process measurement. This is separate from the native user's explicit Cancel response.

## Findings sent immediately to Desktop and Root

### 1. Host rejection was incorrectly promoted into native user abort

`tool_batch_execution.py::execute_serial` initially defaulted every non-approve response without a native decision to `abort`. However, the actual `_await_approval_within_turn_deadline` producer emits host-generated `action="reject"` without a native decision for an exhausted deadline, wait timeout, non-dictionary channel response and a mismatched request digest.

Those paths consequently called `record_user_cancelled("user_cancelled")`, set the original owner cancel event and raised CancelledError, despite no user selecting Abort. The existing `guardian_owned_approval` fence correctly excluded automatic Guardian receipts, but did not exclude these other real host rejections.

Desktop acknowledged and changed the root decision selection: a missing non-approve decision remains None; only an explicit native `decision="abort"` enters user-cancellation behavior. Desktop is adding the necessary host-rejection cases to its pending unified test batch. This review does not mark those tests passed.

### 2. New native facts did not yet survive all canonical cold/raw consumers

At the audited snapshot, `streaming_announcement`, `command_presentation` and `execution_started_at` were preserved by the WS reconnect projection, public tool projection and in-memory ToolCallRecord.result_payload, but missing from:

- `agent/turn_state.py::AgentTurnState.record_tool_call`, `pending_tool_call_record`, `record_tool_result`;
- `agent/execution_journal.py::_TOOL_USE_OPTIONAL_FIELDS`, `_TOOL_RESULT_OPTIONAL_FIELDS`, which are consumed by the actual tool-use/result journal payload builders.

Thus the canonical SDK/transcript projection and durable raw journal path drop the new facts. The existing new test's WS snapshot plus state.result_payload assertions do not exercise those consumers. This was sent to Desktop and Root before any production change by the reviewer.

The required implementation belongs in those real consumers, with actual QueryJournalRecorder/append/read/reconstruct and AgentTurnState checks. Root has taken exclusive ownership of that consumer batch; Desktop retains the native producers and WS/public projection. Desktop separately noted that the new planned status is pending and must not be dropped by the journal recorder, and that abort recovery must not synthesize an empty-argument use in place of the frozen request. A frontend guess or an assertion on copied fixture data cannot close these paths.

## Other reviewed links

- Provider tool-block announcements explicitly carry `streaming_announcement=true`; a bound final executable native request creates a frozen same-ID presentation with command/cwd/creation time and `streaming_announcement=false`.
- The planned native event is yielded before the separate approval request. It does not carry an execution-start measurement.
- The local `guardian_owned_approval` variable is set from the actual host reviewer result, not trusted from arbitrary returned fields, preventing an automatic refusal from being treated as a native user choice.
- Actual `tool.started` records execution start separately from the cached presentation time. Command results use the actual service duration; a never-executed native request does not acquire a duration or exit code.
- `store_result` deep-copies the command presentation into the completion event and result payload. `_finalize_tool_result` stores the receipt before native Abort marks cancellation. WS stream projection/public projection keep the new facts and final status fences.
- The new no-execution completion span omits start/end/duration instead of converting approval wait into an OS duration. Actual command duration remains distinct from runtime-span wall time.

No additional actionable defect was established in those inspected links. This is a scoped code/evidence review, not a claim that the pending batch, cold-journal fix or all frontend behavior is already verified.
