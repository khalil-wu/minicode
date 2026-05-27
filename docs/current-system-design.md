# MiniCode Current System Design

This document describes the code that exists in the repository today. It does not list already-completed work as a gap.

## 1. System Boundary

MiniCode is currently a single-repo `FastAPI + WebSocket + React + Zustand` application.

- `backend/main.py`
  Owns app startup, HTTP routes, status payload assembly, uploads, and WebSocket entry.
- `backend/ws/handler.py`
  Owns session lifecycle, conversation switching, runtime snapshots, permission mode/rules commands, and event emission.
- `backend/agent/query_engine.py`
  Exists as the structured query submission layer and is already used from the websocket session runtime.
- `backend/agent/loop.py`
  Still contains the lower-level agent execution loop for model calls, tool calls, approvals, and streaming output.
- `backend/commands/catalog.py`
  Now acts as the authoritative backend source for built-in protocol command metadata and composer slash-command catalog metadata.
- `backend/agent/claude_md.py`
  Now provides a structured guideline bundle loader with provenance, ordered blocks, and cache invalidation.
- `backend/conversations/*`
  Owns durable conversation metadata, transcript persistence, summary, compaction state, and permission state persistence.
- `frontend/src/hooks/useWebSocket.ts`
  Owns WebSocket connection, reconnect, heartbeat, queueing, and event dispatch into the store.
- `frontend/src/stores/chatStore.ts`
  Still uses a single Zustand store, but already consumes authoritative runtime session snapshots and permission events from the backend.
- `frontend/src/components/ChatPanel.tsx`
  Remains the orchestration shell for the chat surface, but it already delegates major areas to subcomponents.

## 2. Runtime Model

### 2.1 Backend Session Runtime

Each WebSocket session maintains runtime state including:

- `session_id`
- active conversation id
- active task id
- selected model
- permission mode and permission source
- active permission rules
- running task summary
- invoked skill names

This runtime state is emitted as authoritative `task.update` snapshots from `backend/ws/handler.py`.

### 2.2 Durable Conversation State

Conversation durability stays in `backend/conversations/`:

- conversation meta
- transcript
- context snapshot
- memory mode
- permission mode and rules
- summary and compaction state

Runtime state is not rebuilt purely from frontend guesses. The frontend now receives authoritative snapshots and event updates from the backend.

### 2.3 HTTP APIs

Current important APIs:

- `GET /health`
- `GET /api/status`
- `GET /api/llm/settings`
- `PUT /api/llm/settings`
- `POST /api/llm/models/refresh`
- `POST /api/uploads`
- `GET /api/guidelines`
- `POST /api/chat`

`/api/status` includes:

- MCP summary
- skills summary
- memory summary
- runtime snapshot
- capability snapshot
- LLM summary

`/api/guidelines` returns structured guideline blocks and rendered markdown.

## 3. Command System

### 3.1 Authoritative Backend Catalog

`backend/commands/catalog.py` is now the backend authority for:

- built-in websocket/protocol commands
- composer slash-command metadata

The catalog is used to populate `/api/status -> capabilities`.

Composer command entries now include canonical metadata such as:

- `id`
- `name`
- `command`
- `label`
- `description`
- `template`
- `type`
- `source`
- `availability`
- `enabled`

### 3.2 Frontend Consumption

The frontend normalizes composer commands from backend capability payloads through `frontend/src/lib/composer-commands.ts`.
Local runtime slash behavior is now centralized in `frontend/src/lib/runtime-commands.ts` instead of being embedded directly inside `ChatPanel.tsx`.

Important current behavior:

- backend payload is preferred when present
- disabled commands are filtered out
- authoritative metadata like `type`, `source`, and `availability` is preserved
- static fallback commands still exist for offline/bootstrap resilience
- UI-local slash execution paths are delegated through a testable runtime command layer
- frontend `/tasks`, `/status`, and bare `/permissions` now request backend inspection commands and consume unified `command.result` events

### 3.3 Current Limitation

Backend slash routing is now catalog-driven in `backend/commands/slash_commands.py`:

- composer commands are registered from the authoritative catalog
- local commands (`/new`, `/clear`, `/plan`, `/permissions`, `/memory`, `/tasks`, `/status`, etc.) dispatch canonical backend protocol commands
- template commands (`/review`, `/debug`, `/refactor`, etc.) are expanded consistently and reported through `command.result`

Remaining limitation:

frontend still keeps a runtime slash layer for UI-side affordances and fallback behavior. This means semantics are reduced but not yet 100% single-path across all entry points.

