# Hooks / Skill dependency parity handoff — 2026-10-24

Status: implementation READY for the root task's unified verification. The authoring agent did not run pytest, builds, type checks, a model, GUI, a real MCP server, or plugin installation. Fixtures are written, not reported as passing.

## Source basis

Reference checkout: `.tmp/codex-src`, recorded commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

- `codex-rs/core/src/mcp_skill_dependencies.rs`: first-party originator gate; `SkillMcpDependencyInstall`; approval and managed admission around installation.
- `codex-rs/login/src/auth/default_client.rs`: Source's first-party originators. Mini's desktop producer now identifies itself explicitly; SDK callers retain their own source.
- `codex-rs/hooks/src/events/interrupt.rs`: root-turn interrupt observer, exact input fields, strict optional `systemMessage`, failures cannot block cancellation or instruct the model.
- `codex-rs/core/src/hook_runtime.rs`: last running step's model/permission data, transcript flush, root-only execution; `never` maps to `bypassPermissions`.
- `codex-rs/core/src/tasks/mod.rs`: interrupt hooks before the public aborted-turn event; unrelated shutdown is excluded.
- `codex-rs/hooks/src/engine/discovery.rs`: Interrupt default 1 second / maximum 3 seconds, command and synchronous MCP handlers; SessionEnd MCP unsupported.
- `codex-rs/hooks/src/engine/mcp_runner.rs`: recursive typed `${field.path}` templates; missing fields are actual failures.
- `codex-rs/core/src/hook_mcp_executor.rs`: call an already connected, policy-filtered MCP tool without startup/wait; carry thread metadata; concatenate text content; fail on MCP error.

The ledger labels `hooks_interrupt`, `hooks_mcp_executor`, and `integrations.skills_dependency_originator` are audit labels, not invented Source feature flags.

## Implemented chain

1. `turn_iteration_runtime` passes `minicode-desktop` for the real desktop session source and retains SDK/other source values. `skill_activation` admits Mini's own first-party identities. The existing approval, managed-policy recheck and real installation service remain the installation owner.
2. Discovery carries `server` / `tool` / object `input` through the immutable definition into the HookManager entry. `mcp_tool` stays synchronous; unsupported SessionEnd MCP and unsupported Interrupt handler types produce discovery warnings. Source's `statusMessage` spelling is accepted.
3. MCP execution resolves the current step's `run_context.mcp_manager` when executing. It does not retain a stale manager from before environment capability refresh, start a server, or go through the model tool registry. Existing request ownership, cancellation, timeout, thread metadata and captured remote permission scope reach `MCPClient.call_tool`.
4. `QueryTerminalTransaction` invokes Interrupt after the durable terminal/journal commit and before lifecycle observer completion and public completion/DONE. The turn's MCP environment is still alive at this boundary. Only a root turn's `interrupted` cancellation with user interruption provenance qualifies.
5. Interrupt takes a detached tool execution context with no expired turn cancellation/deadline. Its own timeout still applies. Strict success/error evidence reaches the existing `system_notice` and `hook_response` item surfaces. Raw stdout/stderr remain in the synchronous hook evidence. Neither synchronous nor asynchronous Interrupt can veto cancellation, supply model context, or request asyncRewake.

## Files

Production: `backend/agent/skill_activation.py`, `backend/agent/turn_iteration_runtime.py`, `backend/agent/query_terminal.py`; `backend/hooks/models.py`, `discovery.py`, `policy.py`, `manager.py`, `runners.py`, `reducer.py`; new `backend/hooks/mcp_executor.py` and `backend/hooks/interrupt.py`.

Fixtures:

- New `backend/tests/test_hooks_mcp_executor.py`: typed templates, missing fields, config-to-current-step execution, connected/enabled catalog restriction, thread/request ownership, remote permission scope, text-only output, real executor timeout/error.
- New `backend/tests/test_hooks_interrupt.py`: discovery/timeouts, strict output/once behavior, real command stdin after cancellation, async terminal independence, actual durable terminal transaction ordering, root/subagent/shutdown scope and failure non-veto behavior.
- Updated `backend/tests/test_skill_mcp_dependency_policy.py`: actual `run_agent_loop` SDK vs desktop originator entry; the fake installer must run before the first provider request for desktop, while SDK retains the unavailable-dependency evidence. Existing approval and policy recheck coverage is retained.

No changes were made to the original ledger or `backend/services/tool_registry_factory.py` by this work.

## Proposed unified commands

Run from the repository root using the project's Python environment:

```powershell
python -m pytest backend/tests/test_hooks_mcp_executor.py backend/tests/test_hooks_interrupt.py backend/tests/test_skill_mcp_dependency_policy.py -q
python -m pytest backend/tests/test_hooks_contract.py backend/tests/test_hooks_matcher_alignment.py backend/tests/test_codex_compaction_hooks.py backend/tests/test_release_tools_owner_contracts.py backend/tests/test_agent_loop_lifecycle_cleanup.py backend/tests/test_run_timing_owner_chain.py backend/tests/test_architecture_review_repairs.py backend/tests/test_process_text_protocol.py -q
```

## Limits / unverified

- All commands above remain unrun by this agent. Root owns the final pass/failure record.
- No live MCP transport, remote executor placement, user installation, GUI lifecycle or model run was exercised.
- Existing Mini hook configuration retains its snake_case event keys; this change does not migrate every event's config/input/output spelling to Source's PascalCase/camelCase protocol. Interrupt input/output uses the exact Source fields; MCP template input additionally exposes the Source `hook_event_name`.
- No new MCP forms/protocol or executor-scoped hook discovery was introduced.
