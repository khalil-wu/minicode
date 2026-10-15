# Internal request provenance: read-only audit, 2026-10-15

**Later Source caller correction:** the separate implementation following this audit initially misread synchronous Guardian's request container and header eligibility. Source puts an eligible reviewer's `parent_response_id` in **client_metadata**, and custom/API-key/override requests do not acquire reviewer-only headers merely from an internal Guardian source. See `2026-10-15-guardian-response-routing-correction.md`; the old evidence and audit text remain historical.

Status: **audit only; no production code or existing delivery evidence changed**. This is a new follow-up to the frozen lifecycle/native-policy work, not a failure of the six accepted lifecycle/auxiliary GUI cases. No tests or GUI scenarios were rerun for this audit.

Reference checkout: `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

## Findings and evidence limits

1. **Observed MiniCode request classification error:** the real Phase 2 memory-consolidation HTTP request in `output/playwright/codex-control-audit-20261013/production-native-execpolicy-ui-run01/provider-requests.json` carries `metadata.query_source="user"`. The source of that value is the missing producer field and the existing generic default, not the name of the session. QueryChain uses the same default. `query_source` is MiniCode's diagnostic/routing label; it is **not** a Codex Source wire contract field.
2. **Confirmed Source provenance projection gap, statically traced:** Phase 2 has no effective MiniCode InternalSessionSource/ThreadSource/turn-trigger producer and no path to the corresponding Responses metadata/headers. Merely changing `query_source` would leave that gap intact. The captured native-policy request uses Chat Completions, and the QA server retained request bodies, **not received HTTP headers**. Its capture therefore does not establish an actual Responses header failure. The missing Responses path is established by the production producer and consumers below; next-batch verification must capture received headers.
3. **No demonstrated foreground usage or visibility leak:** the memory query owns a distinct AgentSession, context builder, RunContext/LLMTurnContext and accounting session. The existing accepted auxiliary GUI shows an independent held memory HTTP request while `isStreaming=false`, `active_task=null`, and only the completed foreground assistant answer remains. There is no basis to add UI suppression or change the already-fixed lifetime owner.
4. **Accepted-input origin must remain separate:** Source's memory task submits ordinary `user_input` into its own internal session. Source `UserInputOrigin` is `User` or `Heartbeat`; `User` means ordinary/unclassified, not a promise that a human typed that text. Do not classify an input as `agent` because its request source starts with `memory`.
5. **Related paths are not identical failures:** Guardian review already sets `query_source="guardian_review"`; classifier already supplies Source-specific metadata directly; Phase 1 uses an explicit standalone side call. None is another observed `query_source="user"` memory request. A separate static heartbeat provenance gap is described below and should be tracked separately.

The QA server's `main` boolean is `Boolean(body.tools?.length)` in `native-execpolicy-desktop-qa.cjs`. Memory consolidation has real file tools, so this boolean cannot establish that the product treated it as a foreground human task.

Observed memory request identity:

```text
session_id = memory-consolidation-2177d48306ca4eb2850ef93174edc7e6
thread_id  = memory-consolidation-2177d48306ca4eb2850ef93174edc7e6:turn
turn_id    = run_c69d684aa8f2
query_source = user
no conversation_id or assistant_message_id in this memory request
```

## Source producer and consumers

### Phase 2 consolidation

- `codex-rs/memories/write/src/runtime.rs:398`: `spawn_consolidation_agent` starts a distinct thread with `SessionSource::Internal(InternalSessionSource::MemoryConsolidation)` and `ThreadSource::MemoryConsolidation`.
- `runtime.rs:420`: the actual `TurnInputRequest::user_input(prompt)` has `TurnStartOptions.turn_trigger="memory_consolidation"`. It does not pretend to be a spawned collaborator and does not fabricate a parent thread.
- `codex-rs/core/src/session/turn_input.rs:446` and `codex-rs/history/src/heartbeat.rs:23`: accepted origin is obtained from the turn trigger. Only `automation_heartbeat_scheduled` maps to Heartbeat; memory maps to User. The accepted-input order and origin travel on `UserInputMetadata`.
- `codex-rs/core/src/turn_metadata.rs:226`: TurnMetadataState captures the real session source and thread source. `responses_metadata_template` includes the captured `thread_source` and `turn_trigger`.
- `codex-rs/core/src/session/turn.rs:1667`: normal agent acquisition requests use `request_kind="turn"`. Phase 2 is an ordinary agent turn in an internal memory thread; do not label every memory acquisition `request_kind="memory"`.
- `codex-rs/core/src/responses_metadata.rs:458`: Internal(MemoryConsolidation) projects `x-openai-subagent="memory_consolidation"`. Internal sources do **not** acquire `subagent_kind` via `subagent_metadata_kind`.
- `responses_metadata.rs:327,377`: the canonical Responses `client_metadata["x-codex-turn-metadata"]` and compatibility HTTP headers are projections of the same authored metadata. No Source field named `query_source` or `minicode_source` is required.
- `codex-rs/core/src/client.rs:785,805`: actual HTTP compatibility headers add `x-openai-subagent=memory_consolidation` and, only for Internal(MemoryConsolidation), `x-openai-memgen-request=true`. The memgen header is a transport projection, not a reason to invent a collab parent or to relabel accepted user evidence.
- `codex-rs/memories/write/src/phase2.rs:383`: the background worker reads the memory thread's own token usage, reports `MEMORY_PHASE_TWO_TOKEN_USAGE`, shuts down that thread, and keeps job ownership if shutdown is not complete. This does not publish another user turn into the parent's chat.

### Phase 1 extraction is different

`codex-rs/memories/write/src/runtime.rs:310` builds a detached model client from the startup thread's actual config/session source and `self.thread_id`. `detached_memory_responses_metadata` (`core/src/turn_metadata.rs:126`) authors:

```text
request_kind = memory
thread_source = memory_consolidation
turn_trigger = memory_consolidation
subagent header = derived from the actual startup SessionSource
```

It does **not** blindly change that startup SessionSource to Internal(MemoryConsolidation). Thus Phase 1 must not inherit the Phase 2 memgen/subagent header solely because its operation string contains `memory`.

## Current MiniCode producer and consumers

### Memory producer

`backend/memory/consolidation_agent.py:78` constructs one new internal agent with its own actual session/task IDs, registry and context. It writes `state.prompt_context["internal_session_source"]="memory_consolidation"` at line 98. A repository search finds no consumer of that key. It does not appear in the runtime metadata at lines 109–116, which only includes mode/role and memory-disable switches.

The metadata defaults then flow through:

- `backend/agent/loop_bootstrap.py:704`: QueryChain source defaults to User.
- `backend/agent/loop_session.py:235`: prompt query source defaults to User. `agent_role="memory_consolidation"` survives independently.
- `backend/agent/provider_protocol.py:887`: request query source defaults to `user` (or supplied MiniCode source). This is the exact producer of the captured HTTP metadata.
- `provider_protocol.py:830`: only subagent/background modes or roles enter the collaborator branch. The current memory `agent_mode="internal"` appropriately does not create a collab-spawn parent. Preserve that separation when adding Source attribution.
- `backend/agent/provider_stream_runtime.py:164`: `_source_responses_metadata` is built only when the context-window/token-budget feature is enabled, and its current shape has identities, window fields and `request_kind="turn"`, but no internal thread source/turn trigger. Memory's separate ContextBuilder is not bound to the host's model-execution context-window snapshot, so the dead state marker supplies nothing here.
- `backend/llm/openai_adapter.py:1134`: `_responses_client_metadata` projects supplied identities/explicit x-openai-subagent and the Source metadata object. It does not receive the dead marker. No current producer supplies a memory subagent label.
- `openai_adapter.py:1074`: `_responses_identity_headers` does not implement the memory memgen header. Chat Completions instead forwards sanitized MiniCode metadata in its normal `metadata` body (`openai_adapter.py:6221`); Source does not define a Chat Completions `query_source` contract to copy.

### Accepted input and Guardian evidence

`backend/agent/turn_iteration_runtime.py:324` currently derives an origin from whole-turn `query_source` and parent-run identity. Query sources starting `memory`, `side_query`, `extension` or `guardian` become `agent`; Heartbeat is recognized only by the exact source `heartbeat`.

For the current memory producer, the missing source defaults to User, parent_run_id is absent, so the accepted origin is User. This matches Source for this independent memory session. It is then recorded into this session's own Guardian evidence (`turn_iteration_runtime.py:344`), not into the foreground user's evidence owner. `QueryEngine` creates/binds that distinct GuardianHost (`query_engine.py:815`).

`GuardianEvidenceOwner.record_accepted_message` (`guardian/retained.py:367`) changes `origin="agent"` into retained assistant evidence. `guardian/host.py:101` makes the same conversion when reconstructing the model window. Adding only `query_source="memory_consolidation"` would therefore silently change the origin and both evidence paths. That is an observable semantic change and is not justified by Source's input-origin model.

The existing MiniCode source label `background` avoids this unrelated origin change while correctly stopping QueryChain/provider diagnostics from saying User. It is already used by Phase 1 and other auxiliary operations. Source internal identity must still be authored separately; `background` is not a substitute for it.

### Usage and visibility

- `backend/memory/generation.py:162` schedules maintenance with `contextvars.Context()`, avoiding inheritance of the foreground task context.
- `backend/agent/query_engine.py:762` gives each submit a fresh LLMTurnContext. With the memory's actual independent session ID, it has an independent cost session.
- `backend/agent/loop_bootstrap.py:739` and `backend/agent/provider_stream_settlement.py:201` settle foreground acquisitions against that owned `cost_session_id`, not the string `query_source`.
- `backend/llm/base.py:1278` attributes standalone side calls to an explicitly supplied LLMTurnContext, or global accounting without a foreground cost-session owner if none was supplied. This is not evidence of a foreground cost leak.
- The native-policy fixture sends no usage frame, so its capture cannot prove nonzero token-accounting totals. This audit claims ownership from the traced code, not a numeric billing pass.
- `run_memory_consolidation_agent` consumes its own QueryEngine stream locally, provides no frontend `emit_event` callback, and creates no main-conversation projection. Existing accepted auxiliary GUI evidence confirms the resulting visibility. Do not solve the attribution gap by hiding more UI events or changing active-task flags.

## Other examined entry points

| Entry | Current source/authority | Finding |
| --- | --- | --- |
| `memory/generation.py:508` Phase 1 | `SideQueryOptions(operation="memory_phase1_extraction", query_source="background")`; standalone side query, no accepted foreground input | No observed User-default query. The generic side adapter projects only standalone session/thread/turn identity; Source detached-memory request kind/thread source/trigger are not supplied. `SideQueryOptions.query_source` has no adapter consumer. This is a separate static Source projection gap, not proof that Phase 1 was billed to the foreground. |
| `agent/guardian/runtime.py:248` review | Explicit `query_source="guardian_review"`, its own session/context and parent-turn metadata | Not the missing-memory-User-label bug. It lacks authored Internal(Guardian)/GuardianReview/guardian_review provenance and its x-openai-subagent projection. Source `ext/guardian-v2/src/sync_reviewer/mod.rs:89` sets Internal(Guardian), GuardianReview and a reviewer-specific provider header; `ext/guardian-reviewer/src/settings.rs:100` supplies `turn_trigger="guardian_review"`. Coordinate any change with SourceNotes' active reviewer/evidence work. |
| `agent/guardian/async_sampler.py:186` classifier | Explicit classifier record/source; direct request authors guardian subagent metadata, classifier thread source/trigger and parent linkage | Different implemented request path. Do not overwrite its request kind, internal source, model or headers with the memory patch. |
| `tools/agent_tools.py:2672` real children | Actual `parent_run_id`, `query_source="background"` or `subagent`, explicit child role/mode | Not the independent memory entry. Preserve its real collaborator linkage; never force memory through this branch to obtain a header. |
| `ws/run_manager.py:1143` completion wake | Explicit `query_source="task-notification"`, `_parent_notification_only` | Not another missing-source producer. No basis from the memory capture to reclassify its notification as an accepted human prompt. |
| `hooks/runners.py:446`, `tools/web_tools.py:143,882` | Explicit SideQueryOptions and explicit owning turn context where appropriate | Auxiliary operation/accounting path, not independent accepted user input. Generic query_source currently has no wire consumer; Source does not require this MiniCode field to be invented on Responses. |
| SDK / ordinary REST / eval drivers | Actual external user submissions into canonical QueryEngine | The default User source alone is not a defect for these real user entry points. |
| scheduled heartbeat | `scheduled_task_runner.py:275` wraps the stored heartbeat prompt; `chat_api_service.py:276` passes `automation_kind` but no trigger/query source | Separate, statically confirmed provenance omission: scheduled heartbeat is accepted as ordinary User by `turn_iteration_runtime.py:324`; Source `UserInputOrigin::from_turn_trigger` requires Heartbeat for `automation_heartbeat_scheduled`. No heartbeat execution was rerun in this audit. Ordinary cron/manual prompts must remain User. |

## Smallest coherent next-batch scope

Root should explicitly choose the next production batch; the current frozen delivery remains intact.

### Fact ownership, persistence and provider support

| Fact | Actual producer | Lifetime / persistence / recovery | Consumer |
| --- | --- | --- | --- |
| Internal(MemoryConsolidation) | `run_memory_consolidation_agent`, which really constructs that isolated worker | Source Phase 2 explicitly sets `ephemeral=true`, disables recursive memory generation/use, and clears user-facing `notify` (`memories/write/src/phase2.rs:305`). MiniCode creates a fresh session on each actual `_consolidate_phase2` invocation; coordinator job/artifact retry is not chat-turn recovery. No new resumable memory conversation or checkpoint migration is needed. | Internal worker provenance → memory subagent compatibility label and memory-only transport header. |
| Memory thread source | The same isolated memory-thread producer | A transient immutable fact for that worker; recreate from the producer on a new job invocation. Do not recover it by parsing an old generated session ID. | Source turn metadata `thread_source="memory_consolidation"`. |
| Memory turn trigger | The actual consolidation turn submission | A per-turn fact, stable over that turn's iterations/retries. Do not replace its trigger with a generic turn label. | Source turn metadata `turn_trigger="memory_consolidation"`; accepted origin stays User. |
| Guardian internal/thread source | `GuardianRuntime` owns its actual separate reviewer session | Existing reviewer session/context reuse owns this identity. Reauthor it when the host actually creates/runs the reviewer; do not copy it from parent prompt text or promote a parent's session into an internal one. Existing reviewer context snapshots remain owned by their reviewer. | Source Guardian compatibility label `guardian`, thread source `guardian_review`; no memgen header. |
| Guardian turn trigger | `GuardianRuntime.run` when it mints its real child turn ID | A fact of that new review, not a parent window or saved message. Parent/root linkage must remain the actual reviewing turn's supplied linkage. | Source turn metadata `turn_trigger="guardian_review"`. |
| MiniCode `query_source` | Existing host runtime producer | Local request/chain classification. Generic `_save_checkpoint` already copies it; no new global field or Source enum is needed. Ephemeral memory does not use conversation checkpoint resumption (`query_recovery.py:80` requires both session and conversation). | Existing MiniCode QueryChain/prompt/provider diagnostics and sticky transport-owner tuple. It is not Source's accepted-input origin or required wire field. |
| Accepted User/Heartbeat origin | The actual input-admission boundary | `ContextBuilder.export_snapshot` serializes `input_origin` (`context.py:4057`); deserialize restores it (`context.py:4447`). Guardian retained evidence checkpoint preserves each message's origin independently. New provenance metadata must not rewrite these facts during load/replay. | Guardian evidence/parent-window reconstruction; heartbeat input coalescing where implemented. |
| Request kind | The actual request operation | Per-request serialized snapshot; preserve an already-authored request kind. Phase 2 and ordinary Guardian acquisitions use Turn; detached Phase 1 uses Memory. | Source Responses `x-codex-turn-metadata` blob. A generic default must not overwrite a supplied Memory/Compaction kind. |
| Window/session facts | Existing canonical loop identity and, only if active, actual context-window owner | Do not add fictitious context-window IDs/numbers to satisfy attribution. Preserve any already-authored Source snapshot fields when merging the newly authored internal provenance. | Existing Responses snapshot/client metadata/HTTP or WS projection. |

Provider boundary: Source `client_metadata` and `x-codex-turn-metadata`/memory compatibility headers belong to the **OpenAI Responses protocol**, including a configured compatible provider using that protocol. The normal Responses HTTP and Responses WebSocket paths must project the same authored facts; transport changes must not mint another origin. MiniCode Chat Completions can retain its existing local `metadata.query_source` diagnostic. It has no Source requirement to acquire a Codex Responses blob or memgen header. Anthropic Messages must keep its own supported metadata schema; do not inject Responses-only fields or headers into it. The classifier has a dedicated Source request path and must not be rewritten by the ordinary reviewer/memory implementation.

### A. Fix the observed Phase 2 classification and complete its Source projection

1. In the actual memory producer (`consolidation_agent.py`), use the existing MiniCode `query_source="background"`, and author its real Source internal source, thread source and trigger in the host-owned runtime metadata. Do not derive them from session ID prefixes, agent names, prompt text or tool inventory. Keep its accepted input User without editing whole-turn origin heuristics solely for this fix.
2. Carry that authored Source provenance through `provider_protocol.py::build_llm_request_metadata` and `provider_stream_runtime.py` into the Source Responses snapshot independently of the optional context-window feature. Only window/number/history-ingest fields depend on that feature. Preserve `request_kind="turn"` for these Phase 2 agent acquisitions.
3. Project the internal memory subagent compatibility label and exact `x-openai-memgen-request=true` into Responses transport from this provenance in `openai_adapter.py`. Do not synthesize `subagent_kind="collab_spawn"`, a parent thread, or fake identity values. Do not claim Source requires Chat Completions to send new Codex headers or a new custom query-source taxonomy.
4. Preserve existing RunContext/model/LLMTurnContext ownership, usage settlement and local-only stream consumption. No WS lifecycle, timing, draft, frontend component or resource-owner changes are required by these findings.

This fixes the one observed error and the associated actual Source projection chain. It does not close all three other Source gaps by assertion.

### B. Separate source-backed follow-ups

- Extend the same authored provenance projection to the actual Guardian review producer after coordination with SourceNotes; do not alter its classifier request path or retained evidence merely to add wire attribution.
- Give Phase 1 an explicit detached-memory Responses metadata path using its actual startup owner/source identity. Match Source request_kind Memory, thread source and trigger; do not copy Phase 2 Internal headers by operation-name inference.
- Correct scheduled-heartbeat accepted origin at its real scheduler admission, using Source's `automation_heartbeat_scheduled` trigger semantics. Store/replay the accepted-input origin independently of the active turn and of content strings; ordinary/manual/cron input stays User. This requires its own focused admission/checkpoint verification, not a memory lifecycle rerun.

No requirement is established here for a new UI status, custom background conversation, official account integration, globally hiding failures, or additional retry/fallback layers.

## Necessary next-batch verification

After the chosen production changes are complete, batch these checks once:

- Exercise the real memory producer through QueryEngine, with a controlled local provider and nonzero usage, for both Chat and Responses where changed. Inspect actual received Responses headers/body. Assert Phase 2 thread_source/trigger, `request_kind="turn"`, correct memory compatibility headers, stable real IDs across two tool iterations/retries, and no fabricated parent/subagent kind.
- At accepted-input admission, inspect the actual message and Guardian evidence owner: the memory prompt remains User inside its independent owner; the main conversation's retained messages/order and cost totals do not change. Inspect both live evidence and persisted/restored message provenance if those paths are touched.
- Assert separate memory accounting records with known nonzero token usage; foreground totals remain the exact foreground usage. Record cancellation/error accounting independently if settlement is changed; no reason currently exists to rewrite settlement.
- Use one focused GUI attribution companion only if frontend-visible behavior changes or the provider harness needs an end-to-end visibility check. The six already-accepted lifecycle/auxiliary GUI cases and the 86-case lifetime suite do not need rerunning solely for metadata.
- For any separately chosen Guardian or Phase 1 work, capture its own actual request and verify its distinct Source semantics. For heartbeat work, verify actual scheduler admission, save/load origin, and a real human steer retaining User origin; an automation_kind field assertion alone is insufficient.
- Keep static audit, test pass, real provider request and UI evidence separate. Do not call an unexecuted path passed.

## Read snapshot

Hashes record what this read-only audit inspected; they are not a claim of a new build or installed-package freeze.

```text
backend/memory/consolidation_agent.py  f82411372c571488c26c3b5592d0c90590abdf5908b6b1320f4f388d34423e0a
backend/memory/generation.py           933d6f62f51ab611f4dcd3e995a5f88b1367ecd5756d48e517c058b72e8665f9
backend/agent/provider_protocol.py      620416a85ea739b671e81e9368d268200af3299a0e01a9e49776f6ef3f172502
backend/agent/provider_stream_runtime.py 5a130170d73aec599545e3a0458ef2467f157b54dea48813c095e36b05b541a3
backend/llm/openai_adapter.py           bb1a6ceffc82fc3bdcfd7b7995289e3ab419c5b842afbc166c67fac343d0a7f8
backend/agent/turn_iteration_runtime.py e81fbf476d32021f5e2187e9182d299e5930a6252fc34d55150c6d5df122547b
backend/services/chat_api_service.py    b43e89c3809e2221b48c3f23573458773bcada43458a6aabc0b3d37d9aa23ac8
backend/agent/guardian/runtime.py       b02e3324d75428fa99e19b69b4dedcacd303b567103af548a6fe1cbb29e9ed6b
```
