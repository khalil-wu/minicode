# Source native command activity projection

This batch follows the installed Source command item and its separate approval
request through the actual MiniCode producer, live process projection and cold
record. It does not claim complete desktop parity or repeat the closed native
approval panel feature audit.

## Source evidence

- Installed package: `26.1007.9446.0`; ASAR package version:
  `26.1007.71326`; actual binary: `codex-cli 0.162.0-alpha.17.2`.
- Binary SHA256:
  `e98c516f1c8935789aabe9122ee7e05ec4c3efd1ba6acf179242e031c495b99e`.
- ASAR SHA256:
  `356c13dc715816436fa1cf31e9f77863f62ea7b7cacc465b397313246eb29325`.
- Pinned Rust checkout remains
  `822e58cc3d666166c7446c5b1ea2e52f5d09594c`; it is not the installed
  binary's version identity.
- Actual installed app-server allow, decline and cancel wire:
  `output/playwright/codex-control-audit-20261013/source-native-command-events-run01/evidence.json`.
  All three passed against an isolated temporary Source home/workspace and a
  controlled localhost provider. No normal Codex window/profile was used.

The actual `commandExecution` item starts before requestApproval, using the
same item ID. The ASAR `d1` projector keeps this planned item as an `exec` with
`approvalRequestId:null`; a separate request-derived `exec` has a real approval
request ID. `split-items-into-render-groups::p` moves only the latter into the
approval surface. Missing output/exitCode on a terminal declined item does not
turn it back into a pending request.

Source excerpts and exact installed `terminal-light-16` geometry are retained
under `output/playwright/codex-control-audit-20261013/source/`. The initial
incomplete interpretation, which confused the planned and request-derived
items, remains in
`native-command-approval-process-projection-initial-interpretation-retained.json`.
The corrected manifest is `native-command-approval-process-projection.json`.
The direct decline RPC in the Source audit is not evidence of an additional
visible decline menu option.

## Changes

- A streamed provider announcement keeps its raw record and explicit
  `streaming_announcement` origin. It creates no process row while its
  arguments are incomplete. An interrupted announcement remains an actual,
  readable terminal operation; no command text or preparation label is made up.
- A final frozen native request produces a real same-ID planned item before
  approval. `command_presentation` retains its exact command, working directory
  and first actual presentation time. Repeated starts keep that identity.
- The raw pending/waiting-approval lifecycle remains unchanged. Its display
  follows Source `inProgress`, including the full ChatTurn work group, ordinary
  Source terminal icon and Source activity summaries. The separate bottom
  approval request continues to own the user's decision.
- The first presentation timestamp does not measure OS execution. Only the
  actual runtime `tool.started` sets `execution_started_at`; command duration
  remains absent before execution and uses the actual result afterward.
- A real native user abort commits its same-ID terminal receipt, then preserves
  the original owner cancellation and `CancelledError` propagation. It has no
  invented exit code or execution duration. The command and actual refusal
  remain in the canonical/raw record after completion. A pre-execution native
  terminal does not render that model-facing receipt as OS stdout: its Shell
  uses actual typed stdout/stderr, with no invented empty-output placeholder.
  Real typed evidence and genuine errors from an actual execution remain
  readable; Guardian refusal continues to use its existing review details.
- A trusted host Guardian denial or timeout retains its original rejection.
  A deadline, invalid approval response or digest mismatch also retains its
  actual rejection. None is reinterpreted as a user's native abort. Only an
  actual native `decision:"abort"` cancels the original owner.
- Guardian reviews attach only after a genuine projected target exists. A
  review for an unprojected provider announcement remains independently visible.
- The installed `sC → Uh.Footer → yn` branch directly proves the successful
  checkmark/“成功” footer. It also proves the neutral “退出码 未知” fallback when
  exitCode is absent and the execution was not actually interrupted. Native
  human decline does not become “Stopped” merely because MiniCode's raw tool
  status is cancelled. Actual output, errors and raw statuses remain available.
  The initial omission of an unknown footer was an incorrect assumption and
  has been corrected from this direct Source consumer.

Exact output consumer excerpts:
`source/native-command-expanded-output.formatted.js` and
`source/native-command-output-footer.formatted.js`. Their installed
`user-message-attachments-81c735307ef5.js` asset has SHA256
`083cb1df89094558c8fe7e0c81e3871b1fe8510c1b65dd31579b90249afb7e98`.

`backend/agent/turn_state.py`, `execution_journal.py` and `query_journal.py`
canonical/journal consumer work and actual journal/cold tests are owned by Root
in the same batch. The final real production companion reopens the actual
profile and verifies these fields independently of its live WebSocket view.
SourceNotes also closed the original QueryEngine direct-callback journal
entry: manual consumption alone had not proved a cold read during execution.

## Verification status

- `backend/tests/test_native_command_activity_projection.py`: **8/8**, log
  `output/playwright/codex-control-audit-20261013/native-command-activity-backend-batch01.log`.
  Includes an actual native command, actual abort owner propagation,
  Guardian denied/timed_out, deadline, timeout, invalid channel response and
  digest rejection.
- Frontend native activity chain **12/12**, affected ExecCell suite **9/9**:
  **21/21** in two files, log
  `output/playwright/codex-control-audit-20261013/native-command-activity-frontend-batch04.log`.
  Includes full ChatTurn grouping, live execution clock, same-ID output,
  terminal records with no exit, duplicate staged notifications, resume/cold
  hydration and standalone/attached Guardian ownership. The final two cases
  verify that typed output survives without a start timestamp and genuine
  execution errors remain visible after a real start; the pre-execution
  receipt cases verify raw preservation with an empty Shell.
