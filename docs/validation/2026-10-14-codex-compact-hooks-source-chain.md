# Codex compact hooks: owner, lifecycle, input and output

Source checkout `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

`core/src/compact_token_budget.rs` routes both manual and automatic window replacement through the same sequence: PreCompact, ContextCompaction started, actual window replacement, ContextCompaction completed, PostCompact. `core/src/hook_runtime.rs:544,581` stops only on the compact hook's real `should_stop`; `hooks/src/events/compact.rs` derives this from successful universal `continue=false`. A nonzero hook exit is failed hook evidence without a compaction veto. Plain stdout is neither summary guidance nor a new model instruction.

The previous MiniCode manual transaction invoked `ContextBuilder.compact` directly and never bound the operation's hook/runtime/model owner. Automatic normal compaction also injected PreCompact stdout into summary focus and PostCompact messages into prompt state, and ignored `prevent_continuation`. These producer and consumer paths now use `context_compaction_hooks.py`.

- Manual hooks bind to the selected conversation, the independent operation turn, its captured model snapshot and its leased live adapter. No global selected model or previous assistant message is used as the operation identity.
- PreCompact runs before the process item starts or the snapshot is changed. A real stop leaves history, file evidence and live context unchanged, then releases the conversation guard.
- The coordinator completes the durable CAS and publishes the actual completion item before PostCompact. A PostCompact stop keeps the committed new context and completed process record; it does not claim that compaction never happened or mutate an earlier completed turn into a cancelled turn.
- Warnings, errors and real stop reasons use the existing `system_notice` event channel. Their content remains visible without entering the next model prompt. Normal compaction retains the actual user's focus. Raw hook output and run summaries remain available.
- `hooks/src/schema.rs:343` defines the compact input exactly: `session_id`, `turn_id`, `cwd`, nullable `transcript_path`, `hook_event_name=PreCompact|PostCompact`, model, trigger and optional real subagent fields. MiniCode's compact-only producer now uses these fields and consumes the issuing `ToolExecutionContext.turn_id`. It no longer supplies `event`, `custom_instructions`, `compact_summary` or a generic permission-mode field for these two events.
- `schema.rs:90,172,181` defines only `continue`, `stopReason`, `suppressOutput` and `systemMessage` as compact output. The compact-only reducer accepts those Source fields and types, records malformed/unknown JSON as failed evidence and keeps nonzero exits separate from a stop. Other hook events keep their existing contracts.
- No compact handler means no unrelated conversation tool-registry dependency is created. A real handler receives the real conversation registry. This corrected an actual fresh-WS bootstrap failure caught by the new manual path.

Validation used actual Python hook processes and stdin, QueryEngine/model settlement, Windows PowerShell reads, Source apply_patch, a local HTTP Responses compact endpoint, ConversationRepository/CAS and real WS manual dispatch. The model-stop tests prove that sampling does not resume after a Pre/PostCompact stop. Native and token-budget manual cases verify exact model/turn ownership, completion-before-post ordering, warning visibility, prompt isolation and preserved transcript. The patch proof uses real approval responses and the canonical patch trailing newline; it does not weaken permission policy or tool exposure.

Saved batches are under `output/playwright/codex-control-audit-20261013/`:

| Batch | Actual result | Finding |
| --- | --- | --- |
| compact-hooks-run01 | 125 passed, 6 failed | Source label case mismatch; unnecessary no-hook registry consumption; old real-I/O fixture had no approval channel |
| compact-hooks-run02 | 43 passed, 2 failed | New compact cases and WS passed; source-tool I/O still needed actual authorization |
| compact-hooks-diagnostic03 | 2 failed | Full tool records proved approval was missing; no failed I/O was hidden |
| compact-hooks-run04 | 36 passed, 2 failed | Approvals executed real reads/patch/command; the remaining assertion expected the legacy no-newline edit behavior instead of canonical patch output |
| compact-hooks-run05 | 5 passed, 0 failed/skipped | Final real I/O proof and updated canonical commit/delivery fixtures passed |

Latest results keyed by testcase classname/name give **134 passed, 0 unresolved failures, 0 skipped**. This is a deduplicated scope, not a sum of the batches or a claim about all UI controls. `compact-hooks-latest-results.json` records the producing batch for each case. Ruff passed for the new helper and new test module.

Notes and model-owned activation remain separate implementation work. Source `ext/history-notes` uses the selected backend/auth route and private encrypted tool-output attachments; this compact-hook batch does not invent a local Notes substitute or claim that a ChatGPT/Notes backend was connected. The packaged catalog currently declares model-owned token-budget activation false (or omits it with default false), while still carrying the original reminder/fallback defaults. Nothing in this batch enables it merely from a model name.
