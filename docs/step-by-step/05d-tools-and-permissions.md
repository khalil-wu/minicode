# 05D Tools And Permissions

## Claude Code Areas To Read

- `src/tools.ts`
- `src/Tool.ts`
- permission setup/runtime helpers

## What To Extract

- how the tool registry is assembled
- how permission context affects visible tools vs executable tools
- how approval, deny, diff-review, and ask-user flows share one protocol

## MiniCode Mapping

- `backend/tools/registry.py`
- `backend/permissions/*`
- `backend/ws/handler.py`
- `backend/tools/*`

## MiniCode Adoption Decision

- Keep one shared tool registry that can produce both full and filtered capability views.
- Keep permission mode/rules authoritative on the backend.
- Continue aligning file, shell, memory-write, workspace, and MCP tools to a shared approval protocol instead of bespoke per-tool behavior.
