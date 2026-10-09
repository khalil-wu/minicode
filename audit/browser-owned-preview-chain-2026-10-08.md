# Owned preview browser admission review — 2026-10-08

## Reported failure and scope

The isolated task `conv_muzi7o1i_hdka36`, in `output/playwright/projection-chain-20261008/qa-profile-1791461803385`, reached browser navigation for its own `index.html`. Backend admission accepted the active preview, but the desktop host showed `Open local browser target?` and the operation eventually timed out. The matching user screenshots are `codex-clipboard-0ee29ddd-7a0d-4570-ac24-004890a30bd7.png` and `codex-clipboard-7bf499b0-b6f3-439f-8e86-267c165e847a.png`.

This review covers backend browser navigation, the authenticated desktop bridge, native navigation, redirects, cancellation, and token boundaries. It does not claim that the user's running application or a newly packaged application has been visually validated. No user-owned window was controlled during this review.

## Complete call path and correction

1. `BrowserControlTool.execute` resolves an existing workspace HTML file through `start_static_preview`. The resulting process belongs to its exact session, conversation, and workspace.
2. `_navigation_policy_error` accepts the active owned preview. Previously this authorization stopped at the backend: `embedded-browser-manager` had no production binding for its optional `isOwnedPreviewUrl` dependency and independently asked for native approval.
3. `_owned_preview_navigation_authorization` now obtains authority from the live preview registry. It requires the current session, conversation, workspace, process ID, and origin; removed, exited, stopping, or cleanup-pending processes produce no authority. Runtime metadata alone does not attest a registered preview.
4. `_execute_embedded` replaces any caller-supplied authorization with runtime-generated data, normalizes the action, and binds it to the exact generated operation ID and URL.
5. `main.js` generates an independent embedded-browser token. The renderer API token cannot call the bridge as the backend. The embedded token reaches the managed backend environment, while shell, MCP, and helper subprocess environments remove it.
6. The bridge validates the operation, owner, preview ID, session ID, exact requested URL, and preview origin before passing authorization as a trusted execution option. Renderer/manual IPC passes only payload data and cannot supply that option.
7. The manager admits that exact owned-preview origin without a second native prompt. Another private origin still requires its own decision; redirects and connected-peer checks retain the same origin boundary. Manual private navigation retains its existing confirmation behavior.
8. Redirect confirmation previously escaped the original operation: a delayed answer could invoke `loadURL` after the original navigation failed or was cancelled. It now retains the original request and cancellation signal and checks that the same live request/tab still owns the navigation before applying approval. Initial confirmation also checks cancellation before applying a grant. `ERR_ABORTED` from the original `loadURL` remains a failure and is not converted into success.

## Codex source correspondence

- `.tmp/codex-src/codex-rs/core/src/agent/child_config.rs`, `apply_spawn_agent_runtime_overrides`: permission and approval state come from the live turn snapshot. The browser fix consumes runtime authority rather than interpreting model-supplied permission claims.
- `.tmp/codex-src/codex-rs/core/src/tools/network_approval.rs`: network approval retains typed call identity and cancellation state; abandoned approval is not equivalent to successful execution. MiniCode's delayed browser approval is now tied to its original operation lifetime.
- The public Codex checkout does not supply MiniCode's Electron embedded-browser implementation. These references establish the permission/lifecycle contract; they are not presented as evidence of an identical desktop implementation.

## Verification

Backend command:

```text
python -m pytest backend/tests/test_browser_owned_preview_admission.py backend/tests/test_browser_control_tool.py backend/tests/test_browser_navigate_workspace_html.py backend/tests/test_browser_operation_lifecycle.py backend/tests/test_web_browser_chain.py backend/tests/test_preview_start_owner_chain.py backend/tests/test_preview_service_menu_chain.py backend/tests/test_policy_source_boundary_chain.py -q -o addopts=''
```

Result: **101 passed in 10.65 seconds**, no skips or warnings in the final run. This includes a real static preview subprocess, HTTP 200 and expected HTML content, matching runtime registry authorization, and invalidation after stopping that process. The bridge HTTP transport is simulated in the backend payload test; it is not counted as a native browser screenshot test.

Desktop command:

```text
node --test browser-lifecycle-audit.test.js embedded-browser-bridge.test.js embedded-browser-manager.test.js security.test.js backend-sidecar.test.js
```

