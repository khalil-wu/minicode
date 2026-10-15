# Heartbeat provenance review and Phase 1 request scope, 2026-10-15

Later status: Root authorized the bounded Phase 1 producer batch described here. Its implementation and 42 focused passing cases are recorded in `2026-10-15-memory-phase1-request-provenance.md`. This audit text remains the pre-implementation evidence and scope; its old gap descriptions are historical.

Status: **read-only review and proposed follow-up scope**. This document makes no Phase 1 implementation or test-pass claim. Root's heartbeat production files/tests were not changed or rerun by this audit. The separately authorized Guardian routing correction is recorded in `2026-10-15-guardian-response-routing-correction.md`.

Reference Source: `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

## Independent heartbeat review

No actionable logic defect was found in the final due/manual/retry/cold-pending/human-steer provenance boundaries inspected here.

| Path | Actual authority and result |
| --- | --- |
| Scheduled due claim | `tasks/scheduler.py::_schedule_fire` creates `turn_trigger=automation_heartbeat_scheduled` only when `consume_schedule` is true and the claimed task is actually a heartbeat. The gated worker cannot start before that run record and schedule cursor are saved. |
| Manual run | `run_now` creates a new claim with `consume_schedule=False`, hence User origin. Returning an already-active run returns that same actual execution; it does not admit another manual input and does not relabel it. |
| Manual retry | `retry_run` creates a fresh non-consuming claim and only reuses the previous conversation ID. It does not copy the previous run's scheduled trigger. Existing prior input in the conversation snapshot keeps its prior origin; the newly admitted retry is User. |
| Cold pending replay | ScheduledTaskRun's real trigger is preserved by asdict/from_dict. `_reconcile_orphaned_runs` and `_start_worker` reuse the pending run; they do not recompute its origin from the current task kind. Legacy missing/unknown trigger is not guessed. |
| REST admission | `scheduled_task_runner.py` passes `run.turn_trigger` to the owned REST input. `chat_api_service.py` derives explicit `input_origin` from that actual trigger and independently authors a Source turn-trigger snapshot. A heartbeat-shaped prompt or `automation_kind` alone does not create Heartbeat provenance. |
| Consumed human steer | `TurnInput.from_command` does not accept `input_origin` or `turn_trigger` claims in public command data. The consumed input's own `TurnInput.input_origin` takes precedence over the active turn's original metadata. A human addition inside a scheduled heartbeat remains User. |
| Save/load | Actual LLMMessage and Guardian retained-message origins are serialized independently. Loading prior history does not relabel old messages from a later turn trigger. The final code also removed the generic `query_source="heartbeat"` shortcut. |

Other queue callers include normal WS steer and extension send-message. Source `UserInputOrigin::User` means ordinary/unclassified input, not proof that a human typed it; no new authorization inference should be built from the enum name alone. This audit found no reason to replace the per-input producer with whole-turn heuristics.

The Source comparison was the actual `history/src/heartbeat.rs::from_turn_trigger` and `core/src/session/turn_input.rs` start/steer admission. Source's `turn_input_tests.rs:1018` explicitly tests a human steer inside an active scheduled turn and a separately scheduled input inside an ordinary active turn.

Root's current report/manifest records 176 distinct latest case IDs passing, including the final ten-case source file and necessary host fixtures. That is a reviewed existing result, not this subagent executing another suite or claiming an exact-current-tree all-suite pass. The earlier bundled-Python async-plugin failures and four incorrect public-transcript expectations remain in the original artifacts.

Full heartbeat coalescing, automatic steering while a conversation is busy, and other scheduler admission gates are separate behavior already identified by Root's report. Correct provenance is not evidence those behaviors are implemented.

## Phase 1: exact Source owner

Source's Phase 1 extraction model request belongs to the thread that **started maintenance**, not to the old thread whose rollout is being analyzed.

Example: current thread A starts maintenance; the worker selects old rollout B. The prompt describes B's rollout/cwd, but request identity remains owned by startup thread A.

`memories/write/src/runtime.rs::MemoryStartupContext` retains the actual startup `self.thread_id` and `self.thread`. `stream_stage_one_prompt` (`runtime.rs:310`) then reads that thread's actual config snapshot and SessionSource and uses:

```text
session_id = SessionId::from(startup_thread_A)
thread_id = startup_thread_A
window_id = format!("{}:0", startup_thread_A)
turn_id = a newly minted UUID v7 for this detached request
root_turn_id = this same detached turn_id
request_kind = memory
thread_source = memory_consolidation
turn_trigger = memory_consolidation
```

`core/src/turn_metadata.rs::detached_memory_responses_metadata` also records the actual permission profile and available workspace metadata. The optional subagent compatibility label is derived from the **original startup SessionSource**. Phase 1 does not change that SessionSource into Internal(MemoryConsolidation).

Consequently ordinary startup A has no memory subagent or memgen header merely because it is extracting memory. The Phase 2 internal worker's header rule must not be reused by operation-name inference. If a genuinely supported startup source has an actual subagent label, preserve that source's label, rather than inventing a memory label.

The Source detached-memory blob intentionally differs from the normal agent-turn blob. `responses_metadata.rs::CodexResponsesRequestKind::has_thread_identity` is false for Memory. `memories/write/src/startup_tests.rs:683` asserts:

- body `client_metadata["x-codex-turn-metadata"]` equals the real compatibility header;
- request kind Memory, memory thread source/trigger, actual sandbox mode;
- UUID turn ID equals root turn ID;
- session/thread/window identities are **not** inserted inside the Memory blob; actual identities remain in canonical client metadata;
- unavailable workspace enrichment is omitted.

The `:0` window value is Source's explicit detached-request construction. It is not a real context-window ID and must not create `context_window_id`, `window_number`, or a frontend window.

## Current MiniCode chain and exact gaps

| Stage | Current production behavior |
| --- | --- |
| Real startup producer | `ws/agent_runner.py:4469` schedules maintenance with the actual `conversation.id=A`, exact selected adapter and workspace, and leases that adapter to the actual memory task. Production scheduling is for a main conversation. |
| Scheduler/coordinator | `memory/generation.py::schedule_memory_startup` passes A to `run_startup`. `run_startup` uses A only to exclude the current conversation from extraction candidates. It does not retain A as a request owner. |
| Phase 1 extraction | `_run_phase1` claims B's durable revision and `_extract_phase1` puts B's rollout/cwd into the prompt. It constructs SideQueryOptions with operation `memory_phase1_extraction` and an existing Background label, but leaves its identity defaults untouched. |
| Side options | `llm/base.py::SideQueryOptions` creates a fresh random auxiliary session ID, leaves thread/turn empty, and has no Source detached-request facts. This is a generic existing standalone-side-call default, not Source's startup-A lineage. |
| Responses projection | `_responses_side_client_metadata` sends standalone session/thread and optional turn only. `_simple_responses_api` does not use the agent provider-step Source snapshot merge. No request_kind Memory, root turn, Source memory thread/trigger, startup source label or detached window is authored there. |
| Retry | `_run_auxiliary_call` reuses the same SideQueryOptions across retries. This is an existing correct identity boundary to preserve. Each separate extraction should mint its own detached turn, even when multiple old rollouts share startup A. |
| Internal routing context | `LLMSideCallContext.request_metadata` currently carries only an explicitly supplied turn's lifecycle metadata. Phase 1 supplies no foreground turn, so it returns an empty dictionary. A future Source side snapshot must reach both wire construction and actual transport-owner metadata if turn-scoped sticky routing is used; putting a blob only in a local options object does not connect that consumer. |
| Usage/visibility | Phase 1 is a side query, with no foreground LLMTurnContext. It must remain independent of A's foreground usage, accepted user-input history and UI. Sharing A's protocol identity does not mean sharing A's mutable accounting owner. |
| Publication | MemoryJobStore owns B's extraction job/revision/output, and Phase 2 owns its own artifact job. Request attribution must not replace those durable B/job identities with A or change atomic publication semantics. |

The forgetting entry points only perform Phase 2 and do not require a fictional Phase 1 startup owner. The proposed change should not make them construct one.

## Executable follow-up scope for Root to authorize

1. **Capture actual startup data.** Carry startup A through `run_startup → _run_phase1 → _extract_phase1` as explicit host-owned detached-request source data. The existing required `current_conversation_id` is the identity authority; do not use selected B's ID as a fallback. Preserve an original startup SessionSource/header only when the actual host supplied it. For the current main-only entry this means an ordinary source with no subagent/memgen header.
2. **Capture permission facts at the actual startup authority.** The current scheduling block precedes construction of `run_permission_context` and the model-execution snapshot. If permission metadata is included, obtain it from the actual original conversation/run authority at the scheduling boundary; do not read another currently-selected conversation later or borrow the foreground RunContext. Moving only the scheduling call to the point where that exact run's permission snapshot exists is one available small implementation; keep the existing MemoryManager setup and exact adapter/task lease ownership intact.
3. **Give SideQueryOptions an explicit optional Source snapshot.** Reuse its existing session/thread/turn fields for A/A/new detached turn. Add only the data needed to carry the authored Source blob and original source/detached-window compatibility facts to side-call adapters. Do not turn every existing `query_source` into a Source enum or add a generic fallback based on operation strings.
4. **Connect side request construction and transport metadata.** `_responses_side_client_metadata`, `_simple_responses_api` and `LLMSideCallContext.request_metadata` must consume the same authored detached snapshot and stable real IDs. Serialize Source request_kind Memory and root turn without pushing identity/window fields into the Memory blob. Keep the context-window feature disabled unless actually enabled; do not invoke the ordinary agent-turn merger, which would default the kind to Turn.
5. **Keep provider boundaries explicit.** Source client metadata and compatibility headers belong to configured Responses providers. Preserve existing Chat/Anthropic request schemas, normal standalone side calls, no-tool extraction schema and retry budget. Do not send Phase 2 memgen or account/reviewer-credit routing fields on ordinary Phase 1 requests. The current simple Responses side path uses HTTP; any additional transport work must be separately verified, not claimed from the agent-stream WebSocket tests.
6. **Mint one real detached turn per operation.** Source uses UUID v7. There is an existing six-line UUID-v7 implementation in `guardian/connection_pool.py::new_classification_id`; move that primitive into a small common ID utility and import it from both owners if this batch adopts that exact format, rather than creating a second generator or importing the Guardian pool as a memory dependency. Create the turn before the existing side-query retry owner, so retries preserve it.

Likely production files: `memory/generation.py`, the exact startup scheduling block in `ws/agent_runner.py` if permission/source capture needs it, `llm/base.py`, and the side-query sections of `llm/openai_adapter.py`; a shared UUID helper/import only if required for the exact Source ID format. No changes are proposed to WS lifetime ownership, foreground cost settlement, accepted-input origins, user drafts, frontend components, scheduler run origins or the corrected synchronous Guardian routing.

No implementation has started. Root should confirm this bounded scope and coordinate those exact shared files before production edits.

## Necessary verification after implementation

- A real startup A extracts persisted old rollout B through the actual coordinator and a local Responses socket. Assert the received protocol owner is A, while prompt and successful durable memory job/output still refer to B.
- Perform two extractions and one real retry. Check distinct detached turn/root IDs between extraction operations, stable IDs over retries, exact request kind Memory, body/header equality and the original startup source/header semantics. Assert no Phase 2 memgen header or fabricated collab parent.
- Inspect the Memory blob's exact shape, actual permission facts, explicit detached window compatibility identity and absence of context-window fields. Do not test a copied mock blob without the real startup producer.
- Report nonzero extraction usage while A's actual foreground cost totals/context and accepted input evidence stay unchanged. Preserve cancellation/error status and real job-revision ownership; do not add another accounting layer.
- Check the existing generic side-query caller/retry/cost contracts and configured Chat/Anthropic schemas once after the full batch. No rationale currently exists to rerun the six accepted lifecycle GUI scenarios or 86-case model-lifetime suite.

## Heartbeat read snapshot

```text
backend/tasks/scheduler.py 07f9c9432c087afb0d268a65a570571b18971f5e002b4a40af6a9e43d5ae98cd
backend/services/chat_api_service.py 285ff993f3a1167d50f6df817c65b5d4e613e338945f74155d8f597295f8d8f1
backend/services/scheduled_task_runner.py 72aefdbc0aedb17aa6cc681396a89b2bc4945cfc44eb81cdda2d31a386e6cdad
backend/agent/turn_input.py 2e9fb0fc9f715a26a51789b9680b6a3848563861bcb0d102961ccdc14b125efb
backend/agent/turn_iteration_runtime.py f721524c0431442d13ff4759494387151c1a16b0be8b2e4870580a22dbc16c26
```
