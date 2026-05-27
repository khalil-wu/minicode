# 05E MCP Runtime

## Claude Code Areas To Read

- `src/services/mcp/client.ts`

## What To Extract

- connection lifecycle
- auth and session expiry handling
- retry and recovery classes
- long-running progress reporting
- large result truncation and persistence

## MiniCode Mapping

- `backend/mcp/*`
- `backend/tools/mcp_tools.py`
- `backend/main.py`
- websocket runtime/status payloads

## MiniCode Adoption Decision

- Split MCP responsibilities into connection cache, capability discovery, execution adapter, and recovery/error handling.
- Extend runtime/status payloads to expose structured MCP health instead of generic string errors over time.
- Keep backward-compatible protocol evolution while the frontend transitions to richer MCP runtime state.
