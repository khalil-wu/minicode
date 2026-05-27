# 05B Commands

## Claude Code Areas To Read

- `src/commands.ts`

## What To Extract

- how built-in, local, and extension commands are modeled
- how command availability is computed
- how one authoritative catalog feeds UI suggestion and runtime execution

## MiniCode Mapping

- `backend/commands/catalog.py`
- `backend/commands/registry.py`
- `backend/main.py`
- `frontend/src/lib/composer-commands.ts`
- `frontend/src/components/ChatPanel.tsx`

## Current MiniCode Decision

- Backend is now the authority for command metadata and composer catalog payloads.
- Frontend consumes the backend catalog first and only keeps static fallback entries for resilience.
- Local slash behavior has been extracted from `ChatPanel.tsx` into `frontend/src/lib/runtime-commands.ts` to reduce component-level command semantics.
- Backend now returns unified inspection responses for `/tasks`, `/status`, and bare `/permissions` through `command.result`.
- Remaining work is to move the remaining frontend-local UI command behavior behind a fuller backend-authoritative command execution model.