- Earlier frontend batch01 was 18/18 before the additional full ChatTurn case;
  batch02 was 19/19 before the direct Source footer correction; batch03 was
  19/19 before the model-receipt/OS-output correction. These runs overlap and
  must not be summed.
- Root's first unified TypeScript/Vite/debt/budget batch passed with
  `assets/index-Cz0Kkr_F.js`. Its actual companion01 passed the entire provider
  announcement → planned → approval → real execution/output/result scenario.
  Its second scenario stopped on a QA expectation error: MiniCode's actual
  original-owner done status is `cancelled`, which the renderer projects as a
  stopped turn; the fixture incorrectly asserted the Source RPC literal
  `interrupted`. Raw evidence and the failed log are retained. The original
  draft remains hidden in its unchanged original chat.
- Companion01 screenshot03 captured the actual 300ms disclosure in progress;
  it is not counted as a settled expanded panel. A continuation waits for the
  known actual animation before capturing.
- Root's final footer TypeScript/Vite/debt/budget batch passed with
  `assets/index-BqCYZOvL.js`; index SHA256
  `958917700b4f83c97b82857274fc2ea2a464229eb59de70b5314f18ed993a746`.
  CSS remains byte-identical to Cz0 and the preceding BT3 build. Identity:
  `output/native-command-activity-footer-final-build-identity-20261015.json`.
- Companion02 ran no feature scenario: the QA used the local
  `switchConversation` state-apply method instead of the actual
  `requestConversationSwitch → conversation.switch` path. The fixture was
  corrected from the current source. This failed setup and its evidence remain.
- Companion03: **2/2**, errors=[], exit 0, on BqCYZOvL and the final frozen
  backend. Actual native abort cancels its original owner with raw
  `cancelled/user_interrupted`; there is no `tool.started`, execution timestamp,
  exit code or command duration. A real app restart preserves the actual
  approved command's start `1792053612540`, duration **3253 ms**, exit **0** and
  literal stdout. It preserves the new abort's command and terminal metadata.
  The original **8** transcript messages match the raw companion01 startup
  baseline; original draft `NATIVE_POLICY_VISUAL_DRAFT_KEEP` and policy rules
  remain unchanged. Screenshot03b is a settled disclosure after the real
  300ms animation. These five images were visually checked.
- Companion03 image06 still showed the cancellation model receipt in the
  Shell. Direct Source `aggregatedOutput:null → Uh.output:""` proved that this
  was a projection mismatch. The final UI fix above removes only this
  pre-execution non-OS receipt, preserving all raw records and actual output.
- Final TypeScript/Vite/debt/budget/protocol checks passed with
  **`assets/index-2l4V0mq0.js`**. Index SHA256:
  `b8195e87598a308abe3073b63ac9ac948df585f06ed6b00fb81f688454fad142`;
  entry SHA256:
  `1f11fd9cc96c514cbef80d95d609d1bdd447cf9a634bfc1d73b96c3a4b4decf3`.
  Identity: `output/native-command-activity-output-final-build-identity-20261015.json`.
  CSS remains unchanged from BT3/Cz0/BqCY.
- Companion04: **1/1**, errors=[], exit 0. This only reopens the existing abort
  in the final build and expands its Shell: exact command, empty OS output,
  Source “退出码 未知”, preserved raw summary/error, no extra “无输出” label.
  It makes **zero** provider requests and starts **zero** commands, then
  restores the original draft and rules. Image09 was visually checked. Already
  passed allow execution and cold process scenarios were not rerun.

Companion03 and companion04 each keep **16** production file hashes identical
before/after, including the actual final QueryEngine/QueryJournal/ExecutionJournal
and AgentTurnState:

| File | SHA256 |
| --- | --- |
| `backend/agent/query_engine.py` | `3799d7ea5f0b80a008308c6224548adb6dc06ea000bbf1a06096ebe7dc0d48b6` |
| `backend/agent/query_journal.py` | `a68877993e771a88ebb348849dd3dd4fc469c3fb9929b916c4142cf7adac5df0` |
| `backend/agent/execution_journal.py` | `c851bc47f9c8f1b2e644826c4e7f75a9df3a3c265bc54987e8411517f99368d2` |
| `backend/agent/turn_state.py` | `69d24ed858e90cc505bd873d406f113280fabc90f946640c6dd39ae040356a36` |

Final scoped evidence:

- `output/playwright/codex-control-audit-20261013/production-native-command-activity-companion03/evidence.json`
- `output/playwright/codex-control-audit-20261013/production-native-command-activity-companion04/evidence.json`
- Final affected UI:
  `output/playwright/codex-control-audit-20261013/production-native-command-activity-companion04/09-current-build-native-preexecution-raw-receipt-and-empty-shell.png`

Reproducible companion:
`output/playwright/codex-control-audit-20261013/native-command-activity-desktop-qa.cjs`.
It reuses the original isolated native profile, creates separate QA chats, uses
actual production Electron/WS/native policy/commands, verifies a real cold
restart and restores the original history, draft and policy. It does not
reseed the profile or repeat the old approval-panel menu audit. Its final
receipt-visual-only mode reuses the existing abort without creating a chat,
provider request or command. All owned app/provider processes were closed.
