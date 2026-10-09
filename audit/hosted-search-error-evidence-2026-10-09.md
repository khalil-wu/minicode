# Hosted search: retained error evidence and remaining service failure

## Latest user failure

The relevant user profile is `C:/Users/ago/AppData/Roaming/minicode-desktop`, conversation `conv_mv0e36uq_wigkn9`. Its recorded model was `gpt-6.1-sol` and permission mode was `bypass`. This is distinct from the earlier isolated QA news conversations.

The conversation journal is `data/sidechains/conversation_0a86af3f5be6f0c6667fa24165a640266e269c0cf49f9e7024ec831e50d22113/events.jsonl` under that profile:

- Sequence 49: the Chinese news search failed after 15.669 seconds. Desktop log time 2026-10-09 11:12:48 Beijing time identifies request `3c0e99b2-b493-4644-976a-1f98f997873e`.
- Sequence 72: the English news search failed after 7.859 seconds. Log time 11:13:02 identifies request `6a1cb9c7-6739-4092-96b6-0be5dcb39639`.

Both requests reached the configured Responses endpoint and failed inside the SSE stream after HTTP 200. The persisted tool results had `provider_error_type=null`, generic `execution_error`, and an exception string truncated before the request ID. Reading these files did not change the profile, conversation, approval state, credentials, or application window.

## Confirmed local root cause

`WebSearchTool.execute` delegates hosted search through `LLMAdapter.side_query` to `OpenAIAdapter._simple_responses_api` and `_collect_simple_responses_stream`.

The collector previously called the shared Responses error extractor, then discarded its structured fields and kept only the message. The outer adapter wrapped that in a generic `RuntimeError` using `_clean_error_message`, which cut the text to 200 characters. WebSearchTool then returned another generic failure without provider error classification. This removed the stable code/type/status/request identity needed by the existing retry classifier, durable tool result, and expandable frontend diagnostics.

The correction is limited to two production files:

- `backend/llm/openai_adapter.py`: a concrete `ResponsesStreamError` reuses the main Responses error extraction/classification, retains the actual provider body/status/request ID, and keeps the complete redacted diagnostic. It accepts the same nested, top-level, `response.error`, and `response.failed` shapes as the primary stream. The outer side-query adapter logs a redacted message and rethrows the original exception, preserving structured HTTP exceptions as well.
- `backend/tools/web_tools.py`: a hosted-search failure now carries its actual provider, error classification, recoverability, and structured diagnostic metadata; the existing `developer_detail` contains the redacted provider evidence for inspection.

No retry budget, delay schedule, supplier fallback, auto-switching behavior, request compatibility branch, user setting, or UI layout was added. The existing auxiliary retry owner now has the original structured evidence on which to apply its current rules. Fatal billing/auth/model errors remain fatal; a declared transient server error can follow the existing retry policy.

## Request-contract investigation

An offline MockTransport capture ran the real configuration factory → WebSearchTool → Responses adapter using non-sensitive settings fields. The current request selected `gpt-6.1-sol`, `tools=[{type:web_search,external_web_access:true}]`, `tool_choice=required`, `parallel_tool_calls=true`, `stream=true`, and `store=false`.

The request **did not contain a `reasoning` field**: `disable_reasoning=True` attempted `none`, capability validation rejected that unsupported effort, and the field was omitted. The investigation therefore did not find an actual outbound `none` or `minimal` request for this model. Official documentation also explicitly permits `tool_choice=required` when a search must run: <https://developers.openai.com/api/docs/guides/tools-web-search>.

The Codex reference constructs hosted search from its configured cached/live/indexed mode in `.tmp/codex-src/codex-rs/core/src/tools/hosted_spec.rs`; its ordinary Responses builder in `core/src/client.rs` includes the model's resolved reasoning configuration and `tool_choice=auto`. Those differences alone do not establish that MiniCode's request is invalid. The actual comparison below was required before proposing a wire-format change.

## Real service comparisons: not a successful search acceptance

The parent task's bounded probes used the same configured supplier and model, with a 35-second single-attempt limit and no extra retries. Recorded evidence is in `output/projection-chain-20261008/hosted-search-wire-probe*.json`.

