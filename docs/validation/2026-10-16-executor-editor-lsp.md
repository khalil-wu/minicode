# Independent executor editor LSP

The editor language endpoint now starts its language server on the accepted
executor. It no longer returns the former unconditional
`editor.executor_language_server_unconfigured` response. The existing local
desktop language-service route remains local to that desktop host.

Source reference: checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`,
`exec-server/src/server/process_handler.rs`, `local_process.rs`, and
`exec-server-protocol/src/protocol.rs`. The implementation uses actual
`process/start` with `pipeStdin`, `process/write`, ordered byte chunks from
`process/read`, and the original handle's termination/close receipt. It adds no
Source protocol method or environment-file field. Source's app-server filesystem
processor selects its local environment; the independent executor editor remains
a MiniCode workbench feature rather than a claimed Source remote-editor API.

`backend/lsp/executor_editor.py` retains a clone of the accepted environment
lease. Its stdio adapter feeds the existing LSP framing client with binary
stdout/stderr. Registration, conversation, workspace URI and language distinguish
the cached server. Configuration or permission changes close the original client
before starting another. Lifecycle cleanup never resolves a replacement registry
entry to close an old server.

Target PATH supplies the default Pyright, YAML and clangd commands. Optional
`.minicode/language-servers.json` is a dedicated MiniCode editor configuration
read through the same executor and scope. Explicit argv, environment,
initialization options and settings are preserved; controller Node, compilers,
bundled servers and Python formatter are not substituted. Configuration examples
and formatting capability limits are in `backend/lsp/EDITOR.md`.

Dirty text uses target file URIs and renderer document/model versions. Other dirty
documents are synchronized before the requested document. The shared LSP client
now answers the server's actual `workspace/configuration` requests with these
settings; returning `null` had discarded the Pyright analysis configuration.
clangd defaults include its actual `--use-dirty-headers` option, verified against
the installed binary's help, so header overlays participate in parsing. This also
fixes the same omission in the local desktop clangd launch.

HTTP disconnection cancels the original LSP request and sends `$/cancelRequest`.
The existing document/window/client fences still govern `didClose`, `shutdown`
and `exit`; the original process must confirm closure before its lease is released.
A real target start rejection remains the existing HTTP 502 / executor RPC error,
not a successful language-service result or a controller fallback.

The owned Source peer was the installed Windows Codex exec-server, with isolated
temporary CODEX_HOME, state and workspaces. Actual target Pyright verified hover,
definition, references, rename, completion, signature help, symbols, diagnostics,
dirty cross-file inputs, exact model versions and stale-version rejection.
Actual YAML verified formatting without saving the dirty buffer. Actual clangd
verified dirty-header semantics and explicitly configured compiler flags. A real
Source child protocol peer verified HTTP cancellation, the matching original LSP
request ID, document/server shutdown, retained original registration after registry
replacement and actual handle/lease closure. POSIX URI case handling was checked
independently on the Windows controller; this is not a Linux-host execution claim.

Evidence in `output/executor-lsp-20261016/`:

- `executor-lsp-batch01.xml`: 43 nodes, 40 passed, 3 failed, no skips. Initial
  diagnostic expectation, dirty-header behavior and obsolete 503 expectation are
  retained as failures.
- `executor-lsp-batch02.xml`: 43 nodes, 42 passed, 1 failed, no skips. The remaining
  actual clangd dirty-header failure is retained.
- `executor-clangd-diagnostic03.xml`: the one failed clangd diagnostic, exposing
  the real `unknown_typename` result.
- `executor-lsp-batch04.xml`: 34 affected target/editor/LSP nodes passed, no failures
  or skips. The nine unchanged HTTP editor nodes use their passing batch02 records.
- `executor-pyright-settings-batch05.xml`: the strengthened Pyright case passed.
  Changing the target's diagnostic severity setting actually removed the earlier
  assignment diagnostic and closed the old server handle/lease before replacement.

The latest exact-node combination is **43 passed, 0 failed/error/skipped**; it is
not a claim that the last batch reran all 43. The final single-case record replaces
the same Pyright node rather than adding another node. Existing Uvicorn/websockets warnings
remain. No external SSH/WSL host was deployed, no user workspace/configuration was
rewritten, and no system provisioning or account operation was performed.
