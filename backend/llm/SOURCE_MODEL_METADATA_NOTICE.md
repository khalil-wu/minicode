# Source model admission metadata

`source-model-admission-metadata.json` contains exact Guardian, node REPL,
reviewer override, multi-agent version/reasoning effort, model-specialty,
Responses Lite and cache-preserving reasoning-effort update fields from OpenAI Codex
`codex-rs/models-manager/models.json` at commit
`970111f6cb62cc02fe36a82ea7d71b37a2889c97`. Absent optional values and boolean
defaults follow `ModelInfo`'s serde defaults. Unknown model IDs receive no
catalog provenance; model-name families and caller-provided metadata-source
fields do not establish it.

Source: <https://github.com/openai/codex>. These catalog fields and translated
Source contracts are distributed under Apache License 2.0; the included license
is at `../agent/guardian/resources/LICENSE`.
