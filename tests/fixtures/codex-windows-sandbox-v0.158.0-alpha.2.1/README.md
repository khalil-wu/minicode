# Pinned Windows sandbox patch fixture

Source: OpenAI Codex, tag `rust-v0.158.0-alpha.2.1`, subtree
`codex-rs/windows-sandbox-rs/src`.

Upstream archive: https://github.com/openai/codex/archive/refs/tags/rust-v0.158.0-alpha.2.1.zip

The 32 Rust files are unchanged upstream inputs required by MiniCode's
owner-namespace patch planner and its offline contract tests. They are not
generated patched output or a replacement sandbox implementation. The original
upstream license is included in `LICENSE`.

The fixture version matches `UPSTREAM_VERSION` in
`desktop/scripts/patch-windows-sandbox-source.py`. When updating that version,
refresh these exact upstream inputs and rerun the anchored patch tests.