Result: **53 passed**, no failures or skips. Tests exercise the real HTTP bridge and manager with a controlled Electron view boundary: wrong renderer token, owner/operation/preview mismatches, same-origin redirects and connected peers, cross-origin private redirects, manual payload forgery, timeout/cancel before native confirmation, delayed redirect confirmation after cancellation or `ERR_ABORTED`, owner isolation, and view disposal.

`browser-lifecycle-audit.test.js` was missing from the existing `test:unit` script. It is now included so these lifetime regressions run in the desktop CI job. `git diff --check` passed for the files in this change.

An initial real-preview test fixture omitted the disabled sandbox snapshot carried by a full-access turn and correctly failed its sandbox launch. The fixture now supplies `SandboxPolicy.bypass()`. A subsequent test run started and immediately stopped the process and exposed Windows pipe-finalizer warnings; the final test waits for actual HTTP readiness/content before stopping and completed without warnings. Neither earlier run is counted as the final verification result.

## Remaining integration verification

The parent task must build this source and run the isolated native acceptance task. Verify `index.html` navigation returns real URL/DOM/screenshot evidence without the duplicate native confirmation, and retain any real browser/network failure in its expandable record. This document does not mark that packaging or native UI verification complete.

## Follow-up: native screenshot presentation

The next packaged task, `conv_muzlaspk_3ilt68` in `qa-profile-1791467006091`, proved the duplicate approval fixed: navigation, HTTP 200, and actual DOM text succeeded. Screenshot results at journal sequence 663 and 708 were empty. CDP inspection found the guest page viewport at `0 × 0`, even while the main renderer was `1440 × 940`.

An independent Electron probe reproduced the zero viewport. Setting a nonzero viewport corrected DOM layout, but an unpresented `WebContentsView` still had no compositor surface; native capture reported `Current display surface not available for capture`. CDP `Page.captureScreenshot` variants and a fixed delay used only for diagnosis did not establish that surface. A separate hidden `BrowserWindow` could paint, but adopting its already-owned WebContents into another view was rejected by Electron. No replacement-page screenshot or offscreen migration was added.

The production path now creates a real host-sized viewport **after attaching the native view**, before navigation, and keeps it hidden until the browser panel supplies its actual bounds. Activation alone cannot expose the initial full-window viewport over chat. Only trusted backend commands emit `presentation-requested`; the existing renderer channel carries the actual owner and target. Frontend handling opens/selects the current owner's actual browser tab, while background owners and a user's manual panel dismissal retain their existing control.

Screenshot execution waits for that same target's actual panel bounds and activation. It then waits for two `requestAnimationFrame` callbacks before asking the same WebContents for its real compositor image. There are no fixed sleeps, repeated-capture retries, replacement pages, or successful empty-image results. Cancellation or closing the tab releases pending screenshot callers. A genuinely unpresented background page remains an unfulfilled presentation request and is cancelled/timed out through its existing operation lifetime; this is not counted as a successful screenshot.

New real Electron regression `desktop/browser-capture.test.js` serves actual HTTP content, checks the initial host-sized viewport, cancels an unpresented capture, presents the same native target, and validates the returned PNG's dimensions, nonempty data, actual background pixel, target identity, and URL. Windows result: **1125 × 750, 22,678 PNG bytes; test passed**. The three directly affected desktop modules also passed **33 tests**. The native regression is included in `test:e2e` for CI. Full packaged frontend acceptance after this follow-up remains with the parent task.

Final native suite command (from `desktop/`):

```text
node --test electron-smoke.test.js editor-viewport.test.js browser-capture.test.js
```

Result: **3 passed**, no skips, in 1.90 seconds. The existing viewport test now activates its target before exercising the visible panel's resize/emulation/reset contract; new agent-created pages are deliberately hidden until that activation plus actual panel bounds. The screenshot test proves an initially hidden host can initialize the correct layout viewport and cancel an unpresented request; it then shows its isolated host without taking focus and presents the same target before capturing. It does **not** claim that an unpresented WebContentsView in a permanently hidden host can supply a compositor screenshot.

The two-frame synchronization is grounded in the native probe: immediately after making the resized guest visible, a direct capture sometimes returned `UnknownVizError`; once its frame had painted, capture returned the real image. `beginFrameSubscription` did not emit reliably in the isolated native test. Waiting for two actual renderer animation-frame callbacks followed by the native `capturePage` call passed without a fixed delay or repeated retry. Cancellation removes the requesting waiter before any deferred capture; a later frame cannot revive the cancelled screenshot result.
