# Model-owned token-budget activation and current-model resolution

Reference: `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`, `core/src/config/token_budget_startup.rs`, `session/token_budget.rs` and `session/context_window.rs`.

The catalog/auth producer is documented separately in `2026-10-14-source-token-budget-catalog-auth.md`. This batch consumes those actual records; it does not infer defaults or subscription eligibility from a model/provider name.

`token_budget_activation.py` implements the Source startup snapshot and the distinction between configured preferences, activated runtime seed and per-model resolved prompt values. `AppConfig.token_budget_startup_config` keeps the original configured flag/seed. `context_window_config` is a runtime seed separate from the existing UI total/reserve budget. Neither field creates a settings control or writes user configuration.

- Model-owned `enabled=true` activates only without an explicit token-budget configuration. Managed requirements can keep the feature off. The packaged Source model records remain disabled/omitted exactly as declared.
- An enable-only configuration can use current-model reminder, guidance and fallback defaults. Explicit prompt preferences win. The runtime seed keeps prompts unresolved, so selecting another model reads that model's original strings without freezing the first model's instructions into user configuration.
- Fresh startup restores original configured preferences before applying the starting model. A history fork can retain the parent's effective activation and original snapshot. Model switching preserves the existing context-window ID and transcript while changing its current-model guidance/tools.
- Experimental admission follows Source's full conjunction: context-management feature, explicit model support, eligible ChatGPT auth/Plus, Pro, ProLite or ProMax plan, supported Codex backend routes and required OpenAI auth, with no explicit environment key, experimental bearer, auth override or AWS configuration. Unknown/ordinary OAuth and API-key credentials do not acquire ChatGPT eligibility.
- Ineligible feature/model capabilities do not consume an authentication getter. This matches Source's short-circuit order and removed an actual unrelated startup dependency found in the auth-recovery integration.
- Invalid model-owned prompt defaults follow Source's explicit warning-and-retain-configured rule. Invalid user preferences still report configuration errors. Positive integer and string contracts reject misleading boolean/other-type values.
- Registry creation, bootstrap, current model binding, per-iteration tool reconciliation and manual compaction now consume the same resolver. `new_context` remains direct-model-only; `get_context_remaining` keeps its structured CodeMode result. The process/debug visibility and no-summary window lifecycle are unchanged.
- Startup resolution does not replace the issuing model-selection snapshot on `RunContext`. A real auth-refresh race exposed why its identity must remain stable while a host publishes another selection. Context keeps its effective scope configuration separately, so authorization/selection identity and current-model resolution both remain correct.

Verification includes a real QueryEngine startup with an initially empty registry: an explicitly declared model exposes the two context tools, injects its original guidance, executes `new_context`, archives the window and samples the real follow-up. Other cases cover the exact auth gate matrix, managed/explicit priorities, fresh-child/history-fork resolution, registered capability cache stability, preserved transcript/window on binding, declared SDK metadata and absence of activation from a model name. Existing actual WS/native manual compaction, model-auth refresh, Source roles and catalog tests ran together.

Evidence under `output/playwright/codex-control-audit-20261013/`:

| Batch | Actual result | Finding |
| --- | --- | --- |
| model-token-budget-run01 | exit 4, zero tests executed | Validation command named a nonexistent upstream test file; not a passed check |
| model-token-budget-run02 | 83 passed, 31 failed | New fixtures omitted required `api_key`; false-capability auth paths unnecessarily consumed a getter |
| model-token-budget-run03 | 44 passed, 1 failed | New activation cases passed; real auth race detected premature issuing-snapshot replacement |
| model-token-budget-run04 | 45 passed, zero failed/skipped | New cases and complete auth-refresh scope passed after fixing the snapshot identity |

Latest results keyed by testcase classname/name are **114 passed, zero unresolved failures, zero skipped**. `model-token-budget-latest-results.json` identifies each case's final batch. Ruff passed for the new resolver/test module. These counts are not added to the upstream catalog or compact-hook batches.

**Unimplemented boundary:** native Notes/History encrypted backend tools and their private image/encrypted tool-output contract are not connected by this work. The eligible experimental gate and retained `use_history_notes_extension` preference are implemented as Source state; this does not claim Notes tools are available or that a real eligible ChatGPT subscription/backend was exercised. Validation auth/declared-model values are controlled fixtures, not a substituted Notes/MCP service. No user account, live Codex window, model endpoint or normal workspace draft was modified for these checks.
