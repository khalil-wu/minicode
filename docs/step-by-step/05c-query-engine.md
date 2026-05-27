# 05C Query Engine

## Claude Code Areas To Read

- `src/QueryEngine.ts`
- `src/query.ts`

## What To Extract

- the explicit turn state machine
- where model calls, tool calls, approvals, retries, compact boundaries, and termination are coordinated
- which failures are classified at the query-engine layer instead of leaking from lower execution layers

## MiniCode Mapping

- `backend/agent/query_engine.py`
- `backend/agent/loop.py`
- `backend/ws/handler.py`

## MiniCode Adoption Decision

- Keep `loop.py` as the lower-level execution engine.
- Continue promoting `query_engine.py` into the authoritative turn orchestrator.
- Move retry/fallback/reflection/orchestrator hooks behind explicit query-engine phases instead of scattering them through the loop body.
