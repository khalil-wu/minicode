# Project configuration, import flow and real long-task validation — 2026-10-22

Reference: installed Codex Desktop 26.1016.6573.0 and `.tmp/codex-src` at `822e58cc3d666166c7446c5b1ea2e52f5d09594c`. This continues the [UI/projection record](2026-10-22-ui-projection.md). No local GUI automation, renderer reload or user-profile migration was used. The user's open renderer, dirty editor buffers, drafts and older hashed assets were preserved.

## Closed behavior chains

| Trigger | Source contract and implemented behavior |
| --- | --- |
| Configuration scope menu | Installed `configuration-panel::ir/cr` enumerates actual project layers before User/Admin. The public DTO now includes their opaque IDs, paths, workspace roots, disabled reasons and versions. The current project's actual layer is preferred; the first real layer is selected when that preferred layer is absent, as in Source. Raw values retain Source `dr/ir`'s nullish effective-value semantics. |
| Project field write/unset | Writes target the selected real TOML file, serialize under the existing mutation lock, preserve comments/unrelated fields, enforce requirements and disabled layers, and return applied/overridden receipts using actual layer order. Explicit null removes the authored field. No raw configuration or credentials are exposed. |
| Open an ancestor's project configuration | The registered preferred-file opener returns the real project file. Internal editing keeps the current workspace and existing dirty buffers/drafts. The backend session registers only that exact file for that requesting workspace; the existing file read/compare-write consumers carry that actual owner. No synthetic conversation, ancestor-workspace activation, directory-wide permission or agent-tool permission is created. |
| Repair invalid project TOML in the editor | File access uses the exact registered editor capability, rather than reparsing the configuration catalog before each save. The real editor can save a temporary invalid document and then repair it. Source/remote file URIs remain under their executor contract and are not treated as control-plane native paths. |
| Import selected items | Installed `e9l → t9l → i9l → u9l` uses the same progress/results dialog. MiniCode now opens it immediately, locks Escape/outside closure while running, and removes the extra Background/View imported intermediary. Only real successful file results are links; original errors, skipped and unsupported results remain distinct. |
| Observe or restore an import | The actual import operation owns one ID and its selected items, partial results and final/error snapshot. Status reads do not wait for a detection scan. The frontend resumes the real operation and retains the original workspace/detection target during a workspace switch. An admission failure without per-item receipts does not invent per-item failures or success. |
| Import provider visuals and overflow | Use installed Source `Y6l/t8l` SVGs, 44-pixel provider marks and Claude's `#da6a44` background. The Source conditional scroll-to-bottom action depends on actual overflow. Source lists item/path results; it does not require a recursive directory inventory. |

## Local verification

Evidence directory: `output/ui-flow-completion-20261022/` (local audit artifacts, not product instructions).

- TypeScript project check and Vite production build passed. Saved entry: `index-Dy-juS4R.js`. Bundle and UI-debt budgets, protocol sync, kernel boundaries and large-file checks passed; existing build/large-file warnings are retained in the logs.
- Desktop registered-target/open-target checks: 21 passed, none skipped.
- The first frontend batch passed six files and exposed three affected files (including 88 editor cases not executed because a cold Markdown import exceeded a hook deadline). After correcting actual fixtures and moving the real import outside that hook, the three affected files ran 113 cases, all passed. These overlapping runs are not summed into a project total.
- Backend checks exposed the required executor-path platform argument, an unbound test execution context, relative-path settings fixtures and Windows newline translation in a fixture. The real path/URI contract was repaired; fixtures use actual bindings, absolute paths and explicit LF bytes. The affected follow-up ran 26 passed, four skipped and one newline assertion failure; its corrected exact-file case then ran one passed and one skipped. Local optional `asyncssh` cases remain skipped and require CI with the declared dependencies. An initial invocation with the bundled Python lacked `pytest-asyncio` and failed collection; only subsequent Python 3.12 results count.

## Real model long-task acceptance

`scripts/audit_long_task_session.py` used the configured desktop model (`gpt-6.1-sol`, Responses) with an isolated backend state/workspace. No GUI or user conversation was used. Receipt: `output/ui-flow-completion-20261022/actual-long-task/result.json`.

- Passed in 366.359 seconds, with the production backend source unchanged throughout and clean backend exit code 0.
- The independent Decimal reconciliation oracle ran 100.969 seconds, 100 batches and 60,200 checked rows. The original main PID/command incarnation was observed through the actual execution lifecycle.
- The original child process ran 90.344 seconds and checked 27,000 rows. It survived the child's initial final answer; a real follow-up resumed the same child through mailbox epochs 1 and 2 and polled the original process handle.
- A/B conversation switching, queued A follow-up, 36-second WebSocket disconnection, canonical restore and cold durable transcripts passed. While offline, main progress advanced from batch 26 to 63. A real B-model round-trip read `B_NOTE.txt` and `progress.jsonl`, independently of A's work.
- Source V2 spawn/follow-up and actual board create/post/read transactions passed with native namespace `agents`.

The configured endpoint rejects Source's default `collaboration` board namespace with HTTP 400: `Function 'collaboration.create_channel' is not allowed in reserved namespace 'collaboration'`. This failure is retained in `namespace-preflight-current/namespace-preflight.json`. Source explicitly supports `tool_namespace="agents"` (`core/src/config/config_tests.rs`); its real preflight passed and was the explicit configuration for this acceptance run. Production's default was not silently changed. Default-namespace board interoperability is not claimed.

## Committed-build acceptance

Pending the new commit's full CI and actual capture inspection. The existing `desktop-ui` job now exercises real project writes/unsets, trust-revoked disabled state, ancestor editing/invalid-TOML repair with retained drafts/buffers, actual import copies, pending response delivery, a real destination write failure, Theme keyboard submenu and all 15 settings-page baselines at Chromium device scales 1, 1.25 and 1.5.

Only model replies and delivery timing of one already completed import HTTP response are controlled. The import response body, files, failure, configuration state and editor I/O remain real. No transcript/store snapshots are injected. Captures exclude OS window decorations; Chromium device scales do not certify Windows-native OS scaling or every menu's visual state.
