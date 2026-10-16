# Final frontend and desktop validation — 2026-10-16

The root agent delegated the final frontend/desktop checks and later the two formal Electron entries. The runtime agent confirmed frontend/desktop source, scripts, tests and native candidates were frozen before the initial run. The root agent owned the subsequent Connections visual repairs and package manifest changes; those changes and the precise affected reruns are recorded here. Project Git state and ordinary MiniCode profiles were not modified. Git setup/configuration used only the explicitly requested, owned temporary test repository.

All primary evidence uses `output/ci-repair-20261015/frontend-desktop-final-*`. The delivery manifest is `frontend-desktop-final-manifest.json` in that directory. It uses the latest complete result for each test file, so reruns do not inflate the counts.

## Formal entries and results

| Entry | Final result | Evidence |
| --- | --- | --- |
| Frontend `npm run test` (nested npm-exec forwarding, all files, `--maxWorkers=2`) | **357 files, 4297 unique tests passed**, zero failed/skipped | `frontend-desktop-final-vitest-results.json` plus latest affected-file replacement from `frontend-desktop-final-vitest-repair-results.json` |
| Frontend `npm run build` (`tsc -b && vite build`) | Passed; final R3 includes root's last Connections CSS correction | `frontend-desktop-final-build-r3.log` and its status JSON |
| Frontend `npm run check:bundle-budget` | Passed against final R3 dist | `frontend-desktop-final-bundle-budget-r3.log` |
| Frontend `npm run check:ui-debt` | Passed against final R3 dist | `frontend-desktop-final-ui-debt-r3.log` |
| Desktop `npm run test:unit` | **170 passed**, zero failed/skipped/cancelled | `frontend-desktop-final-desktop-unit.log` |
| Desktop `npm run test:e2e` (six official Electron files) | **6 unique tests passed**, zero final failures/skips | Full initial log plus two-file narrow repair log |
| Frontend `npm run test:e2e:electron` (official default multi-agent spec) | **1 passed** | `frontend-desktop-final-frontend-electron.log` |

The latest build time, index/entry SHA256 and full dist file manifest are in `frontend-desktop-final-dist-identity-r3.json`. Earlier build identities are retained separately; root was given each identity before using it for actual desktop QA.

## Initial frontend failures and narrow fixture repairs

The complete initial Vitest run finished with 4284 passed / 13 failed across four files. Every failure and stack is retained in `frontend-desktop-final-failures.json`, the full JSON result and the full log.

- `EditorPanel.test.tsx`: eight save assertions omitted the new explicit fifth workspace owner argument; the restored read assertion omitted its explicit optional signal/owner arguments. These assertions now retain the exact API contract, including `undefined` where no owner exists. The actual content, dirty-buffer, rename, hidden-workspace and save ordering assertions remain.
- `FileTree.scope.test.tsx`: the native-only large-directory case accidentally had an active conversation owner. Owned workspaces correctly route through executor HTTP rather than the controller filesystem. That one native scenario now explicitly has no conversation owner and still verifies the original absolute path and cancellation after a workspace switch.
- `editorLanguageClient.test.ts`: two cases expected controller-native Monaco paths. The wire now carries executor-relative document paths. The exact relative path, unsaved contents, language, document identity, lifecycle and hidden-buffer assertions are retained.
- `contextPayload.test.ts`: the fetch URL is now a `URL` object with the same exact address. The test now expects that type and still checks the exact URL and headers.

No production fallback was introduced to satisfy these old assertions. The four files plus `ConnectionsTab.test.tsx` passed together: **117/117**, including all four Connections cases. The untouched complete-file results remain from the full run.

The root agent's actual Connections QA then produced a final token-color CSS correction. Only the final build and both budgets were repeated for that correction, as requested by root; the 117 behavioral tests were not repeated without a behavior change.

## Real desktop and frontend Electron ownership

Before launching, the six desktop tests and frontend launcher were audited for independent profiles, state roots, dynamic loopback ports and cleanup. They spawn and stop only their owned child objects. Temporary-profile removal is bounded to each created directory. The ordinary MiniCode instance and its unsaved drafts were not targeted.

The desktop official entry initially passed four and failed two. Both original failures are retained:

1. `window-unload-electron.test.js` executes the actual main-process shutdown code in a fixture VM. The newly introduced `remoteDesktop` binding was absent from that VM. The fixture now supplies the real `close()` shape and verifies it closes before backend shutdown. Production main-process behavior was not weakened.
2. `external-backend-electron.test.js` ran a real FastAPI instance, production Electron main/preload and an isolated state root. Before the model was called, the internal Git prompt snapshot tried to acquire a Windows command sandbox account that did not exist in that isolated state. This was a real production call-chain issue; the test was not changed to bypass, skip or provision accounts.

