# Synchronous Guardian routing correction, 2026-10-15

Status: **corrected within the available local-provider scope; final 23 focused cases passed, zero skipped**. The original 47-case and intermediate 21-case artifacts remain intact. No official account login/service implementation, ModelRuntime ownership, lifecycle, Node evidence or frontend changes were made.

## Retraction

The preceding internal-request batch made two incorrect Source claims:

1. It put a Guardian `parent_response_id` into ordinary Responses request `metadata`, after reading the local parameter name inside `set_guardian_metadata` without following its caller.
2. It added `x-codex-guardian: reviewer` whenever `internal_session_source="guardian"`, after reading the extension's header declaration without following its model/auth/routing gate.

The two original Guardian HTTP cases asserted those incorrect positive expectations. `output/source-internal-request-provenance-final-20261015.xml` still truthfully records 47 tests passing against that previous code, but it is **not evidence that those two expectations match Source**. Both earlier reports now carry a correction at the top; their history and original XML have not been silently replaced.

Reference Source: `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

## Complete Source chain

- `core/src/client.rs:1729` calls `set_guardian_metadata(&mut request.client_metadata, ...)` for HTTP.
- `core/src/client.rs:2042` passes `&mut ws_payload.client_metadata` for WebSocket.
- `codex-api/src/common.rs:301,357` serializes `client_metadata` by that exact field name, with no rename to ordinary `metadata`.
- `core/tests/suite/guardian_review.rs:271` checks `body["client_metadata"]["parent_response_id"]`; lines 289 onward also check that ordinary requests have no Guardian parent-response routing.
- `ext/guardian-v2/src/sync_reviewer/mod.rs:98` authors CodexResponsesHeaders for the provider's preferred reviewer model.
- `core/src/client.rs:1108` only applies that header declaration when its configured model equals the actual selected model, current auth satisfies `uses_codex_backend`, and the provider supports Codex backend routes.
- `core/src/client.rs:1119` requires actual Codex backend auth, the OpenAI provider, required OpenAI auth, and no separate env key, experimental bearer token, command auth or AWS auth.
- `model-provider-info/src/lib.rs:620` defines supported routes from the actual OpenAI provider name and an absent configured base URL or a URL ending in `/backend-api/codex`. A backend-looking URL alone does not establish auth or model eligibility.
- `core/src/client_tests.rs:1969` exercises model mismatch, API-key auth, missing auth and a custom URL, which all exclude scoped Codex headers.
- `core/tests/suite/guardian_review.rs:151` defines `credits_enabled` from actual auth/provider/route. Its custom/API-key/override branches still perform real Guardian review, while omitting reviewer-only credit headers and parent-response routing.

The existing MiniCode `history_notes_backend_enabled` and `TokenBudgetAuthContext.supports_codex_backend_routes` are not exact aliases for this entire Source gate. The former accepts a different set of credential forms; the latter is an explicit provider configuration fact. Neither was repurposed as permission to issue reviewer-only requests.

## Current local behavior

The available configured custom/API-key/overridden-model routes do not have the Source scoped backend-reviewer authority. The correction removes the automatic reviewer header and the incorrect ordinary-metadata field. It also removes the now-unused provider-protocol forwarding of that parent-response value.

Root's independent follow-up found a second boundary issue: a nested general `llm_request_metadata.internal_session_source` could still survive `remaining` and reach the adapter even when the real host had no internal source. The request producer now removes exactly the new host-only fields `internal_session_source` and `parent_response_id` from that override, alongside the already-excluded Source blob. The canonical top-level runtime source remains authoritative. Other existing override keys were not broadly rewritten.

Synchronous review continues with its real separate reviewer session, `x-openai-subagent=guardian`, Source `thread_source=turn_trigger=guardian_review`, and actual parent thread/turn facts. It sends **no** `x-codex-guardian` or parent-response routing field on these local compatible paths. Memory-only `x-openai-memgen-request` remains restricted to the actual internal memory producer.

For a genuinely eligible Source backend-reviewer request, the correct parent-response container is `client_metadata`. This batch does not implement, simulate as a live service, or claim completion of that excluded official-account route. It does not construct a dummy auth owner, transfer a ModelRuntime, infer authority from a model name, or change user-configured network endpoints.

## Verification

Interpreter: `C:\Users\ago\AppData\Local\Programs\Python\Python312\python.exe`.

Intermediate `output/source-guardian-response-route-correction-20261015.xml`: **21 passed, 0 failures, 0 errors, 0 skipped; 41.499 seconds**. After the additional producer boundary correction, final `output/source-guardian-response-route-host-boundary-final-20261015.xml`: **23 passed, 0 failures, 0 errors, 0 skipped; 72.748 seconds**.

- Six actual local HTTP cases cover custom, OpenAI API key with the preferred API model, and an explicitly overridden model, each with and without a candidate parent-response value. They reuse the real GuardianRuntime and inspect the socket server's received headers and bodies. All preserve real Guardian provenance and omit ineligible reviewer-only routing from both `metadata` and `client_metadata`.
- Three actual WebSocket cases cover the same provider/model forms. Each reused reviewer runs three new real turns, with two supplied candidate parent-response values and then none. Real handshake/body captures show no ineligible reviewer header, no stale parent response and distinct real child turns on the same reviewer thread.
- Seven other cases in the same 16-case internal-chain module retain the actual memory HTTP/WebSocket, real file read, nonzero independent usage, Source snapshot merge, ordinary Background exclusion and Anthropic schema evidence. The ordinary request now also carries a supplied unrelated parent-response candidate, which is not projected.
- Two added real ordinary QueryEngine admissions supply nested forged Memory/Guardian sources, Source blobs and parent-response candidates. Neither request acquires an internal header or Source snapshot. Together these make the final internal-chain module 18 cases; the real top-level memory and Guardian producer cases still pass in the same batch.
- Five existing cases cover the private reviewer harness, cold native compaction/reviewer HTTP, and reviewer cache identity.

No prior lifecycle/desktop GUI scenarios were repeated. This correction verifies the scoped local paths and does not claim unrelated concurrent Node/heartbeat work passed because these tests passed.

Post-batch source hashes for Root's combined freeze:

```text
backend/agent/provider_protocol.py ee136fbb184d1e59ddf587b5f1430e3a3e95d638bfe1272b43880a23b6e6fbd8
backend/llm/openai_adapter.py 41b465d1ed146cd6ee98d3df28a5b5070511493f42fc2f3be18ab7d086a1a51b
backend/tests/test_internal_request_provenance_chain.py e836556d85e53727f65375dce9f4a202fad5ded595bfa1f6bc0ce0c47235d471
```
