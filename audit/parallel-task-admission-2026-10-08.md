# Parallel Task admission and inherited permissions — 2026-10-08

## Actual failure

The weather task `conv_muzm5l9f_2zfb5h`, run `run_78dc62a62215`, asked ordinary agents to research Beijing, Shanghai, and Guangzhou. Parent journal sequence 236 rejected the first batch with `Parallel write-capable task(s) declare no write_scope`. No child had reached its real tool execution at that point. The parent retried with extra declarations, so the extra gate caused a preventable failed dispatch.

Source evidence: `output/playwright/projection-chain-20261008/qa-profile-1791467006091/data/sidechains/conversation_6a37f78f83bb5a5b1c94d84af9b5a8e88213d4acb114b249806e40fa12b0907e/events.jsonl`. Detailed independent operation accounting is in `output/projection-chain-20261008/weather-parallel-scope-audit.md` and its JSON companion. Wrapper duplication, parent `task_status` polling, and actual weather-source connection errors are separate evidence; removing this admission gate does not hide them.

## Codex correspondence

- `.tmp/codex-src/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs:263` defines `SpawnAgentArgs` with message, task name, agent type, model, reasoning effort, and fork options. It does not require a `write_scope` to spawn an ordinary worker.
- The v1 `SpawnAgentArgs` in `tools/handlers/multi_agents/spawn.rs:217` likewise has no scope declaration gate.
- `core/src/agent/child_config.rs:131` clones the parent's live turn configuration; `apply_spawn_agent_runtime_overrides` at line 171 reapplies its approval policy and permission profile snapshot to the child.

MiniCode's optional `read_only` and `write_scope` restrictions remain extensions. They narrow inherited permissions when explicitly declared. Their omission must not invent a conflict or prevent an otherwise valid ordinary parallel dispatch.

## Complete MiniCode path reviewed

1. Canonical Task schema: `backend/tools/agent_tools.py::_task_tool_parameters` supplies single and parallel tool schemas. Its old prose falsely made every writer's optional scope mandatory. The description now accurately says omission inherits the parent's allowed scope.
2. Parallel admission in `TaskTool.execute` normalizes structured agent type and workspace, resolves model configuration, and checks explicitly declared conflicting write scopes. The extra `_parallel_undeclared_writers` check then classified **every** unscoped general-purpose task as unsafe before either scheduler ran. This entire extra check and its sole helper were removed.
3. `_start_background_subtasks` and `_run_parallel_subtasks` still enforce real runtime capacity, ownership, hooks, cancellation, and lifecycle. They now receive an otherwise-valid unscoped batch.
4. `_run_single_subtask` still reads the live parent permission provider and builds the child's permission snapshot. Ordinary children preserve the parent's approval/network mode and explicit tool-deny rules.
5. `_narrowed_subagent_scope_metadata` still combines inherited and explicitly requested restrictions without widening them.
6. `backend/agent/tool_execution.py::subagent_scope_guard_reason` still blocks mutations under a read-only contract, out-of-scope file changes under an explicit write scope, and tools whose mutation targets cannot satisfy that declared scope. The normal permission checker still handles explicit parent deny rules.

No task-description or prompt keywords are used to infer read-only status, intended writes, or authorization. Existing explicit role definitions and structured restrictions retain their behavior. The scope change is two production files, with the removed gate accounting for most of the change.

## Regression design

The new `test_parallel_weather_without_optional_scopes_inherits_real_parent_permissions` uses the real TaskTool, foreground/background scheduler, QueryEngine, WebFetchTool, permission checks, runtime records, and per-child execution journals. Three independent local HTTP weather bulletins must all receive a request before the server releases any response, proving that the unscoped workers actually run concurrently. A controlled model supplies deterministic tool calls; this is not claimed as a live commercial-model or public-weather-site acceptance test.

Each child is submitted with only a description and prompt: no `read_only`, `write_scope`, or special agent type. The six cases cover foreground/background delivery crossed with unrestricted parent permissions, an explicit parent `write_file` deny, and an inherited parent read-only contract. All three sources must be fetched successfully without an approval request. Restricted cases additionally attempt a real write, require a blocked tool result, and verify that no file was written.

`SubagentRunRecord.read_only` stores the child's declaration, so it remains false when omitted. Effective inherited read-only behavior is verified through the actual blocked write and journal evidence rather than misinterpreting that declaration field as the effective permission snapshot.

## Validation

```text
python -m pytest backend/tests/test_subagent_startup_lifecycle.py backend/tests/test_task_tool_async.py backend/tests/test_write_scope_exclusivity.py backend/tests/test_subagent_context.py backend/tests/test_subagent_scope_guardrails.py backend/tests/test_subagent_preflight_projection.py backend/tests/test_subagent_execution_metadata_chain.py -q -o addopts=''
```

Final result: **181 passed in 173.97 seconds**, no failures or skips. This includes all six new three-city concurrency cases plus the existing explicit scope, permission inheritance, lifecycle, and projection regression modules. `git diff --check` passes for the changed production and test files.
