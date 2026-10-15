# Source granular approval policy

Reference checkout: `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

The wire/config shape follows `codex-rs/protocol/src/protocol.rs:961` and `app-server-protocol/src/protocol/v2/shared.rs:206`. `sandbox_approval`, `rules`, and `mcp_elicitations` are required booleans. `skill_approval` and `request_permissions` default to false. The Core `on-failure` alias normalizes to `on-request`; a bare `granular` string is not an executable policy.

## Implemented chain

| Source contract | MiniCode consumer |
| --- | --- |
| Complete typed policy object | Layer merge validates the final effective object; config settings, native checker construction, session runtime and SDK implicit context preserve it. Higher config layers can override a single granular boolean without losing the lower-layer fields. |
| Exact managed constraint | `allowed_approval_policies` parses full policy objects and compares complete normalized values, as in `config/src/config_requirements.rs`. A policy with different granular flags is rejected rather than coerced to the first allowed string. |
| Explicit rule vs sandbox prompt | `core/src/exec_policy.rs:216` supplies independent rule and sandbox gates. Actual configured/session ask rules retain their attribution; default routing lists are identified from config provenance. Ordinary allowed operations are not denied just because a granular prompt category is disabled. |
| No ordinary Never exec prompt | `core/src/tools/sandboxing.rs:195` returns Skip for Never. Ordinary commands continue within their actual sandbox without the old generic shell-expansion prompt. Explicit rules and destructive/capability boundaries remain authoritative. |
| Strict turn-grant review | Existing strict grants produce the actual `tool/strict_auto_review` route into the frozen-request Guardian consumer. Builtin routing cannot rename it as a sandbox prompt. Explicit rules, escalations and stronger capability restrictions cannot masquerade as strict review. `ext/guardian-reviewer/src/routing.rs` validates and approves Full access before reviewer routing, even when strict; Never + Disabled therefore emits no reviewer assessment. Never + Managed and granular requests retain actual review. |
| stdin pre-hook policy gate | `core/src/tools/approvals.rs:486` gates actual sandbox overrides before permission hooks, with the Source strict+Never exception. Ordinary strict stdin retains its existing review routing. |
| MCP tool approval category | The admitted MCP ToolSpec supplies the producer category. MCP tool approval is independent of granular sandbox/rules/elicitation flags; Never still prevents a human prompt. No tool-name prefix is used to infer this new category. |
| MCP elicitation gate | The originating child call publishes its live permission owner and actual sandbox profile. Forbidden elicitation categories decline before hooks or UI. Never accepts an empty form only with a published Disabled/External/full-disk-write authority, matching `core/src/session/mcp.rs:940` and `codex-mcp/src/mcp/mod.rs:91`. Missing or restricted authority declines. |
| Stable transitions | Live permission rebuilds, narrowed child contexts and standalone Plan entry/exit retain granular flags. Full access is the explicit Never mode override. |
| Owner-scoped settings protocol | Permission-mode admission, session projection, capability available modes and updates use the owning ConfigLayerStack. Backend session and capabilities payloads carry the complete object. Frontend wire types and store partial updates preserve it. No new permission controls or typography changes were added. |

`request_permissions` consumes the normalized optional flag through the existing request/grant implementation. This Source revision declares `skill_approval` but has no execution producer that consumes it; no synthetic Skill approval flow was added. Network approval is independent of the granular flags in the typed policy helper; this batch does not claim to add a missing network-approval producer or remote-environment MCP profile system.

## Validation evidence

`output/granular-20261014-run01.xml` records a collection failure from the concurrent Guardian import cycle; no cases executed and none are counted as passed. The import cycle was fixed in the Guardian owner.

`output/granular-20261014-run02.xml` records 327 cases, 322 passed and five fixture-contract failures. `run03.xml` records 415 cases, 413 passed and two failures: one incomplete projectless fixture and the old bypass shell-expansion prompt. These earlier runs remain failure evidence rather than successful completion claims.

The final result is reconstructed by case identity in `output/granular-20261014-results.json`: **518 distinct backend cases passed, with no failing, error or skipped cases in their last executions**. Repeated runs count once. `run07.xml` is the final affected Source/checker/Guardian/permissions batch: 190 passed. Unchanged cases from the wider SDK, MCP, config, WS, Plan and child scopes retain their latest earlier passing execution.

Earlier strict tests initially used an External parent profile, which correctly could not provide the Source executor-managed readonly reviewer. The fixture was corrected rather than weakening that producer restriction. A second diagnostic incorrectly expected a reviewer under Never + Disabled; the Source Full access first branch was verified and the assertion corrected. The failed diagnostic XML is retained. A separate Never + Managed test now consumes a real strict turn grant, freezes a real patch request, enters the controlled reviewer transport and applies actual bytes to a temporary file. Granular command review similarly controls actual Python process/file I/O. Explicit rule and escalation variants verify that no reviewer runs when the policy forbids their true category. These contract tests do not claim to test the review model's judgment or a production OS sandbox.

Frontend verification passed **60 cases in three suites**: `approvalPolicy.chain.test.ts` (2), `capabilities.test.ts` (9), and `server-event-validation.test.ts` (49). The new chain tests cover inbound network normalization through capability/session store projection and partial updates while retaining the draft. TypeScript/build validation is included in the parent integration batch; no visual or font changes belong to this scope.
