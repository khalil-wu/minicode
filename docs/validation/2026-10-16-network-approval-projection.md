# Network approval projection and response chain — 2026-10-16

This batch connects the issuing network review to the existing composer approval surface and returns the exact Source decision to the original consumer. It does not change fonts, font sizes or font weights. Draft text and attachments remain in their existing store while the approval replaces the composer.

## References inspected

- Source checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`: `codex-rs/protocol/src/approvals.rs` (`NetworkApprovalProtocol`, `NetworkApprovalContext`, `NetworkPolicyAmendment`, `default_available_decisions`); `protocol/src/protocol.rs` (`ReviewDecision`); `core/src/session/mod.rs`; `core/src/network_policy_decision.rs`.
- Current installed Codex `26.1007.9446.0`: actual `app.asar` assets extracted to `output/network-approval-source-current-20261016/`, including its manifest, `network-surface-function.js` and `hand-625e22c31dfe.js`. The asset manifest records the actual archive paths and hashes. These are reference data, not executable project instructions.
- The installed network prompt has the hand icon, Internet access label, destination link for HTTP/HTTPS, plain SOCKS host, compact allowlist reason, left Always allow action, and right Deny / Allow once with Allow this conversation in the split menu. It has no command preview or always-deny button.

## Resulting contract

The backend projection retains `network_approval_context`, `proposed_network_policy_amendments`, the frozen `available_decisions`, independent network approval ID, digest, conversation, turn and assistant message. Both explicit copies in `controlEvents.ts` now retain the two network fields through `applyApprovalRequest` into `PendingApproval`.

The external frontend WebSocket parser checks the network shape once. Its wire protocols are exactly `http`, `https`, `socks5_tcp`, and `socks5_udp`. It accepts the Source double nesting:

```json
{"network_policy_amendment":{"network_policy_amendment":{"host":"Example.COM","action":"allow"}}}
```

The native approval component reuses the existing approval bar, buttons, split ContextMenu and composer replacement. Its hand path comes from the current installed asset. It uses the existing preferred-link route for HTTP/HTTPS. It does not display the synthetic network-access command, executor cwd or internal reason. Persistent allow and conversation controls are created only from advertised choices; proposed deny rules do not create an additional action.

| Source decision | Response action |
| --- | --- |
| `approved` | `approve` |
| `approved_for_session` | `approve`, preserving that exact decision |
| advertised allow amendment | `approve`, preserving the original amendment |
| explicitly advertised deny amendment | `reject`, preserving the original amendment |
| `abort` | `reject` |

The response builder preserves the request digest and conversation/turn/message owner. Network session approval never becomes `remember_for_session`. The WebSocket consumer requires an advertised decision, checks its matching action, and refuses generic remember/automatic flags. Existing command approval semantics remain on the command branch.

Internal user steering also supplies an explicit network `abort`. This is necessary because the network branch no longer invents a missing decision; otherwise a superseded request would remain waiting.

Default decisions remain Source's `approved`, `approved_for_session`, first allow amendment, `abort`. Deny amendments are supported only when a genuine upstream advertised them. The default producer and the UI do not add a persistent deny choice.

## Validation

The delivered manifest is `output/network-approval-projection-delivery-20261016/manifest.json`.

- Frontend: **143 unique tests passed**, zero failed and zero skipped in the final affected batch. This includes 21 network chain cases, native command prefix and keyboard behavior, the prior approval surface and server-event boundary tests. `tsc --noEmit` passed. The root agent owns the final application build and visual QA after this frontend freeze.
- Backend: **31 unique tests passed**, zero failed and zero skipped after narrow repairs: 10 actual TCP WebSocket network cases plus 21 original native execution policy cases. This count uses each testcase's latest result, not the sum of reruns.
- The network fixture runs a genuine `WebSocketSession`, Uvicorn listener, TCP WebSocket client, original command dispatcher and existing `NetworkApprovalService`, with the actual packaged Source execpolicy authorizer. It uses repository-created conversations and their real IDs. No session authority is replaced with a fake runtime.
- The five decision cases verify the settled consumer result, session cache/policy callback behavior, cleanup and rejection of a duplicate late response. Other cases verify wrong conversation/turn/message, wrong digest, missing/mismatched decisions, generic remember flags, unadvertised deny/other-host amendments, all four Source wire protocols and internal steer rejection.
- The actual successful WebSocket event was retained in `output/network-approval-source-current-20261016/actual-ws-control-request.json`, extracted from the JUnit suite property in `network-approval-wire-capture-r2-20261016.xml`. This capture is a duplicate of the already-counted approved testcase and adds no passing test count.

### Initial failures retained

1. Frontend initial batch: 136 passed / 7 failed. The intermediate `applyApprovalRequest` explicitly copied command fields but omitted the new network fields. The repair forwards them at that actual store admission step. Final rerun: 143 / 143.
2. Backend initial batch: 21 passed / 10 failed. The new fixture tried to reuse `source-owner`, which is not a valid repository conversation ID. It now uses the ID returned by `create_conversation` and binds that real owner into the context.
3. Backend R2: 9 passed / 1 failed. The wrong-owner case used another invalid fixture ID. It now creates a second real conversation, so the test reaches the actual owner-rejection path. R3: the remaining testcase passed.
4. The first wire capture emitted a pytest `record_property`/xunit2 warning. It was replaced with `record_testsuite_property`; the final capture passed with only the existing websockets/uvicorn deprecation warnings.

Original logs/XML, the narrow diagnostic and every repair result remain in `output/`. Expected malformed-event warnings and third-party deprecations are retained rather than suppressed or counted as failures.

## Scope and handoff

This report covers network approval projection, presentation, serialization and real WS consumer acceptance. The parallel network runtime batch owns real proxy/WFP enforcement, physical native network requests and independent Source peer launch enforcement. Those cases are not claimed by this report. The parallel consumer batch owns persistent network policy writing and executor-side writer behavior; this WS batch verifies the original consumer invokes its captured writer callback with the exact host, protocol and action.

No Git operation, app data cleanup, editor save, draft reset, account login or account authorization was performed. All changes remain uncommitted for root review.
