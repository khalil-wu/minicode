# Subagent roles inherit the actual parent permission profile

## Live task evidence

This review only read the user-operated profile `output/playwright/projection-chain-20261008/qa-profile-1791469894200`; no window, approval, pending question, or conversation state was changed.

Conversation `conv_muzq58wr_crpajr` had a persisted `permission_mode=bypass`, no explicit deny rules, and a bypass runtime environment in the parent's recorded context. Its latest manifest during the review was generation 66, revision 79, and the weather turn had already completed.

Two different requests occurred:

- Parent journal sequence 29 invoked `ask_user`; sequence 31 asked which three cities to research. This was a legitimate clarification and remains supported under full access.
- Parent sequences 97, 114, 117, 138, 142, and 155 mirrored six actual child `web_fetch` approvals. These were not the clarification question. The child journals independently contain the matching approval waits: Beijing `subagent-8995726b` at 26/39; Shanghai `subagent-120cc9a9` at 26/39; Guangzhou `subagent-eea06003` at 20/39. The parent mirror events must not be added again to the count.

The read-only SQLite inspection of `data/swarm/swarm.sqlite3` confirmed that all three children belonged to parent `run_a181a5bb3a78`, had `agent_type=explore`, `read_only=true`, `permission_mode=plan`, `team_mode=false`, and `plan_mode_required=false`. They eventually completed after their web fetches succeeded. The task did not have a new-conversation/UI mode mismatch: the backend's typed role branch had overwritten the inherited mode.

Root journal: `data/sidechains/conversation_eabc75e40b1a8b9f83f9368616bc33174a5ab60f07169ae3f6aaa2474f08976b/events.jsonl` under that profile.

## Root cause and complete correction

The prior ordinary `read_only` fix covered general-purpose children but retained `agent_type in {explore, plan} → permission mode plan`. The same builder separately reset default teammates to `confirm`. The teammate live permission provider also replaced the parent's actual mode with the child's previous mode before deriving the next context. That let a newly narrowed parent profile be bypassed by an independently retained teammate mode.

`backend/tools/subagent_context.py` now applies one permission derivation for all default roles and delivery types:

1. Inherit the actual parent mode, approval policy, sandbox profile, owner fields, managed constraints, grants, and explicit deny rules.
2. Clamp an explicitly requested child mode to that parent ceiling.
3. Retain an explicitly requested required-plan workflow as its own Plan admission gate.
4. Keep read-only role/task constraints in their real tool-execution contract; do not convert already-authorized web reads into a new approval policy just because the role is named `explore` or `plan`.

`backend/tools/agent_tools.py` now reapplies the true live parent snapshot in the teammate permission provider. It also resolves the built-in explore/plan read-only contract in the canonical execution-default path, replacing the scattered admission assignments. Startup and resumed/private execution therefore retain the role's actual write restriction when the approval mode correctly inherits bypass.

No user conversation or queued request was rewritten. Existing running applications need the rebuilt backend to use this source change; the audit does not treat an already-imported old backend module as updated automatically.

## Codex source basis

`.tmp/codex-src/codex-rs/core/src/agent/child_config.rs` clones the parent turn configuration in `build_agent_shared_config` and reapplies the runtime permission/approval snapshot through `apply_spawn_agent_runtime_overrides`, including after role configuration. A role definition does not replace the current parent approval/profile with a separate default.

MiniCode's explicit required-plan and read-only contracts remain supported extensions. The required-plan exit still follows the existing approved transition path, a requested mode cannot exceed the parent ceiling, and read-only mutation attempts remain blocked.

## Validation

Core batch:

```text
python -m pytest backend/tests/test_subagent_context.py backend/tests/test_subagent_startup_lifecycle.py backend/tests/test_task_tool_async.py backend/tests/test_subagent_scope_guardrails.py backend/tests/test_subagent_plan_review_handler.py backend/tests/test_subagent_preflight_projection.py backend/tests/test_subagent_execution_metadata_chain.py -q -o addopts=''
```

**240 passed in 171.62 seconds**, no failures or skips.

Related root and boundary batch:

```text
python -m pytest tests/test_subagent_lifecycle.py tests/test_regressions_infra.py backend/tests/test_source_audit_contracts.py backend/tests/test_deferred_tool_exposure.py backend/tests/test_harness_tool_surface_regressions.py backend/tests/test_multiagent_tool_surface.py backend/tests/test_tool_agent_approval_boundary.py -q -o addopts=''
```

**125 passed in 91.64 seconds**, no failures or skips. Combined result: **365 passed**.

Coverage includes all four parent modes across general-purpose, explore, plan, and custom role context construction, foreground/background/teammate delivery, and 18 real TaskTool → QueryEngine → HTTP WebFetch cases with attempted workspace writes. Full-access reads do not ask again; explicit web deny still blocks; read-only writes do not reach the filesystem. Existing required-plan approval, requested-mode clamping, inherited parent Plan boundaries, lifecycle, and tool-surface checks also pass.

The root `tests/` contracts were inspected and the relevant complete modules were run. The old backend tests that asserted a Plan parent could spawn a confirm teammate were corrected to the current inherited permission contract. `git diff --check` passed for the changed source and tests.
