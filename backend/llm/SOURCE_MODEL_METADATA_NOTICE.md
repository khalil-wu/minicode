# Source model admission metadata

`source-model-admission-metadata.json` contains exact Guardian, node REPL,
reviewer override and model-specialty fields from OpenAI Codex
`codex-rs/models-manager/models.json` at commit
`822e58cc3d666166c7446c5b1ea2e52f5d09594c`. Absent optional values and boolean
defaults follow `ModelInfo`'s serde defaults. Unknown model IDs receive no
catalog provenance; model-name families and caller-provided metadata-source
fields do not establish it.

Source: <https://github.com/openai/codex>. These catalog fields and translated
Source contracts are distributed under Apache License 2.0; the included license
is at `../agent/guardian/resources/LICENSE`.
