# Runtime Skill roots and durable queue alignment

Reference: Codex source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` and desktop `26.1013.4894.0`.

## Completed behavior

- `skills/extraRoots/set` replaces process runtime roots with shared user ownership. Bootstrap, connected sessions, loader cache and catalog discovery use the same owner. An active turn retains its captured catalog; subsequent discovery sees the replacement.
- A real recursive filesystem observer publishes `skills/changed`. Initially missing roots are watched through their existing ancestor. The Source 10-second throttle does not restart on every event. Root replacement and shutdown stop obsolete notifications and observers.
- The frontend consumes invalidation for the active conversation and requests its Skill, command and runtime capability catalogs. A late response from a different conversation cannot overwrite the current catalog.
- Durable queue add, input replacement and exact-set reorder use the existing queue file/owner lock. Claimed inputs cannot be edited or restored. Cross-window input changes replace stale hot command text and survive a cold restart. Queue addition alone does not start a model turn.
- The existing queued-message menu supports Edit. Input moves into the normal composer; save replaces the queued input and restores its position, while Escape restores its original identity and order. Prior drafts, file bytes, image URLs, quotations, Skill/agent mentions and Diff comments survive the workflow, including hidden conversations.
- The existing numbered queue row supports pointer and keyboard reorder without an extra drag button.
- Queue deletion and editing participate in application Undo/Redo. Source expiry is retained: deletion 60 seconds, editing 30 minutes. New edits invalidate the corresponding redo branch, submission invalidates the edit history, and rejected or already claimed identities cannot be revived.
- Focused ProseMirror text history has priority over queue actions. Other text inputs and Monaco retain native editing. Existing native Edit items, shortcuts and command menu actions share queue history; browser text undo remains on the presented document and original window.

Source queue methods are `thread/queue/add`, `thread/queue/update` and `thread/queue/reorder`. MiniCode exposes the corresponding behavior through its existing command protocol as `user_message.queue.add`, `user_message.queue.update` and `user_message.queue.reorder`; this is not a claim of literal `UserInput[]` wire compatibility.

## Source evidence

- `codex-rs/app-server/src/request_processors/catalog_processor.rs`: `skills_extra_roots_set`.
- `codex-rs/app-server/src/skills_watcher.rs`: runtime registration, recursive registration and `WATCHER_THROTTLE_INTERVAL`.
- `codex-rs/app-server/src/request_processors/thread_queue_processor.rs`: `add`, `update`, `delete` and `reorder`.
- `codex-rs/app-server/tests/suite/v2/skills_list.rs`: process-wide runtime-root replacement.
- `codex-rs/app-server/tests/suite/v2/thread_queue.rs`: durable queue contracts.
- Desktop extracted `app-primary` queue editor `hDe` imports `jFr` (`uh` in `app-initial`); `Rwt=6e4` and `nlt=18e5` define the two expiry intervals. `Ddo`/`mHi` define editor-history priority and application fallback. `undoAppAction`/`redoAppAction` are actual command-menu actions.

## Verification

Receipts are under `output/full-parity-20261021/`.

| Scope | Current pass evidence | Receipt |
| --- | --- | --- |
| Frontend queue, history, draft persistence and catalog consumers | 122 distinct cases | `catalog-queue-verification.json` |
| Backend roots, watcher, durable queue and identity boundaries | 38 distinct cases | `catalog-queue-verification.json` |
| Native Edit actions and browser document ownership | 2 cases; no skips | `queue-undo-native.log` |
| Final TypeScript check | Exit 0, session 41856 | `catalog-queue-final-typecheck.log` |
| Final protocol check | Exit 0 | `catalog-queue-final-protocol.log` |
| Hour-audit production V2 provenance | 5 passed; 9 unrelated cases deselected | `hour-production-v2-catalog.xml` |

The 122/38 totals deduplicate the latest result of each case across incremental runs. They are not one fresh consolidated run. Earlier failed and filtered receipts remain available. The repaired fixture cases involved a missing real streaming assistant, an obsolete fire-and-forget cancellation mock, and a Shift+Ctrl+Z event whose lowercase `key` did not match a physical keyboard. The keyboard fixture was corrected without adding a production fallback.

The hour audit now requires the original production `ModelRuntime` to advertise both Responses API and `multi_agent_version=v2`. It copies only the actual nonsecret version and reasoning-effort fields into the isolated audit catalog and records public model provenance plus its hash. Exact endpoint/model/wire namespace proof remains mandatory. Unknown models, V1 declarations, a configured V1 override and a non-Responses production catalog are rejected before creating the isolated catalog or changing the audit profile.

The previously completed real native-namespace round trip is retained in `namespace-provider-preflight-source/namespace-preflight.json`; the provenance change did not rerun the provider probe. This workstream did not perform physical UI validation, commit/push, CI or the one-hour acceptance. Those belong to the coordinated final pass.

## Final application integration repair

The first real application acceptance attempt found that `main.websocket_endpoint` passed `runtime_skill_roots`, but `WebSocketManager.connect` had not accepted or forwarded it to `WebSocketSession`. Root added that missing link rather than removing the bootstrap-owned roots. The failure is retained in `hour-live/` and was not counted as a long-task pass.

`root-bootstrap-roots-manager-final.xml` records 3 passed cases: a new real application lifespan/entrypoint test verifies two sessions consuming the same bootstrap roots, replacement and reconnect identity; the two existing unconfigured/selected-model protocol variants verify ordinary startup/restore/catalog/file operations without binding a model. These are an integration delta, not a rerun of the earlier 38-case inventory.

The parent-owned process audit additionally verifies original OS PID/create-time, command owner and handle, continuous offline samples, elapsed monotonic duration, successful exit and the original reviewer's follow-up poll. `hour-original-process-audit.xml` records 24 passed cases, including required production-model provenance and child-oracle regressions. Real hour acceptance remains separate.