| Variant | Recorded result |
| --- | --- |
| Exact current request | Failed, 6.031 seconds |
| `tool_choice=auto` | Failed, 5.735 seconds |
| Required search with explicit low reasoning | Failed, 5.860 seconds |
| Plain control without a tool | Successful model reply, 7.953 seconds |
| Codex-shaped native request | Failed, 6.297 seconds |
| Preview search-tool variant | Failed, 6.297 seconds |
| Native text-and-image search | Search lifecycle events, then error; no accepted result |
| Native sources control | `searching/completed`, then error; receipt had `sources=null`, `results=null` |
| Precisely identified Responses Lite request | Ordinary reasoning/message output and `response.completed`, but no web-search receipt |

Live CPA metadata advertised `web_search=true`, `use_responses_lite=true`, and text-and-image search support. That advertisement did not turn the observed Lite answer into a search result. Its output items were only reasoning and message items. The sources control likewise supplied no source/result data that could support a partial factual search answer.

The search receipt guard remains intact. The service search execution is **still failing and has not passed live acceptance**. This patch fixes the confirmed local evidence-loss defect; it does not claim to repair or bypass the separately observed hosted service failure.

## Unified verification

```text
python -m pytest tests/test_openai_responses_alignment.py tests/test_web_tools.py backend/tests/test_side_llm_cost.py backend/tests/test_hosted_search_error_chain.py backend/tests/test_provider_handshake_error_chain.py backend/tests/test_hosted_search_receipts.py backend/tests/test_web_search_fetch_repairs.py backend/tests/test_web_search_capability_chain.py -q -o addopts=''
```

Result: **201 passed in 39.14 seconds; 0 failed, 0 skipped**. `git diff --check` passed for the two production files and new regression file.

The seven new regressions cover structured nested/top-level stream errors, long diagnostics with credential redaction, real HTTP-error objects, fatal structured errors without retries or supplier fallback, and transient SSE server errors recovering through the existing auxiliary retry owner while preserving request identity. The existing hosted-receipt tests continue to reject a model-only answer as proof of web search.

## QueryEngine persistence and final display contract

The completed result content now starts with `网页搜索失败。` followed by the actual redacted diagnostic. The extra application-added English `Hosted web search failed` prefix was removed; provider error evidence and the existing user summary remain available. No request construction or search-success rule changed.

An additional integration regression runs the real QueryEngine, code VM, WebSearchTool, Responses adapter, runtime state, public ToolCallRecord projection, event delivery, and journal recorder. A controlled SSE endpoint keeps returning `server_error`; the existing policy makes one attempt plus three retries. The test requires the final attempt's request ID, structured code/type/status, full diagnostic tail, and redaction to survive all of these surfaces:

- `AgentState.tool_calls[].developer_detail` and failed status;
- public API ToolCallRecord;
- `tool_result.error_info.developer_detail`;
- a newly constructed `ExecutionJournal` reading the written result from disk.

Private `runtime_metadata` is not required or exposed by any public projection. The regression explicitly checks that it is absent, so diagnostic completeness relies on the established public error fields rather than a private side channel.

The final focused batch was:

```text
python -m pytest backend/tests/test_hosted_search_error_chain.py backend/tests/test_tool_result_persistence_paths.py backend/tests/test_tool_call_record_projection.py tests/test_web_tools.py -q -o addopts=''
```

**58 passed in 18.67 seconds; 0 failed, 0 skipped.** This overlaps the earlier 201-test batch and is not added to it as a distinct-test total. `git diff --check` passed after the final content change. There are now eight new error-chain regressions including the QueryEngine persistence case.

The parent task also reported a post-fix real `WebSearchTool.execute` check: all three existing retries were used and the service still failed, while the final `developer_detail` was 565 characters and retained the `network` classification, `server_error`, and request ID `7094bb9f-08f9-4869-a7f3-790d2c7e1b3a`. This verifies improved error evidence, **not** successful real-time search.

The final public-source controls also omitted the auxiliary `query_source` marker and tested the advertised GPT-6 Astra model without changing saved settings. Both still received `server_error` after native search lifecycle events; neither produced source/result data. The current error is therefore not established as a Sol-only reasoning-level issue. These controls are recorded as failures, not search acceptance.

The three existing frontend reader modules (`ActivityCell`, `transcriptHydration`, and `InspectorTab`) passed 87 tests. They verify that the existing display/history/inspector paths still accept the error evidence; this does not claim a successful external search. No supplier configuration, credential, application draft, or historical failure was changed.
