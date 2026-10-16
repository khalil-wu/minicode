# Windows remote PTY resize

The selected Windows desktop host now resizes its original ConPTY rather than returning HTTP 409. The remote terminal keeps its original session, wrapper PID, shell and console child. The terminal byte stream is unchanged; resize requests use a separate pair of inherited anonymous pipes.

## Source and ownership

The reference checkout is `.tmp/codex-src`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`. `codex-rs/utils/pty/src/process.rs::ProcessHandle.resize` calls its original master or driver resizer. `utils/pty/src/win/conpty.rs` passes the character size to the existing `PsuedoCon`; `win/psuedocon.rs` calls the actual `ResizePseudoConsole` and returns its HRESULT error.

MiniCode's bundled Windows wrapper is built from the separately pinned Source archive `rust-v0.158.0-alpha.2.1`, SHA256 `e0a5e3ba9421f3f3f46836974d0396bbd8782721b4913b614ff0267911e0a3ee`. That version has the same master resize path. It is not presented as the newer reference commit.

The existing native `run_pty` forwarded Source's `SpawnedProcess` into its stdio bridge but had no independent control route. The owned patch now exposes a shared-session callback in that bridge; its original entry point still calls the same body with an empty callback. The private PTY launcher receives an `Arc` clone of that exact `ProcessHandle` and serves resize commands on its inherited control handles. The shell does not inherit those handles: Source's ConPTY `CreateProcessW` uses `bInheritHandles=FALSE`.

The Python owner passes only the two child control handles in the explicit Windows handle list, then closes its child ends after spawning. It retains the request writer and response reader with the original wrapper. A resize returns only after that native session reports success, or propagates the original native error. Requests are serialized; a cancelled requester drains its accepted response before another request can read the pipe. The original process wait closes the remaining control handles. Failed startup closes the allocated handles through its existing scoped resource handoff.

`NativeTerminals` still checks the actual window and conversation owner before operating. Detaching the relay does not retire terminal or control ownership. `window:release`, terminal kill and daemon shutdown continue through the original process-tree reaper. Unix retains its original `TIOCSWINSZ` operation, now exposed by the same PTY process method. Dimension types and bounds are checked once at the remote request boundary; Windows uses the native signed COORD bound, while Unix retains its unsigned winsize bound.

No resize control is written as terminal text, no replacement shell is created, and no sandbox runner, managed Linux routing, MCP/public protocol, account, ACL or firewall provisioning was changed.

## Real native validation

The candidate was built with the existing pinned producer and cached source/target, using Rust 1.95.0. `prepare-windows-sandbox-runtime.ps1` wrote only `output/windows-remote-pty-resize-20261016/native-runtime/`. The formal runtime directory was not overwritten. Its manifest records the source fingerprint and actual three executable hashes.

The probe program remains in the same live PTY process and calls `os.get_terminal_size()`, with both stdin and stdout reporting TTY. It writes actual OS measurements to an owned temporary file, so echoed commands or an API's returned dimensions cannot supply the expected result.

- The direct wrapper test proves the same console child PID changes from its original size to **101Ã—31**, then **132Ã—44**, and exits normally.
- The real daemon test starts that probe through the owned interactive shell, resizes it, detaches the actual proxy, reconnects to the same daemon generation/backend port, and resizes that same session and child PID again. Both actual console measurements match.
- A different authenticated window and a different conversation cannot resize the terminal. Invalid dimensions are rejected before native control.
- Releasing the original window reaps its actual console child and removes its session while another window's live terminal remains registered.
- A real native error is returned intact: unsigned column `65535` becomes native signed COORD `-1`, and the same Source handle returns HRESULT `-2147024809`. The original channel then successfully resizes that same child to **132Ã—44** and exits zero.

`native-invalid-coord-probe.json` preserves that independent error observation. The initial new negative test incorrectly assumed `(0,0)` would be rejected; the actual Windows API accepted it. The first complete batch is preserved as **23 passed / 1 failed**. Production was not changed to manufacture the expected error; the test now uses the independently observed native rejection.

The final latest-result scope is **24 unique tests passed, zero failures/errors/skips**. It combines the initial full-file results with the two-test corrected file rerun; it does not claim a second complete batch. This covers the new resize tests, both existing actual desktop-daemon cases, relay byte/flow/half-close behavior and the complete shell PTY owner/yield/stdin/timeout/cancel suite. The long real command-retention case emitted the configured 30-second faulthandler diagnostic and then passed; that original output remains in the log.

The candidate build, full initial XML/log, narrow repair XML/log, actual size properties, source freeze and runtime identities are indexed by `output/windows-remote-pty-resize-20261016/delivery.json`. Parent task activation, packaging and final CI are separate gates. No user window, data or profile was operated on, and this agent performed no Git operation.

## Repeated-cancellation review

The Python control owner now uses the existing `to_thread_cancel_safe` primitive while holding the original channel lock. A second cancellation cannot cancel the asynchronous worker wrapper while its actual blocking pipe read is still active. The real ConPTY test gates the original response reader after the first request has reached that pipe, cancels its owner twice, and proves a second resize remains behind the lock until that exact first ACK is consumed. The same console child then measures 132¡Á44 and exits zero.

The affected three-test file passed using `output/ci-repair-20261015/pytest-python311.py`, the established bundled-Python test entry. Earlier direct `-m pytest` and manual plugin invocation attempts could not execute the asynchronous cases; their logs remain as environment diagnostics. No test dependency was installed into or changed in the bundled runtime. Latest results now cover **25 unique tests passed, zero failures/errors/skips**; only this affected file was rerun after the ownership correction. The Rust candidate is unchanged.
