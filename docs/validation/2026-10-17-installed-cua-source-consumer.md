# Installed CUA runtime consumer

Source: local Codex desktop package 26.1013.40934 / Appx 26.1013.4894.0,
and codexsrc 822e58cc. Reference inventory and exact native host excerpts are
under `output/desktop-services-source-20261017/`.

MiniCode consumes the user's explicitly selected installed CUA runtime. No CUA
package/native executable is bundled or redistributed. Loading MCP settings
records this server; a captured local Windows task admits it with its real
thread, turn, environment and network owner. Other environments do not receive
a fabricated local computer capability.

Use the existing `.mcp.json` server configuration. The `cua_repl` entry's
`command` is the selected runtime's absolute `bin/node.exe`, `args` contains
that same runtime's absolute
`bin/node_modules/@oai/cua-repl/bin/cua-repl.mjs`, and `env` contains:

```json
{
  "CUA_REPL_NODE_REPL_PATH": "<selected absolute runtime>/bin/node_repl.exe",
  "CUA_REPL_ENABLED_SURFACES": "computer"
}
```

This uses the Source launcher's existing configuration contract. There is no
installation-directory scan, account login, auth copying or alternate-runtime
fallback. Missing files and mismatched executable/launcher identities are real
errors. Browser control continues through its existing capability; this native
consumer admits the computer surface explicitly.

The original installed CUA launcher supplies tool schemas, API documentation,
the `cua` bootstrap and the trusted `@oai/sky/service` module. MiniCode supplies
an application-owned `CODEX_HOME`, the selected runtime paths, and its own
Source-compatible Windows pipe host. The installed `WindowsHelperTransport`
owns native helper launch, native protocol, app policy/approval replay, Escape
interrupts, actual native errors and shutdown. MiniCode never implements that
helper protocol.

The host is shared for the same selected runtime and application home. It
serializes native work, sends the original app approval request to the same
socket, waits up to Source's 300 seconds, rejects unreachable approvals, and
closes the original package transport when the last socket disappears.
`closeActiveTurn` matches the real session and turn. Retired older turns cannot
close a newer active turn. Final turn cleanup follows original consumers and
capability retirement; closing a thread closes its original Node process,
host reference and environment lease.

CUA MCP calls carry core-owned `x-codex-turn-metadata` with the real
conversation/thread and turn; arbitrary caller metadata cannot replace these
identities. The originating MCP owner still supplies cancellation and the
interactive approval session.

App prompts are actual MCP elicitations. The Source package gives
`codex_approval_kind=mcp_tool_call`, `connector_id=computer-use`, real app ID,
display name, risk level and allowed persistence modes. The frontend must use
the Computer Use approval card and return explicit `accept` plus
`content.persist=session|always`, or `decline`; a generic empty form must not
silently approve an app. Audio requests do not gain persistent approval.

## Validation boundary

The accompanying fixtures exercise the actual Node host and named-pipe flow
against a controlled module at the installed-package boundary. They never
launch, screenshot, inspect or control a desktop application. They cover
fragmented frames, approval/decline, package errors, cross-client serialization,
original thread/turn cleanup, runtime identity selection and natural host exit.

Root's unified backend run is tracked separately under
`output/source-backend-unified-20261017/`. Its fixture evidence is distinct from
the actual installed-runtime validation below.

The actual installed package was then exercised through MiniCode's real local
environment, MCP manager, captured owner and companion lifecycle in two new,
application-owned profiles:

- `output/cua-readonly-bootstrap-profile-20261017/proof.json`: real initialize,
  tool/schema inventory and public API bootstrap passed. This proves bootstrap
  only, not native desktop execution.
- `output/cua-readonly-app-count-profile-20261017/proof.json`: the independent
  bootstrap and public `cua.computer.list_apps()` call passed. Only aggregate
  counts (40 applications, 5 windows) were emitted; inventory content and titles
  were not logged.

Both runs returned the original thread/turn metadata unchanged, ended the
original host with exit code 0, and left the executor registry and companion
pool empty. No app approval was raised or answered. No screenshot, desktop
input, ordinary-window open/close operation or official-home access was made.
This is read-only native enumeration evidence; app approval, screenshot and
input behavior remain outside these two probes.
