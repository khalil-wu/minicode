Official OpenAI Codex prompt resources copied from https://github.com/openai/codex.

Source revision: `822e58cc3d666166c7446c5b1ea2e52f5d09594c` (2026-10-06).
Checkout: `.tmp/codex-src`, branch `codex/upstream-20261006`, tracking `origin/main`.
Previous checkout revision `588b781ab4924ce7352488394028e63d74cf807f` is retained on its original `main` branch.

Source: `codex-rs/models-manager/prompt.md` and `models.json`.
`multi-agent-mode.json` preserves the bundled mode strings from `codex-rs/prompts/src/model_messages/multi_agent.rs` and any catalog mode overrides. Ultra selection follows `ModelInfo::resolve_reasoning_effort` and the source's proactive mode selection; it is retained locally while the ordinary inference request uses the model-owned wire effort.
`prompt.md` is copied byte for byte. Catalog instruction templates are preserved verbatim after JSON decoding.
This revision supplies literal templates; the old `instructions_variables` personality fields are no longer present.

Selection follows `codex-rs/prompts/src/model_instructions.rs::render_model_instructions`; unknown-model fallback and explicit personality disabling follow `codex-rs/models-manager/src/model_info.rs`.

Copyright OpenAI. Licensed under the Apache License, Version 2.0; see LICENSE.txt.

`realtime-backend.md` preserves
`codex-rs/prompts/templates/realtime/backend_prompt.md` byte for byte at the same
revision. Realtime startup/end contextual fragments follow the public
`codex-rs/core/src/context/realtime_*instructions.rs` consumers; explicit
configuration, a request prompt and an explicit null retain Source precedence.

`collaboration-plan.md` and `collaboration-default.md` preserve the source files
`codex-rs/collaboration-mode-templates/templates/plan.md` and `default.md` byte for byte.
Their developer-role injection follows
`codex-rs/core/src/context/world_state/collaboration_mode.rs`.
Rendering maps only the official `request_user_input` name to MiniCode's existing
`ask_user`, and states its actual single `question` / string `options` schema.
The permission-owned legacy plan-file workflow remains independent.

`desktop-review-guidelines.md` preserves the decoded `M` prompt from installed
Codex desktop `26.1007.2314.0`, `webview/assets/code-review-aae677399d79.js`
(asset SHA-256 `bee20a5cfea1d5fa4f628cfeb6fcad9b74d50ae47604d4130a591a832ea80119`).
It belongs to the ordinary Markdown review turn path, independently of the
Rust `ReviewTask` JSON rubric. See the source audit under `docs/validation/`.
