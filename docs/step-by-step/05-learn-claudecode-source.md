# 05 Learn Claude Code Source

This step is now the index for the structured Claude Code study notes used by the MiniCode parity plan.

## Purpose

We are not trying to byte-copy `claude-code-main`. We are aligning MiniCode to the same core runtime architecture:

- startup and runtime state
- command system
- query main loop
- tools and permissions
- MCP lifecycle

Each note ends with a concrete "MiniCode adoption decision" so the research feeds implementation directly.

## Study Notes

1. [05a-startup-and-state.md](./05a-startup-and-state.md)
   Covers startup entry, bootstrap state, app state, session lineage, runtime hydration.
2. [05b-commands.md](./05b-commands.md)
   Covers authoritative command catalog design and command availability semantics.
3. [05c-query-engine.md](./05c-query-engine.md)
   Covers the turn state machine, orchestration boundaries, retry/error handling, and compact boundaries.
4. [05d-tools-and-permissions.md](./05d-tools-and-permissions.md)
   Covers tool registry construction, permission context, filtered tool views, and approval flows.
5. [05e-mcp-runtime.md](./05e-mcp-runtime.md)
   Covers MCP connection lifecycle, auth/session expiry, progress reporting, and recovery paths.

## Current MiniCode Status

Already landed in repo:

- backend command catalog authority via `backend/commands/catalog.py`
- extended runtime session snapshot fields
- structured guideline bundle loading via `backend/agent/claude_md.py`
- corrected system design baseline in `docs/current-system-design.md`

Still in progress:

- full `QueryEngine` authority over the entire turn lifecycle
- full permission/tool registry parity
- richer MCP auth/session lifecycle parity
- frontend runtime shell and store-slice refactor

## Working Rule

Whenever we finish reading one Claude Code area, we should land one MiniCode action immediately:

- add or update one repo doc
- add or update one test
- land one focused implementation slice
