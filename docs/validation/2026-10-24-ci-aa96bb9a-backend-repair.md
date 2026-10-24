# aa96bb9a backend/root CI repair — 2026-10-24

READY for Root's combined build/new-head CI run. The settings/layout batch remains intact. No workspace Git mutation, commit, GUI, user permission change, full test suite or full build was performed for this repair.

## Complete failure inventory

The backend job `120834143261` recorded10 failed /8049 passed /164 skipped. Root job `120834143210` recorded3 failed /1790 passed /52 skipped. The empty root log was refetched to `output/full-parity-audit-20261023/github-aa96bb9a-backend-root-refetched.log`; its dedicated fetch stderr file is empty. No signed download URL was printed.

| CI failure | Root cause / repair |
|---|---|
| Guardian private reviewer | Production: runtime-selected utility/coordination tools were registered before applying the session's narrow scope. Canonical ToolSpec extraction now accepts a selected instance before registration; automatic registration applies the same scope first. Guardian explicitly binds its five permitted tool names as its active selection. Exact registry/schema constraints remain strict. |
| Skill admission `[minicode-True]` | Production: the earlier desktop-originator fix also admitted generic `minicode`. That generic SDK identity is no longer first-party. The actual desktop producer still sends `minicode-desktop`, and its real-loop installation fixture remains passing. Source's own originator list/prefix and feature/policy gates remain intact. |
| Canonical plan writer | Fixture: explicitly selects `tools.update_plan.enabled=true` instead of depending on the old default. Production gate remains off by default. |
| Compaction command evidence | Fixture: asserts the complete original command output, all3000 progress lines, both call IDs, running handle, error flag and error tail; rejects a truncation marker and replacement characters. |
| WebSocket runtime projection | Fixture constructed via `__new__` now initializes `selected_model` and `provider` using its actual config; no production `getattr` fallback. |
| Readonly Git boundary ×2 | Fixture accepts the actual safe `rev-parse --verify --quiet --end-of-options HEAD` call. It also verifies that the resolved HEAD/empty-tree identity reaches the diff call. |
| Real active sleep ×2 | Fixture model lacks Source's `clock` capability, so the model-driven sleep gate rejected its call and the child reached a second iteration without entering sleep. The fixture now explicitly selects Source's `always_on` mode in its captured model config and asserts the provider receives the sleep tool. Existing10-second entry timeout, real30-second sleep, active-session residency, cancellation and parent-task assertions remain. |
| WS extension registry identity | Fixture now checks the actual per-run fork contract: different registry maps, same schema owner, exact tool-name set and same tool implementations. Lifecycle runtime identity checks remain. |
| Root conversation deletion | Fixture supplies the real lifecycle collaborator's workspace resolver; deletion/resource ordering assertions remain unchanged. |
| Root weather/question tool sets ×2 | Fixtures now include the three enabled goal tools in their exact expected sets, while retaining the original MCP/read tool and prompt/no-extra-execution assertions. |

## Files

Production:

- `backend/tools/catalog.py`: reuse canonical instance metadata before registry mutation while preserving registered-name validation.
- `backend/agent/tool_schema_derivation.py`: scope automatic utilities and coordination registration.
- `backend/agent/guardian/runtime.py`: explicit reviewer active-tool selection.
- `backend/agent/skill_activation.py`: only the explicit Mini desktop identity is added to first-party admission.

Fixtures:

- `backend/tests/test_runtime_tool_gates.py`, `test_codex_guardian.py`, `test_canonical_control_plane.py`, `test_harness_long_task_fidelity.py`, `test_harness_tool_surface_regressions.py`, `test_readonly_git_service_boundary.py`, `test_subagent_thread_services.py`, `test_ws_extension_composition.py`.
- `tests/test_regressions_websocket.py`, `tests/test_smoke_models.py`.

No changes were made to `backend/agent/tool_execution.py`, its parent-recipient receipt hunk, the frontend parent-message projection, or the original audit ledger.

## Directed verification

First batch:165 cases,162 passed /3 failed /0 skipped. The three failures were introduced fixture-construction errors: the new explicit AppConfig constructors omitted required `LLMSettings.api_key` in plan and the two sleep cases. These were corrected with an offline fixture key; no production fallback was added.

Follow-up:45 affected cases,45 passed /0 failed /0 skipped. This includes all runtime utility gates, the actual private Guardian session, both real active-sleep cases, and the plan writer.

Merged by JUnit `(classname,name)`, taking each case's latest outcome: **165 passed /0 failed /0 skipped**, not210 unique tests.

Evidence under `output/full-parity-audit-20261023/`:

- `ci-aa96bb9a-backend-directed.log` / `.xml`
- `ci-aa96bb9a-backend-directed-followup.log` / `.xml`
- Original backend log and refetched root log remain available.

The first batch covered the complete ten relevant backend test files plus the three failing root nodes. Existing Skill desktop/SDK/approval/managed-policy cases were included. Validation ran on the current Windows Python environment; a new Linux CI run is still required and belongs to Root's new-head push. No skipped or previous failed case is counted as passing without a later actual result.