Key examples where frontend behavior is still part of the flow:

- `/new`
- `/clear`
- `/plan`
- `/permissions`
- `/tasks`
- `/status`

The inspection result channel remains standardized via `command.result`, but full execution unification still needs one final consolidation pass in the frontend runtime command layer.

This is still a known follow-up item, not a hidden gap.

## 4. Agent Execution

### 4.1 Query Entry

`backend/ws/handler.py` submits user turns through `QueryEngine`.

Current high-level turn flow:

1. normalize user input and attachments
2. ensure active conversation/runtime state
3. submit query through `QueryEngine`
4. execute through `run_agent_loop`
5. stream events back to frontend
6. persist transcript/summary/runtime changes

### 4.2 Context Assembly

`backend/agent/context.py` currently handles:

- system prompt construction
- guideline injection
- skill context injection
- inherited memory notes
- RAG retrieval injection
- history token budgeting and compaction support
- native multimodal attachment injection for provider-capable models

### 4.2.1 Native Multimodal Policy

MiniCode treats model-native multimodal understanding as the first path, and tools as the fallback/verification path:

- Images are kept as base64 attachments and injected into `LLMMessage.images`.
- PDFs are kept as base64 document attachments and injected into `LLMMessage.documents` when the active provider wire format supports native document input.
- Text extraction, chunk indexing, and `read_artifact` remain available for search, oversized files, unsupported providers, and exact quote/reference workflows.
- Upload-time ingestion must not eagerly summarize images with a fixed provider. The selected conversation model should interpret the original attachment at answer time.
- Tools are still preferred for codebase reads/writes, filesystem state, external web/API lookups, and any operation where deterministic verification matters.

Provider mapping:

- OpenAI Responses: images use `input_image`; PDFs use `input_file`.
- OpenAI Chat Completions / compatible gateways: images use `image_url`; PDFs fall back to extracted text/artifact unless the gateway documents native file blocks.
- Anthropic Messages: images use `image`; PDFs use `document`.
- Gemini and other native multimodal providers should follow the same adapter contract: preserve original bytes and map them to the provider's file/media parts before considering parser tools.

### 4.3 Guidance Loading

`backend/agent/claude_md.py` now loads ordered sources in this order:

1. `CLAUDE.md`
2. `.claude/CLAUDE.md`
3. `.claude/rules/*.md`
4. `CLAUDE.local.md`

It now exposes:

- ordered blocks
- provenance metadata
- additional directory support
- cache invalidation on file change
- rendered markdown for current prompt consumers

## 5. Frontend UI State

### 5.1 What Is Already Implemented

The following are already present in the codebase and should not be tracked as missing:

- message list virtualization
- visible hydration/loading shell behavior
- ChatPanel delegation to subcomponents
- reconnect-aware WebSocket transport
- inline approval and ask-user handling
- workspace mentions and workspace panel integration
- permission rules panel backed by backend events

### 5.2 Current Store Shape

`frontend/src/stores/chatStore.ts` is still a monolithic store, but it already tracks:

- conversations and transcripts
- runtime snapshots
- permission rules state
- approval and ask-user state
- MCP state
- skills state
- LLM settings
- composer slash-command catalog

Store slicing into dedicated runtime/conversation/permissions/composer/workspace slices is still future work.

## 6. Tests Covering Current Architecture

Current repository tests already cover important architectural behavior including:

- capability snapshot exposure
- runtime snapshot merging
- permission mode update flow
- permission rules add/list/remove round-trip
- conversation switch restoring permission state
- ChatPanel delegation architecture
- message virtualization expectations
- composer command normalization
- structured guideline bundle ordering and invalidation

## 7. Current Gaps

These are the real remaining gaps after correcting stale docs:

- session/runtime state is still not split into dedicated frontend store slices
- slash-command execution semantics are still partly local in `ChatPanel.tsx`
- `QueryEngine` is not yet the sole authoritative home for all turn-state phases
- permission context and tool registry parity with Claude Code is incomplete
- MCP auth/session-expiry/progress lifecycle is still lighter than the Claude Code mainline
- frontend runtime shell visibility is still less rich than Claude Code
- guideline blocks are structured now, but downstream consumers still mostly use rendered markdown

## 8. Recommended Next Implementation Order

1. Continue moving runtime and slash-command semantics out of `ChatPanel.tsx`
2. Expand runtime snapshot and store slices around authoritative session state
3. Promote `QueryEngine` into the single turn-state authority
4. Align permission context and filtered tool registry behavior
5. Complete MCP lifecycle/auth/progress parity
