# Phase 1 detached memory request provenance, 2026-10-15

Status: **implemented in the authorized scope; 42 distinct focused cases passed, zero skipped**. This batch is frozen for Root's combined package verification. It does not repeat or supersede the accepted six desktop lifecycle scenarios or the 86-case model-lifetime batch.

Reference: `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`; the prior full producer/consumer audit is `2026-10-15-heartbeat-review-and-phase1-provenance-scope.md`.

## Resulting ownership and wire behavior

When startup conversation A triggers maintenance, extraction of old persisted rollouts B and C uses **A's actual protocol owner**, while the durable extraction jobs and outputs remain owned by B and C. The prompt still describes the selected old rollout; its identity is not substituted for the startup owner.

- `MemoryStartupSource` is a small frozen data snapshot passed through this exact `run_startup → _run_phase1 → _extract_phase1` invocation. It contains no RunContext, Session, ModelRuntime, adapter owner or mutable accounting sink.
- Each known-startup extraction sets SideQueryOptions session/thread to A, and mints its own UUID-v7 turn/root before entering the existing auxiliary retry owner. Two extraction operations have distinct turns; a retry reuses its operation's original identity and snapshot.
- Source detached metadata uses `request_kind=memory`, `thread_source=turn_trigger=memory_consolidation`, and the actual startup sandbox diagnostic when supplied. The Memory blob contains no session/thread identity, agent name, context-window ID/number or invented parent. Protocol session/thread identities stay in canonical `client_metadata`.
- The detached window compatibility identity is Source's explicit `A:0` construction. It is not a physical application window or a context-window-feature allocation.
- Responses body `client_metadata["x-codex-turn-metadata"]` and the HTTP compatibility header serialize the same authored snapshot. The real side-call transport receives the same A/A/turn ownership metadata; this does not borrow the foreground LLMTurnContext.
- The current production scheduler entry is a real main conversation and has an ordinary startup SessionSource, so it sends no subagent or memgen header merely because extraction is memory work. An explicitly host-supplied original subagent label is preserved by the side serializer; it is never rewritten into Phase 2's Internal(MemoryConsolidation) label.
- A missing startup ID produces **no Source startup snapshot**. The existing generic standalone auxiliary identity remains explicitly unbound; it is not reported as known A and does not adopt B. This is covered separately from the actual persisted-A success case.
- Chat Completions and Anthropic Messages retain their supported wire schemas. Chat may carry the existing short diagnostic identity in its normal metadata; Anthropic retains only its supported user_id. Neither receives a Responses blob or Codex-specific headers from this change.

The production maintenance scheduling call moved only far enough to capture the original run's canonical resolved permission facts after `run_permission_context` is constructed. The earlier MemoryManager initialization remains in place, and the same `_lease_session_llm_for_task(self, run_llm, memory_task)` binds the actual same adapter/task. The existing foreground-task wait, coalescing key and resource lifetime were preserved. A coalesced second caller does not rewrite the first real startup source.

The original UUID-v7 primitive was moved verbatim from Guardian's connection pool into `backend/ids.py::new_uuid7`. The existing `new_classification_id` import name remains an alias, so the real classifier caller keeps its existing algorithm and ownership behavior.

## Production scope

| File | Change |
| --- | --- |
| `backend/memory/generation.py` | Carry actual startup source through candidate extraction; construct one detached Source request per operation without replacing durable old-rollout job IDs. |
| `backend/llm/base.py` | Three optional Source snapshot/compatibility fields on SideQueryOptions; pass the same actual detached identity into side-call transport metadata. Existing standalone calls without a Source snapshot keep their old behavior. |
| `backend/llm/openai_adapter.py` | Source side-client metadata and actual detached-window compatibility header; normal agent-step Source merger and corrected local Guardian routing remain intact. |
| `backend/ws/agent_runner.py` | Only the maintenance scheduling location and data snapshot, using this original run's resolved sandbox diagnostic and unchanged actual adapter/task lease. Root separately owns native span/journal/clock changes in this shared file. |
| `backend/ids.py`, `backend/agent/guardian/connection_pool.py` | Share the existing UUID-v7 primitive under its original Guardian alias. |

No new retry layer, fallback based on operation names, account route, frontend control, accepted-input-origin change or memory publication subsystem was added.

## Verification

Interpreter: `C:\Users\ago\AppData\Local\Programs\Python\Python312\python.exe`.