After the fixes, only these two failed files were rerun. Both passed. The other four initially passing Electron tests were not repeated. Evidence is `frontend-desktop-final-desktop-e2e-repair.log`.

The external test then proved native WS admission, a model-visible browser result, a real Pyright process, rejection of a foreign window proof and release of that LSP client on desktop close. The unload test proved runtime reload and Ctrl+R committed pending 2 MiB drafts, normal Quit committed before backend stop, and a cold process recovered the committed attachment and authored text.

The separate frontend Electron entry passed its actual workbench flow: read-only child transcripts, lazy results, process restart and persisted child history recovery. Its owned apps and Vite server were closed by the formal harness.

## Source-aligned internal Git metadata repair

Source checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c` was inspected:

- `file-system/src/lib.rs` selects sandboxed reads by actual full-disk read permission; the existing local filesystem and project-instruction behavior already matched and was not relaxed.
- `git-utils/src/info.rs:371` runs the fixed metadata queries with `FsmonitorOverride::Disabled`; its lower-level runner at 412–429 uses `safe.bareRepository=explicit`, a disabled hooks path (`NUL` on Windows, `/dev/null` elsewhere) and `core.fsmonitor=false`.
- `git-utils/src/git_process.rs` uses the original host process/JobObject rather than the user command sandbox. Source's metadata timeout is 5 seconds.

`backend/agent/environment_prompt.py::build_environment_git_status` now gives only its private, fixed metadata queries a one-call internal read admission when the already captured scope has full-disk read authority. It keeps the original attachment, cwd, shell environment owner and caller permission profile. A genuine denied-read scope keeps its original enforcing scope. Ordinary Git tools, Git writes, user commands and whole-session permission modes are unchanged.

The fixed commands use the Source flags above, preserve no-optional-locks behavior and run with a five-second timeout. No new catch, retry, fallback decision or system sandbox initialization was added.

`backend/tests/test_environment_git_metadata.py` and the existing local ContextBuilder chain passed **4/4**, zero skipped. These tests exercise a real local executor under confirm/managed caller authority, a real Git repository and malicious fsmonitor/hook configuration, and verify the denied-read admission and timeout contract. The fsmonitor positive control actually writes its own marker before the production query proves the helper no longer executes.

The initial non-repository fixture exposed an environmental assumption: the Windows user home itself is a Git repository, so ordinary pytest temporary paths had a repository ancestor. The direct Git probe and actual cwd are preserved in `frontend-desktop-final-git-metadata-diagnostic.xml`. The fixture now selects an independent owned Public temporary workspace through the real environment owner, verifies that a direct Git probe is truly non-repository, and closes its original leases before removing only that temporary directory. The final batch is `frontend-desktop-final-git-metadata-r2.xml`.

The root-test missing-workspace assertion was separately aligned with the actual explicit 422 detail (`workspace_root is required`) and passed in `root-tests-final-workspace-batch07-r2.xml`. Production API behavior was not changed for that fixture.

## Preserved diagnostics

The full logs retain node-pty `AttachConsole failed` stderr from otherwise passing actual PTY cases, Electron console API deprecations, the Browserslist data-age notice, bundler timing/chunk/dynamic-import warnings, expected malformed-frame test logs, and existing React key warnings. None was silently suppressed or counted as a skipped test. Original failures and each precise repair result remain available alongside the final latest-result manifest.

## Final actual R3 screenshot and owned QA shutdown

The final independent visual check used the existing Playwright CLI session `remote-connections-current-20261016`, attached only to the owned Electron CDP port 57044. It reloaded final R3 and navigated through the real Settings and help button, Settings menu item and Connections category.

`output/playwright/remote-connections-20261016/12-r3-final-empty-primary.png` was captured and viewed alongside the Source `reference-connections.png`. The requested empty rounded box, Laptop / ellipsis / Server icons and dark Add button were present. Computed style retained the existing Anthropic Sans/Noto Sans SC stack, 14px / 600 / 16.8px button typography, a 1px rounded border, and the primary token colors. The loaded document script was the actual final `assets/index-DdoZ091a.js`. Different viewport dimensions are not claimed to be pixel-identical.

The actual titlebar Close button then closed the owned Electron PID 25908. Its backend PID 19676 completed ordinary shutdown and the desktop log records `exited (code=0)`. The CLI's automatic post-click page lookup reported a missing page URL after closure; that original CLI output is retained and the native shutdown verdict comes from the independent process/log receipt. The test profile was preserved.

Only after verifying the complete original command line for Python HTTP server PID 23148 at port 57042 was that owned static server stopped by its specific PID. No process-name kill, ordinary user profile or ordinary MiniCode window was used. The final actual QA manifest and cleanup evidence are `output/playwright/remote-connections-20261016/actual-QA-manifest.json` and its listed files. No production code changed during this visual/cleanup step.
