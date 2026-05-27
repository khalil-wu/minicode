# 05A Startup And State

## Claude Code Areas To Read

- `src/bootstrap/state.ts`
- `src/state/AppStateStore.ts`
- startup entry and runtime hydration path

## What To Extract

- how CLI/bootstrap inputs become runtime session state
- which fields belong to session runtime vs durable conversation/project state
- how reconnect and hydration restore the authoritative runtime shell

## MiniCode Mapping

- `backend/ws/handler.py`
  Session runtime authority today.
- `backend/conversations/*`
  Durable conversation state.
- `frontend/src/stores/chatStore.ts`
  Current frontend state entry point.

## MiniCode Adoption Decision

- Keep durable transcript and conversation meta in `backend/conversations/*`.
- Continue moving session runtime state into authoritative websocket snapshots instead of frontend inference.
- Treat frontend store slicing as a follow-up refactor, not a blocker for backend runtime authority.
