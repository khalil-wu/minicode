# Model-owned token-budget catalog and auth producers

Reference: `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

This upstream scope follows `protocol/src/openai_models.rs::ModelMessages/ModelTokenBudgetConfig`, `protocol/src/account.rs::PlanType`, `core/src/session/token_budget.rs`, and `core/src/config/token_budget_startup.rs`. The ContextWindow lifecycle, startup/default resolver, compact helper, hooks, and real MCP extension binding belong to the separate context-window integration scope.

`ModelDefinition.model_messages` now exposes the complete model-owned Source dictionary retained in its existing `extra` record. `supports_experimental_context` is an explicit boolean capability. Settings metadata, native base catalogs, configured models, model overrides, OAuth catalog modifiers, prepared `ProviderAdapterSpec.model`, and OpenAI adapter `LLMSettings` preserve these fields. Source catalog objects detach their mutable metadata at admission, and extension/export dictionaries detach it again. Switching an unregistered direct SDK model clears the previous model's messages and experimental capability.

`source-context-model-capabilities.json` preserves the exact token-budget prompt/default records for eight models from the reference catalog. Their activation flags are false or omitted/default-false in this revision; they are not silently enabled. Unknown slugs receive no fabricated defaults. Runtime code reads this packaged data rather than the development `.tmp/codex-src` checkout. No Notes tool, prompt rewrite or substitute MCP implementation was added.

`ModelRuntime.token_budget_auth_context` returns an immutable, secret-free `TokenBudgetAuthContext`: declared `auth_mode`, `account_plan_type`, and the actual provider restrictions required by the Source experimental activation gate. ChatGPT mode and plan come only from canonical credential facts produced by the owning authenticator. Provider name, base URL and an ordinary OAuth credential do not imply a ChatGPT subscription. The Source eligible plan wire values are `plus`, `pro`, `prolite`, and `promax`. This does not claim to add a missing native ChatGPT login importer or invent its account data.

`AppConfig.token_budget_startup_config` and `context_window_config` are runtime fields, distinct from MiniCode's existing UI total/reserve `TokenBudget`. Their type-only imports avoid config/harness cycles. The context-window owner implements and consumes their seed, snapshot, fresh-child and history-fork semantics; these fields are not written to user settings and add no UI control.

## Validation

`output/source-token-catalog-20261014-run01.xml` records an invalid test path at collection. No cases executed and none are counted as passing. The corrected concentrated batch, `run02.xml`, passed **164 cases, zero failed/errors/skipped**, including real config/model/adapter producers and OAuth login → credential publication → catalog modifier → captured model/auth facts.

A wheel was built with `pip wheel --no-deps --no-build-isolation`. Its ZIP entries were checked to contain valid JSON for both the new context catalog and the existing model-request capability catalog. `pyproject.toml` now declares `backend.llm` JSON package data so these runtime imports also work from the distribution. The built verification artifact is `output/source-token-catalog-wheel/minicode-0.2.0-py3-none-any.whl`.

The final consumer integration still needs the parent context-window batch to verify model switches, auth startup gates, managed feature disable, explicit settings precedence and child/fork lifecycle against these actual producer fields.
