# Native execution policy engine

This standalone workspace vendors `execpolicy`, `utils/absolute-path`, and the
selected shell parser / command safety modules from
OpenAI Codex commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`. Their source files
are unchanged. `source-manifest.json` records each original path and SHA256;
the original Apache-2.0 license and NOTICE accompany the code. The bridge also
retains the Source amendment algorithms and banned suggestion list, adapting
only the protocol value type and function visibility.

The parser is the actual Source `PolicyParser`, with Starlark 0.14.2's
`AstModule` / `Evaluator`. `Cargo.lock` fixes the native dependency graph and
Rust 1.95.0 matches the pinned Source toolchain. The executable needs no Codex
checkout or Rust installation at runtime.

The JSON stdin API supports identity, policy inspection, native matching of
already parsed command argv, Source model-policy filtering / environment
overlay ordering, amendment proposals, and the original locked append writer.
Policy parse errors retain their original file and one-based source location.
The low-level `evaluate` operation accepts explicit unmatched decisions for
engine verification. The `command_requirement` operation instead uses the
original bash / PowerShell tree-sitter parsers and Source dangerous-command
classification, unmatched decision, shell executable identity, granular prompt
rejection and amendment algorithms. Dynamic shell syntax remains opaque when
Source cannot lower it. Every parsed segment must have an explicit Allow rule
before the result bypasses the sandbox. Ordinary safe unmatched commands keep
Source's Skip result.

`command_requirement` requires host-owned, already-resolved permission facts:
filesystem kind, full-disk-write and managed-profile status; the actual Windows
sandbox level; configured shell; requested sandbox override; and captured
approval policy. These transport values replace the large Source protocol and
core ownership types. Their producer must bind them to the admitted model and
the exact final executable request. This helper alone does **not** establish
backend approval authority, persistent approval acceptance, prompt injection,
or the complete agent producer chain. Those consumers are separate work.

Policy snapshots include the original Source `format_allow_prefixes` output,
including token-count / length / lexicographic ordering, JSON token escaping,
and Source's count and character truncation behavior. The filtered base policy
is returned separately from environment overlays for permission instructions.

The original config `requirements_exec_policy.rs` producer also compiles the
managed `[rules]` object into its native restrictive policy. Its original
checks reject Allow rules, missing decisions and invalid pattern tokens. A
host-owned `approved_prefixes` list represents successful amendments to the
loaded policy without re-reading unrelated external file edits. Those updates
use the original native `Policy::add_prefix_rule` and Source's already-allowed
check before model filtering. The host must append this list only after the
native durable writer succeeds.

Build/test using the actual pinned toolchain, for example on this Windows host:

```powershell
$env:Path = 'C:\Users\ago\.rustup\toolchains\1.95.0-x86_64-pc-windows-msvc\bin;' + $env:Path
cargo fetch --locked
cargo test --locked --offline --workspace --no-fail-fast -j 2
```

Stage a release runtime with the preparation script. Supply an isolated output
while a production desktop batch is frozen:

```powershell
python desktop/scripts/prepare-execpolicy-runtime.py `
  --cargo C:/Users/ago/.rustup/toolchains/1.95.0-x86_64-pc-windows-msvc/bin/cargo.exe `
  --output-directory output/native-execpolicy-runtime-stage
```

The script verifies original Source hashes, uses the pinned compiler and locked
offline dependencies, compiles the release executable, executes its actual
identity operation, and stages binary hashes, Source provenance, upstream
LICENSE / NOTICE and dependency license text. Runtime resource packaging and
the complete backend consumer chain are separate follow-up steps; a successful
helper test must not be reported as their completion. The Source PowerShell
subprocess parser is a test-only oracle; production classification never
executes the submitted shell script.
