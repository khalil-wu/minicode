# Internal memory and synchronous Guardian provenance, 2026-10-15

**Correction to this historical report:** the original claims that every internal Guardian should send `x-codex-guardian: reviewer`, and that its `parent_response_id` belongs in ordinary request `metadata`, were wrong. The Source HTTP and WS callers pass **client_metadata** to `set_guardian_metadata`, and only after the real selected-model/backend-auth/routing gate admits reviewer headers. The two original local Guardian cases used incorrect positive expectations. Their 47-case XML is retained as execution history, not proof of those Source claims. The correction and new scoped evidence are recorded separately in `2026-10-15-guardian-response-routing-correction.md`.

Status: **this scoped implementation is complete; 47 focused cases passed, zero skipped**. No frontend changes, ordinary-window interaction, lifecycle rerun, official-account work or package build was performed by this subtask.

The preceding read-only audit is `2026-10-15-internal-request-provenance-readonly-audit.md`. Its read snapshot and findings are historical, before this authorized follow-up implementation. Reference Source remains `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

## Resulting producer-to-consumer behavior

- The actual memory-consolidation producer now supplies the existing MiniCode `query_source="background"` diagnostic instead of allowing its request/QueryChain to default to User. Its accepted input remains User in its own independent context and Guardian evidence owner, including a real export/load round trip.
- Memory and synchronous Guardian producers author their actual `internal_session_source` and Source metadata in their host runtime. Memory uses `thread_source=turn_trigger=memory_consolidation`; Guardian uses `guardian_review` and the real reviewer parent thread/turn. A root turn is copied only if the issuing host actually has one. Memory acquires no parent or collaborator-spawn identity.
- The request producer copies the canonical host's Source snapshot and internal source. An `_source_responses_metadata` value inside the general `llm_request_metadata` override is still discarded.
- The admitted provider step merges the existing Source snapshot and only supplies missing real identity/default request facts. Existing session/thread/turn, request kind, window identity/number and start timestamp survive. Optional context-window fields are added only from a real enabled context-window owner. Internal Source attribution works with that feature disabled.
- Phase 2 memory and normal Guardian agent acquisitions default to Source `request_kind="turn"`. A previously authored request kind is preserved; the generic step does not overwrite a Memory/Compaction snapshot with Turn.
- OpenAI Responses HTTP and WebSocket project the real internal source as `x-openai-subagent`. Only Internal(MemoryConsolidation) acquires `x-openai-memgen-request=true`. Synchronous Guardian acquires its Source reviewer header and the canonical parent-thread compatibility header. An actual Guardian `parent_response_id` goes in the Responses request's `metadata` object, matching `core/src/client.rs::set_guardian_metadata`; it does not enter the Source turn blob or `client_metadata`.
- Chat Completions keeps its existing MiniCode request diagnostics, now correctly Background for this memory worker. Internal provenance, Source blobs and Guardian-only parent-response routing remain out of generic sanitized metadata. Anthropic Messages continues to receive its supported `user_id` shape. A generic Background label alone creates no memory or Guardian headers.

Memory's independent session/model lifetime, usage owner, tool registry, permissions, local stream consumption and frontend visibility were preserved. Source Phase 2 is ephemeral and clears user-facing notifications; this work does not create a resumable memory conversation or invent a new UI state.

## Production changes owned by this subtask

| File | Scoped change |
| --- | --- |
| `backend/memory/consolidation_agent.py` | Replace the dead prompt-context source marker with actual runtime provenance and explicit existing Background classification. |
| `backend/agent/guardian/runtime.py` | Only the `GuardianRuntime.run` metadata dictionary: actual internal reviewer source and Source parent/trigger facts. SourceNotes separately owns Node admission/cursor edits in the same file. |
| `backend/agent/provider_protocol.py` | Carry canonical host Source metadata and internal session source, plus a real Guardian parent response where supplied. |
| `backend/agent/provider_stream_runtime.py` | Merge authored Source facts, preserve existing facts/kind, and keep real window enrichment conditional. |
| `backend/llm/base.py` | Keep internal source and Guardian-only parent-response routing outside the generic provider metadata schema. |
| `backend/llm/openai_adapter.py` | Responses-only internal source projection, memory/reviewer headers, actual parent-thread compatibility projection and Guardian request metadata. |
| `backend/tests/test_internal_request_provenance_chain.py` | Nine focused cases using actual producers, QueryEngine, local HTTP/WebSocket transports and real file-tool execution. |

Other agents' existing changes in these shared files remain intact. No commit, reset or wholesale file replacement was performed.

## Validation

Interpreter: `C:\Users\ago\AppData\Local\Programs\Python\Python312\python.exe`.

Final result: `output/source-internal-request-provenance-final-20261015.xml` — **47 tests, 0 failures, 0 errors, 0 skipped; 88.049 seconds**.

| Cases | Count | Verified |
| --- | --- | --- |
| New internal provenance chain | 9 | Actual memory producer over Chat/Responses HTTP; controlled real 503 retry; one successful real `read_file(file_path=...)`; received Source headers/body and stable identities; independent nonzero memory usage; accepted User origin and snapshot/load; real Responses WebSocket across file-tool continuation; reused Guardian runtime with optional real parent response; authored snapshot merge with window feature off/on; ordinary Background exclusion; Anthropic metadata boundary. |
| Source history-notes chain | 15 | Existing Source window/identity and private notes behavior under the modified provider metadata merge. |
| Source model request policy | 17 | Existing request policy and dedicated reviewer/side-call ownership behavior. |
| Existing private reviewer + cold native compaction | 3 | Existing standard reviewer harness and actual HTTP compaction/reviewer provenance. |
| Existing memory canonical control plane | 1 | Memory remains on QueryEngine's canonical lifetime. |
| Existing sticky HTTP/WebSocket route | 2 | Existing route/owner behavior remains valid. |

The two actual HTTP memory cases start a real foreground QueryEngine request before maintenance. Each successful provider response reports 7 input / 3 output tokens. The memory worker retries one controlled 503 and then performs its real read and final response: memory accounting is **14 input / 6 output**; the original foreground accounting stays **7 input / 3 output**, and its exact context snapshot is unchanged. These are controlled local-provider results, not claims of a live external model or a new desktop screenshot.

For Responses, the test asserts on the socket server's actual received HTTP headers and body. The WebSocket case captures the real handshake, performs the real read, and verifies the next `response.create` uses the same internal attribution and the actual `function_call_output` continuation.

Initial history is retained in `output/source-internal-request-provenance-initial-20261015.xml`: **6 passed / 3 failed**. All three failures were a QA provider script using `read_file(path=...)` instead of the registered tool's `file_path` schema. The product correctly blocked those invalid calls. Only the script's arguments were corrected; no product tool/schema bypass was added. The initial run is not counted as successful file execution.

The six previously accepted lifecycle/auxiliary desktop cases and the 86-case model-lifetime suite were not rerun. This scoped result does not claim the entire current shared tree was frozen during execution or that concurrent heartbeat/Node work is covered. Root owns the combined final source/package freeze.

## Remaining boundaries

- Phase 1 detached-memory Source metadata is still a separate follow-up from the read-only audit. This batch did not replace its standalone side-query identity or apply Phase 2's internal memory header to it.
- Root separately owns scheduled-heartbeat per-input origin/trigger admission and its persistence/steer validation. This subtask did not change TurnBoundaryInput, accepted-origin heuristics, scheduler/chat API or checkpoints.
- SourceNotes separately owns actual Node evidence acceptance and reviewer cursor work. The synchronous reviewer's metadata dictionary was coordinated; its Node evidence behavior is not claimed complete by this report.
- A local configurable remote executor is separate project scope; this report does not label an environment ID or permission field as remote execution support.

## Post-batch read snapshot

These are source-file hashes read after the passing batch, for Root's packaging coordination. `guardian/runtime.py` is shared with SourceNotes; a subsequent coordinated Node edit can change its whole-file hash without changing this subtask's metadata dictionary.

```text
backend/memory/consolidation_agent.py      fe369a2a975c12420c8563269b08a9590a4e9feb6113aea67692430fafd7d919
backend/agent/guardian/runtime.py          a43f89d3cfd84ffada65535e4c344a247f106cbe07c9fb6306f30d7e2b7bff34
backend/agent/provider_protocol.py         d3dd49183892ac68adcd27ae6598fa6ebab4fd2d2d49fea15173eb4f9c283e0e
backend/agent/provider_stream_runtime.py   6dd5de285ef3a8546eb18eee3a6dd7bbd01ece98ded4cbbee1c444b02bd53cbd
backend/llm/base.py                        86d0e5955680c86d8afa14f2af37eaa80307cf56b18a18848285bec7b19ca98f
backend/llm/openai_adapter.py              c340003bc1f0548267c61742123e80afca9b822b7a5239541e32c222e37a1526
backend/tests/test_internal_request_provenance_chain.py 25f1912822d6fff4c78d292f6e378304972513534a89f68d22fc3cec0e07aaf2
```
