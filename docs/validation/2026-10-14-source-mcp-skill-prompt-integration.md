# Source MCP / Skill / Prompt integration — 2026-10-14

Reference checkout: `822e58cc3d666166c7446c5b1ea2e52f5d09594c`. This closes the open implementation batch; it does not establish whole-product Codex equivalence.

## Implemented production chains

MCP `tools/call` uses the official SDK's `send_request` framing/lifecycle with an un-narrowed native result body. Source `CallToolResult` accepts arbitrary content values; the SDK's closed content union previously rejected unknown block kinds. Code Mode receives complete native `content`, `structuredContent`, `isError` and content-level metadata, with only the root `_meta` stripped. The complete result is retained in the private execution record; it is not copied into public WebSocket tool events.

Model history admits a separate `mcp_output`: Source structured-content precedence (except encrypted items), native text/image/audio/encrypted blocks, unknown content as JSON text, wall time and output header, exact issuing model truncation policy, tighter per-tool `output_token_limit`, and `ceil(policy * 1.2)` serialization allowance. Image detail defaults to high and original detail follows captured model capability. Audio uses decoded duration at 10 tokens/second, falling back to URL size only when Source's accepted media cannot be decoded. Model-visible text/media/encrypted estimates, checkpoints and cold replay use this admitted body; raw MCP cannot bypass the context budget. Normal compaction replacement drops the native field with the replaced body. MCP is not sent through MiniCode's unrelated generic per-user output quota.

MCP approvals preserve three-state read/destructive/open-world hints. Auto uses Source's exact decision order; Writes/Prompt/Approve and parallel routing remain independent. Never preapproval uses the actual issuing Disabled/External/full-write profile; strict review precedes preapproval. Explicit asks, hard denies and plan restrictions remain. The frozen proxy → request → SDK query chain was exercised for real allowed/blocked tool execution.

Skill discovery consumes the admitted owner config stack, ordered selectors, include/bundled/context-budget settings and Source product restrictions. Actual Agent Plugins v1 manifest and direct-child skill discovery feed the same snapshot. Agent Plugin bodies have Source's 8,000 UTF-8 byte cap and warning. The full Source skill usage instructions and aliases are rendered. Bootstrap refreshes the manager after loading the current turn stack, so readable roots and activation use that turn's policy.

Captured model templates/collaboration modes propagate through the selected adapter and builder, including intentional empty strings. Unknown model descriptors receive `model_info_from_slug`'s local prompt and Bytes(10,000) policy at the descriptor producer. No model-slug prefix guesses are used for catalog templates or TokenBudget activation.

## Validation

- Batch01: 314 passed / 15 failed; original XML retained. Failures included missing fixture api_key/transport/catalog initialization, old read-only/log assertions and fixtures that accidentally requested explicit write approvals.
- Batch02: deliberately stopped the owned pytest process after an obsolete fake Session.call_tool seam caused an unbounded wait following the move to SDK send_request. It is excluded from pass counts. The fake transport seam was updated and all affected cancellation cases ran in Batch03.
- Batch03: 180 passed / 3 failed; original XML retained. Remaining issues were the official fallback prompt's literal newline preservation, the old unknown-descriptor assertion, and the patch fixture's default explicit DIFF_REVIEW rule.
- Batch04: 91 passed / 0 failed. Full CodeVM and affected Skill/Prompt/catalog groups completed after those fixes.
- Latest case-identity union across completed batches: **398 passed / 0 failed / 0 error / 0 skipped**, in `output/mcp-skill-prompt-20261014-latest-results.json`. These overlap earlier audit batches and must not be summed with them.
- New raw-result test starts a real stdio JSON-RPC process and invokes it through QueryEngine + CodeVM, including a 90,000-character body, 5,000 structured rows, native error result, image metadata, unknown block and root-private metadata exclusion. This is not a proxy-only fixture.
- Fresh independent Electron `output/playwright/codex-control-audit-20261013/production-guardian-run05`: 4 scenarios passed, 7 screenshots, no page errors. Actual WS/repository/file I/O verifies review/denial/manual authorization/retry and draft preservation; only model HTTP responses are controlled.
- Bundled Python actually imports av 17.1.0 and the native result projector. Both pyproject and `desktop/requirements-sidecar.lock` declare it.

## Remaining boundaries

Later verified batches closed MCP session/permanent approval, native tool_search/full discovery metadata, Code Mode media/output and History/Notes transport plus the SDK/native-body/cold chain. See `2026-10-14-active-work.md` and its latest linked reports; these later changes are not included in this earlier batch's pass count. Native account entrypoints and official service validation, Guardian async V2/ThreadOwned checkpoints and enhanced tools, remote execution and advanced review remain open. Temporary Electron profiles do not verify Windows native readonly ConPTY enforcement. Controlled model decisions do not validate a real model's risk judgment. The saved Source UI extraction is a reference artifact, not proof of the currently installed Codex version.
