# MiniCode DESIGN Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining gaps between the current implementation and the approved `DESIGN.md`/`need.md` behavior, especially upload/docparse, artifact preview, protocol usage, MCP state updates, and verification coverage.

**Architecture:** Keep the current FastAPI + WebSocket + React structure, but finish the missing closure paths instead of adding new subsystems. Session-scoped WebSocket state remains the runtime spine; HTTP endpoints are added only where the design needs explicit file upload or artifact read APIs.

**Tech Stack:** Python 3.11, FastAPI, WebSocket, React 18, TypeScript, Vite, pytest

---

## Planned File Structure

- Modify: `backend/main.py` - Add strict upload/artifact/status endpoints and keep backend mainline as the verified entrypoint.
- Modify: `backend/ws/handler.py` - Expose session metadata and support stricter session lookups for upload/artifact flows.
- Modify: `backend/agent/loop.py` - Preserve and emit usage/budget signals through the loop.
- Modify: `backend/agent/message.py` - Synchronize runtime event literals with the actual protocol.
- Modify: `backend/artifact/store.py` - Support artifact metadata reads needed by the HTTP/UI bridge.
- Modify: `backend/rag/pipeline.py` or add helper integration in backend - Index uploaded documents into the `documents` collection.
- Modify: `frontend/src/types.ts` - Synchronize protocol/event types and upload/artifact response types.
- Modify: `frontend/src/hooks/useWebSocket.ts` - Handle session setup and ack-driven state transitions.
- Modify: `frontend/src/stores/chatStore.ts` - Track session id and keep UI state aligned with backend truth.
- Modify: `frontend/src/components/ChatPanel.tsx` - Wire real upload, retry guards, and artifact modal reads.
- Modify: `frontend/src/components/MessageBubble.tsx` - Forward artifact click handlers.
- Modify: `frontend/src/components/ToolCallView.tsx` - Keep artifact click behavior explicit.
- Modify: `tests/test_chat_api.py` - Move verification onto `backend.main`.
- Modify: `tests/test_app_bootstrap.py` - Move bootstrap verification onto `backend.main`.
- Add: `tests/test_backend_mainline.py` - Verify upload, artifact fetch, and status behavior against the new mainline.
- Add: `tests/test_ws_session.py` - Verify WS session metadata and protocol closure paths.
- Add: `tests/agent/test_loop_protocol.py` - Verify `done` usage propagation and protocol events.

## Execution Outline

### Task 1: Unify Verification Onto The Real Backend Mainline
- Update tests so they import `backend.main:app`, not the legacy `app.py`/`agent/`.
- Add regression coverage for `backend.main` import, `/api/status`, and the live FastAPI bootstrap path.

### Task 2: Close The Artifact Read Path
- Add a backend read endpoint that can resolve artifacts from the active session runtime.
- Wire frontend tool cards to request and display artifact content in the existing modal.

### Task 3: Implement Real Upload -> Parse -> Index -> Artifact Flow
- Add a real file upload API.
- Parse uploaded files with existing docparse logic.
- Save full text as artifact, write document chunks into the `documents` vector store, and return the artifact/doc identifiers.
- Wire the frontend upload button to call this API instead of sending a fake text-only placeholder.

### Task 4: Close WebSocket Protocol Drift
- Emit session metadata needed by the UI.
- Propagate `done.usage` and any budget updates that the frontend already expects.
- Align backend event literals with the real runtime protocol.

### Task 5: Tighten Frontend State Truth
- Remove double-toggle behavior from slash-command skill activation.
- Keep retry/upload flows from creating fake assistant placeholders while disconnected.
- Preserve MCP status fidelity instead of collapsing richer backend states unnecessarily.

### Task 6: Verify Against DESIGN.md/need.md
- Run focused pytest coverage for the new backend paths.
- Run full pytest.
- Run frontend TypeScript/Vite build with `npm.cmd run build`.
- Compare the finished behavior against the remaining documented closure checklist.
