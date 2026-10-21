# CEF sidebar CI experiment

This directory is an isolated experiment, not MiniCode's production browser.
It uses an Electron 43.4.0 owner and a separate CEF Chrome-style child HWND in
the owner's original right 60% rectangle. It does not import production code,
read user profiles, or install a runtime into MiniCode.

The independent `CEF sidebar prototype (Windows)` workflow runs on matching
push paths and `workflow_dispatch`. The SDK is downloaded only inside the
disposable Windows runner. `prepare-cef.py` and the JS launcher refuse local
execution. No Playwright, CDP, screenshots, or desktop automation is involved.
The prototype itself exercises native HWND/CEF APIs on the runner.

The workflow builds one DLL against the pinned standard CEF distribution and
renames its matching `bootstrapc.exe` to the probe's executable. This follows
the sandbox-enabled Windows `tests/cefsimple/CMakeLists.txt.in` contract at CEF
commit `c1a097eaa7738483abe2ea9d6297a937d6a884c0`. It exports the documented
`RunConsoleMain` entry, copies the full runtime resources, and applies the
CEF LPAC ACL macro. It does not set `no_sandbox` or `enable-automation`.

Each 1×/1.5× run starts the CEF helper twice against the same isolated profile
and local HTTP origin. The first process writes content settings and a random
localStorage token; the second only reads them. Both processes check:

- Actual Chrome runtime style, cross-process parent/root HWND, and WS_CHILD.
- Original owner client pixels converted through CEF Display into DIP bounds;
  actual child pixels and renderer viewport checked before/after host resize.
- Native DPI values, forced renderer scale, native focus and document focus.
- No visible additional top-level window belonging to the CEF helper.
- Real `CloseBrowser(false)`/`OnBeforeClose`, followed by helper exit; the
  original Electron owner must remain alive after asynchronous close delivery.

`host.jsonl` and raw helper stdout/stderr remain available on failure. A phase
has a 45-second bound; timeout is a failure, and only its disposable helper tree
is stopped. `runs.json` records both scale results. The workflow uploads logs
and JSON evidence, not a production runtime or a parity-completion claim.

Physical cross-monitor DPI, human input/IME, owner-initiated close/pane hiding,
native password/permission/download UI and complete browser parity are explicitly
skipped. Forced device scale is not evidence of a physical WM_DPICHANGED event.
Native build and runtime results must come from this workflow; local syntax/API
inspection alone does not establish that the embedding works.