| Artifact | Cases | Result |
| --- | --- | --- |
| `output/source-memory-phase1-provenance-initial-20261015.xml` | 39 | 39 passed, zero errors/failures/skips; 32.243s. |
| `output/source-memory-phase1-provenance-host-callers-20261015.xml` | 3 | 3 passed, zero errors/failures/skips; 7.280s. |

These are 42 different case IDs, not a sum of repeated executions. No repair/retry run was needed.

The five new cases in `test_memory_phase1_source_request_chain.py` exercise actual local sockets and the real coordinator/job store:

- **Actual scheduled startup A:** persist A, B and C in ConversationRepository; complete a real foreground A QueryEngine request; call the production `schedule_memory_startup(A)`; extract both old rollouts, with a real controlled 503 and retry; check every received session/thread/turn/root, UUID version, Source blob/header shape and sandbox diagnostic; verify B/C outputs are still durably indexed by B/C.
- **No foreground accounting borrow:** each successful fixture response reports 7 input / 3 output tokens. Foreground A stays **7/3**, total after the two extraction responses is **21/9**. A's exact context snapshot and side-call record list stay unchanged; every actual extraction LLMSideCallContext has no foreground turn owner.
- **Coalescing ownership:** a second caller with B and a different sandbox diagnostic receives the existing original maintenance Task and cannot replace A's captured source.
- **Explicit original-label contract and unknown owner:** a host-supplied original collab label survives without a memgen header. This is a serializer contract fixture, not a claim of a production child-thread startup integration. A separate empty-startup case emits no Source startup blob/window/turn, and never adopts the analyzed rollout ID.
- **Actual Chat and Anthropic requests:** extraction completes through both real local provider parsers and publishes the old rollout's job output while preserving each protocol's supported metadata schema.

Phase 2 is deliberately stubbed in these five Phase 1 cases and is **not** counted as a new Phase 2 pass. Existing Phase 2 behavior remains covered by earlier independent evidence.

The remaining necessary cases are nine existing memory-generation cases, 23 auxiliary cost/retry cases, two memory coalescing/full-transcript cases, and three directly affected original callers: real AgentRunner WS processing, actual memory Task/model-adapter drain, and real eight-thread Guardian prewarm/reuse that validates UUID-v7 identities. The strict auxiliary lifecycle fixture's signature was updated only to accept the new explicit optional startup facts; the 86-case lifecycle suite was not rerun.

## Practical boundaries

This batch closes the known startup-A versus analyzed-B attribution path for the available providers. It does not add optional Git/workspace enrichment that MiniCode does not actually possess at this boundary, invent Source installation/account facts, or turn an unknown startup into a known one.

The current standalone simple Responses side-query implementation uses HTTP. The new Phase 1 cases verify that real path, not an unexecuted side-query WebSocket path. Existing normal agent-stream WebSocket and memory Phase 2 WebSocket evidence must not be cited as a Phase 1 WebSocket pass.

Root owns the combined source/package freeze. In particular, the whole-file agent_runner hash may include Root's concurrent native consumer changes; this scoped result is not an all-tree or installed-wheel claim.

Post-batch read snapshot:

```text
backend/ids.py 14f24e2ddb0b4485c42f8263d3810ade1f875e9fc7f94c96581f4857cd7157cc
backend/memory/generation.py ec9f97adbebb0bb9aaf2b03bc9dc4c6d7ede7f2166245aec0f9f7525efe7498e
backend/llm/base.py bfd577a954a904abb23a913ebba7600ba33d5bf6f2ef9120477ca012856f8a6f
backend/llm/openai_adapter.py 2e372c13f9d5152700b7732dabb41232f92902fb3aa4cde590a30b9a0acf1dba
backend/agent/guardian/connection_pool.py 1cdaadebedcc947ba00382789c6cb267e49338780fcfd4835db09042e3bcc5c4
backend/ws/agent_runner.py ca758ca2f6335c10421eda19da05b2374dcd17d9e452b9195a9abd2266c09ea8
backend/tests/test_memory_phase1_source_request_chain.py 74ccd8b30c238197913763fcb7a3a168dfa8d92750f6974e1e8452ff6a0b1e32
backend/tests/test_ws_auxiliary_model_lifetime.py 8d5cabcb5eb6eef4b3aaf372a6a6f06d997f91bb1f90362a84206c44f2be2bed
backend/tests/test_internal_request_provenance_chain.py 42cbcccfea1f561832de6d10dbd62ba50626da338996b35e032f4b95692d2b68
```
