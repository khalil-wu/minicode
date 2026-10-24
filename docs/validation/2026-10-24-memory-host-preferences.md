# Host memory preferences

The installed Personalization consumer `personalization-settings-a40d408010a2.js` writes the memory feature and `memories.generate_memories` / `use_memories` together. Its tool-assisted switch writes the inverse `disable_on_external_context`. The official checkout documents generation as a default for newly created threads (`config/src/types.rs`, `core/src/session/session.rs`); the feature gate controls startup (`memories/write/src/start.rs`).

MiniCode now addresses these settings and instructions on the selected connected desktop host. Host selection uses a narrow main-process API and retains the window's runtime, conversation, working directory and drafts. Only connected hosts are listed; preference requests never connect or authenticate a host. Other hosts' runtime tokens remain in main.

The memory feature defaults to false. Generation and use default to true; external-context disabling defaults to false. Existing thread modes and stored memories are untouched by preference writes. Each new run captures the policy. Use controls new prompt injection and builtin memory capabilities; the feature controls startup. Generation changes the default for newly created threads. External-context policy governs new pollution marks, while previously persisted pollution remains recorded.

## Configuration precedence and migration

`features` is the internal configuration namespace. The pre-existing `feature_flags` file alias is normalized inside each parsed layer, before normal precedence merging. Canonical entries win a same-layer collision. Higher project / policy layers override lower layers regardless of their input spelling. Each managed drop-in normalizes before its ordered file merge. Requirements write the same canonical namespace and origins identify the actual winning layer.

Runtime consumers read canonical `features`; there is no union of the two tables after layers have merged. Public WebSocket/status projections named `feature_flags` retain their existing wire contract.

Saving an explicitly edited feature writes `features.<key>` and removes only that key from the old alias table. Unedited feature values and their file sources remain in place. No bulk user-file migration runs on load. The Personalization master writes that one feature plus its two memory booleans in a single user-file mutation.

## Verification handoff

No GUI, model request, build or test execution was performed for this implementation by its author. Fixtures cover API persistence, old task modes / memory / instructions, generation defaults, live run snapshots, feature/use/external combinations, same-layer conflicts, project / managed precedence, managed drop-ins, connected-host forwarding and instruction drafts. Tests and visual verification remain for the unified root run.
