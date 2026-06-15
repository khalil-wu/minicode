# MiniCode Backend Deep Audit Report

**Date:** 2026-06-10
**Scope:** 60+ source files across 14 subsystems in `C:\Desktop\MiniCode\backend`
**Focus areas:** agent/policies, agent/harness, ws/, ws/handlers/, bootstrap, api, permissions, approvals, artifact, conversations, tasks, terminal, hooks

---

## Executive Summary

The codebase is generally well-structured with good use of Protocol/dataclass patterns, frozen dataclasses for immutability, and clean separation of domain logic from transport. However, I identified **28 actionable issues** spanning security vulnerabilities, memory leaks, race conditions, incomplete implementations, and architectural concerns. Issues are categorized as **Critical**, **High**, **Medium**, or **Low** severity.

---

## Issue Summary Table

| # | Severity | Module | Issue |
|---|----------|--------|-------|
| 1 | HIGH | ws/handler.py | Unbounded task creation from WebSocket messages |
| 2 | HIGH | ws/handler.py | Memory leak: `_conversation_run_locks` never shrinks |
| 3 | HIGH | ws/handlers/terminal.py | Incomplete stub: `handle_terminal_resize` is a no-op |
| 4 | HIGH | ws/handlers/terminal.py | `terminal.exec` bypasses CONFIRM-level permissions |
| 5 | HIGH | ws/handlers/mcp.py | `TaskScheduler` instantiated fresh on every handler call (no shared state) |
| 6 | HIGH | ws/approval_runtime.py | `_session_approval_cache` grows without bound |
| 7 | HIGH | conversations/repository.py | No file locking for concurrent writes |
| 8 | MEDIUM | ws/handler.py | Memory leak: `_approval_diff_cache` persists for unresolved approvals |
| 9 | MEDIUM | ws/handler.py | Memory leak: `_pending_approval_payloads` persists for unresolved approvals |
| 10 | MEDIUM | ws/handler.py | `artifact_store.clear()` skipped on generation-mismatched disconnect |
| 11 | MEDIUM | ws/permission_runtime.py | Redundant double `build_context` call |
| 12 | MEDIUM | ws/agent_runner.py | Direct mutation of private `_persistent_notes` on ContextBuilder |
| 13 | MEDIUM | ws/agent_runner.py | Potential duplicate `done` events emitted for a single run |
| 14 | MEDIUM | ws/conversation_runtime.py | Silent exception swallowing in `_hydrate_snapshot` |
| 15 | MEDIUM | conversations/repository.py | Blocking `time.sleep` in retry loop on event loop thread |
| 16 | MEDIUM | conversations/repository.py | LRU cache uses O(n) list operations |
| 17 | MEDIUM | hooks/manager.py | Hook commands execute shell with LLM-influenced environment variables |
| 18 | MEDIUM | agent/harness/catalog.py | `_schema_required_args` silently swallows all exceptions |
| 19 | MEDIUM | bootstrap/app.py | All startup failures silently degrade with no aggregated health signal |
| 20 | MEDIUM | approvals/models.py | `ApprovalSummary` always reports `approved=0, rejected=0` |
| 21 | MEDIUM | Cross-cutting | 253 bare `except Exception` blocks across the codebase |
| 22 | MEDIUM | Cross-cutting | No structured logging or correlation IDs |
| 23 | MEDIUM | Cross-cutting | Mixed sync/async patterns in ConversationRepository |
| 24 | LOW | agent/harness/search_plan.py | Hardcoded timezone `Asia/Shanghai` |
| 25 | LOW | agent/harness/guardrails.py | Uses MD5 for result hashing |
| 26 | LOW | approvals/manager.py | Silent callback swallowing in `_notify_approval_update` |
| 27 | LOW | permissions/checker.py | `inspect.signature` called on every permission check |
| 28 | LOW | permissions/checker.py | Catastrophic command blocklist covers only ~13 patterns |

---

## Detailed Findings by Module

---

### 1. `ws/handler.py` -- WebSocketSession (1489 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\handler.py`

This is the central god object that manages the entire WebSocket session lifecycle. It inherits from four mixins: `SessionCommandHandlersMixin`, `SessionPermissionRuntimeMixin`, `SessionApprovalRuntimeMixin`, and `SessionAgentRunnerMixin`.

#### [HIGH] Issue #1: Unbounded task creation from WebSocket messages

**Lines 759-765:**
```python
task = asyncio.create_task(
    self._handle_command(
        command,
        connection_generation=active_generation,
    )
)
task.add_done_callback(self._on_command_task_done)
```

Every incoming WebSocket message spawns a new `asyncio.Task` with no concurrency limit or task tracking. While there is a per-conversation run guard (lines 1019-1036) that prevents duplicate agent runs, non-run commands (approvals, workspace changes, terminal operations, etc.) can all execute concurrently without bound. A misbehaving or malicious client could flood the event loop.

**Impact:** Resource exhaustion, potential event loop starvation.

**Recommendation:** Track spawned command tasks in a bounded set with a configurable limit (e.g., 32 concurrent commands). Reject or queue excess commands.

---

#### [HIGH] Issue #2: Memory leak -- `_conversation_run_locks` never shrinks

**Line 154:**
```python
self._conversation_run_locks: dict[str, asyncio.Lock] = {}
```

**agent_runner.py line 167:**
```python
lock = locks.setdefault(target_conversation_id, asyncio.Lock())
```

Every unique `conversation_id` that has ever been run creates a new `asyncio.Lock()` in this dict. Locks are never removed, even when conversations are deleted or archived. Over a long-running session with hundreds of conversations, this dict grows unboundedly.

**Impact:** Gradual memory increase over session lifetime.

**Recommendation:** Clean up locks when conversations are deleted or when the lock has not been used for a configurable TTL.

---

#### [MEDIUM] Issue #8-10: Approval payload caches never expire

**Lines 148-149:**
```python
self._pending_approval_payloads: dict[str, dict[str, Any]] = {}
self._approval_diff_cache: dict[str, dict[str, Any]] = {}
```

These caches are populated when approval requests are emitted but only cleaned when the approval is resolved (`_resolve_pending_approval`) or explicitly cancelled (`_cancel_pending_approvals`). If the client drops an approval request without responding, the entries persist for the session lifetime. The diff cache in particular can hold large payloads (up to 100KB per `APPROVAL_INLINE_PATCH_LIMIT_BYTES`).

**Impact:** Memory growth proportional to abandoned approvals.

**Recommendation:** Add a TTL-based eviction or clean up in the delayed disconnect cleanup (line 1383+).

---

#### [MEDIUM] Issue #10: `artifact_store.clear()` skipped on generation mismatch

**Line 773:**
```python
finally:
    if active_generation == self._connection_generation:
        self.artifact_store.clear()
```

After a reconnect (generation bump), the old generation's artifacts are never explicitly cleared. The `finally` block runs but skips the clear. Artifacts from the old connection persist until the session is fully cleaned up.

**Impact:** Artifacts from stale connections consume memory until session destruction.

---

### 2. `ws/handlers/terminal.py` (276 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\handlers\terminal.py`

#### [HIGH] Issue #3: Incomplete implementation -- `handle_terminal_resize` is a no-op

**Lines 62-63:**
```python
async def handle_terminal_resize(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return True
```

This is a complete stub. Resize events from the frontend are silently discarded. The underlying PTY process never receives `SIGWINCH` or equivalent. Terminal UIs that depend on terminal dimensions (e.g., `vim`, `htop`, `less`) will render incorrectly after any resize.

**Impact:** Broken terminal experience after resize.

**Recommendation:** Implement by calling `process._process.send_signal(signal.SIGWINCH)` on Unix or the Windows equivalent, after setting the new window size via `set_winsize`.

---

#### [HIGH] Issue #4: `terminal.exec` bypasses CONFIRM-level permissions

**Lines 219-228:**
```python
perm_level = checker.check("terminal.exec", command_args, context=session.permission_context)
denial = checker.get_denial_reason("terminal.exec", command_args, context=session.permission_context)
if denial or perm_level.name in {"CONFIRM", "DIFF_REVIEW", "ALWAYS_DENY"}:
    await session._send_ws_payload({
        "type": "terminal.output",
        "command": command,
        "output": denial or "terminal.exec requires agent tool approval; use run_command for approved execution.",
        "exit_code": -1,
    }, log_context="terminal.output")
    return True
```

When the permission level is `CONFIRM` or `DIFF_REVIEW`, the handler sends a rejection message and returns. It does NOT create an approval flow that would allow the user to approve the command. This means `terminal.exec` is effectively `ALWAYS_DENY` for CONFIRM-level tools, which is inconsistent with how the agent-loop approval flow works for the same permission levels on other tools.

**Impact:** Users cannot approve `terminal.exec` commands that require confirmation, even though the permission system is designed to allow this.

**Recommendation:** Either implement a proper approval flow for CONFIRM-level `terminal.exec`, or document that `terminal.exec` is ALWAYS_DENY and adjust the permission check accordingly.

---

### 3. `ws/handlers/mcp.py` -- Scheduler Handlers (428 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\handlers\mcp.py`

#### [HIGH] Issue #5: `TaskScheduler` instantiated fresh on every handler call

**Lines 335, 360, 377, 397:**
```python
async def handle_scheduler_list(session, data):
    from backend.tasks.scheduler import TaskScheduler
    scheduler = TaskScheduler()  # <-- New instance every call
    ...

async def handle_scheduler_add(session, data):
    from backend.tasks.scheduler import TaskScheduler
    scheduler = TaskScheduler()  # <-- Another new instance
    scheduler.add_task(...)
    ...
```

Every scheduler handler (`list`, `add`, `remove`, `toggle`) creates a fresh `TaskScheduler()` instance. The constructor calls `self._load()` which reads from disk (`.minicode/scheduled_tasks.json`). This means:

1. Each handler call does redundant file I/O.
2. There is no shared running scheduler loop -- `scheduler.start()` (which starts the `_run_loop` coroutine) is never called from these handlers.
3. Tasks added via the UI are persisted to disk but never actually executed because no scheduler loop is running.

**Impact:** Scheduled tasks are saved but never fire. The scheduler feature is effectively non-functional end-to-end.

**Recommendation:** Create a singleton `TaskScheduler` during bootstrap, store it on the session or in a global, and have handlers reference that shared instance. Call `scheduler.start()` during application startup.

---

### 4. `ws/approval_runtime.py` (271 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\approval_runtime.py`

#### [HIGH] Issue #6: `_session_approval_cache` grows without bound

**Lines 21, 39:**
```python
def _init_approval_cache(self) -> None:
    if not hasattr(self, "_session_approval_cache"):
        self._session_approval_cache: set[str] = set()

def _mark_session_approved(self, tool_name: str, args: dict[str, Any]) -> None:
    self._init_approval_cache()
    self._approval_cache_cache.add(self._approval_cache_key(tool_name, args))
```

Every approved tool call where the user checked "remember for session" adds a cache key of the form `"{tool_name}::{json_args}"` to this set. The set is never pruned, cleared, or bounded. For a long-running session with hundreds of approvals, this set grows indefinitely.

**Impact:** Gradual memory increase proportional to approved tool calls.

**Recommendation:** Either bound the cache with an LRU eviction policy, or scope the cache to the current conversation rather than the session.

---

#### [MEDIUM] Approval timeout has no proactive client notification

**Line 109:**
```python
result = await asyncio.wait_for(future, timeout=300)
```

After 5 minutes, the approval auto-rejects with `{"action": "reject", "guidance": "approval timed out after 5 minutes"}`. The client is never proactively notified that a pending approval expired. It only discovers this when it finally responds and gets a "stale approval" debug log. The frontend may show an approval dialog that is already dead.

**Recommendation:** Emit an `approval.cancelled` event to the client when the timeout fires, before returning the rejection.

---

### 5. `ws/permission_runtime.py` (183 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\permission_runtime.py`

#### [MEDIUM] Issue #11: Redundant double `build_context` call

**Lines 73-87:**
```python
def _sync_permission_mode_with_active_conversation(self, *, source: str) -> str:
    ...
    self._set_permission_context(           # First build_context call
        mode=requested,
        session_overrides=overrides,
        tool_deny_rules=deny_rules,
        source=source,
    )
    self.permission_context = self.permission_checker.build_context(  # Second, identical call
        mode=self.permission_context.mode,
        session_overrides=self.permission_context.session_overrides,
        tool_deny_rules=self.permission_context.tool_deny_rules,
        filesystem_constraints=self.permission_context.filesystem_constraints,
        workspace_scope=scope,
        source=source,
    )
    return requested
```

The first call to `_set_permission_context` already calls `build_context` and sets `self.permission_context`. The second call immediately overwrites it with a new context built from the same values plus `workspace_scope`. The first call's result is wasted.

**Impact:** Unnecessary CPU work on every conversation switch/create. Not a correctness bug.

**Recommendation:** Consolidate into a single `build_context` call that includes `workspace_scope`.

---

### 6. `ws/agent_runner.py` (730 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\agent_runner.py`

#### [MEDIUM] Issue #12: Direct mutation of private `_persistent_notes`

**Lines 282-293:**
```python
existing_notes = getattr(run_context_builder, "_persistent_notes", [])
existing_notes[:] = [
    note for note in existing_notes
    if note.get("kind") != "compaction_summary"
]
existing_notes.append({
    "kind": "compaction_summary",
    "title": "Compacted conversation memory",
    "content": compaction_summary,
})
```

This reaches into `ContextBuilder` internals and mutates a private list in-place using slice assignment. If `ContextBuilder` changes its internal representation (e.g., switches to a tuple or adds validation), this will silently break.

**Recommendation:** Add a public method to `ContextBuilder` like `set_persistent_note(kind, title, content)`.

---

#### [MEDIUM] Issue #13: Potential duplicate `done` events

In the `finally` block (lines 606+), if `failed_tool_only_reply` is generated and `run_failed_message` was previously empty, a new error event and done event are emitted (lines 640-653). But a done event may have already been emitted from the `except` block (line 604). This can result in two `done` events for the same run.

**Impact:** Frontend state machine may receive conflicting terminal signals.

**Recommendation:** Track whether a `done` event has already been emitted for the current run and guard against duplicates.

---

### 7. `ws/conversation_runtime.py` (271 lines)

**File:** `C:\Desktop\MiniCode\backend\ws\conversation_runtime.py`

#### [MEDIUM] Issue #14: Silent exception swallowing in `_hydrate_snapshot`

**Lines 145-146:**
```python
except Exception:
    return
```

If snapshot deserialization fails for any reason (corrupt data, incompatible format version, missing fields), the hydration silently fails with no logging. The user sees `is_hydrating: True` in the `conversation.switched` event but never receives the `conversation.hydration.updated` event with `is_hydrating: False`, leaving the UI in a perpetual loading state.

**Impact:** Confusing UX where the conversation appears to be loading forever.

**Recommendation:** At minimum, log the exception. Ideally, emit a hydration-complete event with an error flag so the UI can recover.

---

### 8. `conversations/repository.py` (520+ lines)

**File:** `C:\Desktop\MiniCode\backend\conversations\repository.py`

#### [HIGH] Issue #7: No file locking for concurrent writes

**Lines 344-354 (`_safe_write_text`):**
```python
def _safe_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
    import time
    for attempt in range(5):
        try:
            path.write_text(text, encoding=encoding)
            return
        except (IOError, PermissionError) as e:
            if attempt == 4:
                logger.error("Failed to write text to %s after 5 attempts: %s", path, e)
                raise e
            time.sleep(0.05 * (attempt + 1))
```

No file locking is used (`fcntl.flock` on Linux, `msvcrt.locking` on Windows). Two concurrent operations on the same conversation (e.g., `append_transcript_message` and `save_context_snapshot`) can produce a torn meta file. The `_append_transcript_message` method (lines 490-505) opens in append mode without locking; concurrent appends could interleave JSONL lines.

**Impact:** Potential data corruption under concurrent writes.

**Recommendation:** Use `portalocker` or platform-specific file locking. Alternatively, write to a temp file and atomically rename.

---

#### [MEDIUM] Issue #15: Blocking `time.sleep` in retry loop

**Lines 345-354:** `time.sleep(0.05 * (attempt + 1))` blocks the event loop thread. Since `ConversationRepository` methods are called synchronously from async handlers (not via `asyncio.to_thread`), these retries block all asyncio processing for up to 0.75 seconds across 5 attempts.

**Impact:** Event loop stalls during file I/O contention.

**Recommendation:** Either use `asyncio.to_thread` for file operations, or use `asyncio.sleep` in an async wrapper.

---

#### [MEDIUM] Issue #16: LRU cache uses O(n) list operations

**Lines 329-334:**
```python
self._record_cache_order.remove(record.id)  # O(n) list scan
self._record_cache_order.append(record.id)
while len(self._record_cache_order) > self._MAX_RECORD_CACHE:
    evict_id = self._record_cache_order.pop(0)  # O(n) list shift
```

With `_MAX_RECORD_CACHE = 64`, this is tolerable but not ideal.

**Recommendation:** Use `collections.OrderedDict` for O(1) move-to-end and pop-first operations.

---

### 9. `hooks/manager.py` (323 lines)

**File:** `C:\Desktop\MiniCode\backend\hooks\manager.py`

#### [MEDIUM] Issue #17: Hook commands execute shell with LLM-influenced environment

**Lines 268-277, 291:**
```python
def _tool_env(self, tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {"TOOL_NAME": tool_name}
    try:
        env["TOOL_ARGS_JSON"] = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        env["TOOL_ARGS_JSON"] = "{}"
    path_arg = args.get("path") or args.get("file_path") or args.get("directory")
    if isinstance(path_arg, str):
        env["TOOL_PATH"] = path_arg
    return env
```

Hook commands run via `asyncio.create_subprocess_shell(command, ...)` where the environment includes `TOOL_ARGS_JSON` containing raw tool call arguments from the LLM. A malicious or hallucinated tool call could inject crafted values into `TOOL_ARGS_JSON` that influence hook script behavior. While `sanitized_subprocess_env()` strips dangerous system env vars, the hook-specific additions (`TOOL_ARGS_JSON`, `TOOL_PATH`) are attacker-influenced.

**Impact:** Potential command injection if hook scripts naively use these environment variables.

**Recommendation:** Document that hook scripts must treat `TOOL_ARGS_JSON` and `TOOL_PATH` as untrusted input. Consider sanitizing or length-limiting these values.

---

### 10. `agent/harness/` (15 files)

**Files:** `C:\Desktop\MiniCode\backend\agent\harness\*.py`

The harness subsystem is well-designed with clean separation of concerns:
- `contracts.py` -- frozen dataclasses for ToolSpec, SearchPlan, EvidenceRecord, etc.
- `catalog.py` -- tool metadata resolution
- `guardrails.py` -- progressive tool-call loop detection (warn -> block -> halt)
- `control.py` -- control tool routing (ask_user, skill management)
- `projection.py` -- UI label generation
- `issues.py` -- tool error classification
- `answer_gate.py` -- final-answer integrity checks
- `search_plan.py` -- temporal query normalization
- `toolsets.py` -- toolset visibility policy
- `repair.py` -- missing argument repair engine
- `resources.py` -- implicit reference resolution
- `mcp_adapter.py` -- MCP tool classification
- `guidance.py` -- per-turn runtime guidance generation
- `_common.py` -- shared constants
- `schema.py` -- model-facing schema post-processing

#### [MEDIUM] Issue #18: Silent exception swallowing in `_schema_required_args`

**`catalog.py` lines 40-42:**
```python
try:
    schema = tool.get_schema()
except Exception:
    return ()
```

If `get_schema()` raises, the tool is treated as having no required arguments. This could allow malformed tool calls to proceed without repair, leading to confusing downstream errors.

---

#### [LOW] Issue #24: Hardcoded timezone `Asia/Shanghai`

**`search_plan.py` line 21:**
```python
def current_temporal_anchor(timezone: str = "Asia/Shanghai") -> tuple[str, str]:
```

Users outside this timezone get incorrect date stamps in search queries unless the caller explicitly passes a timezone. The `build_search_plan` function also defaults to `Asia/Shanghai`.

---

#### [LOW] Issue #25: MD5 used for result hashing in guardrails

**`guardrails.py` line 521:**
```python
result_hash = hashlib.md5(result_raw.encode()).hexdigest()[:12]
```

MD5 is used for deduplicating idempotent tool results. While not a security concern (not used for authentication), truncated MD5 has collision risks. A truncated SHA-256 would be more robust.

---

### 11. `approvals/` (models + manager)

**Files:** `C:\Desktop\MiniCode\backend\approvals\models.py`, `C:\Desktop\MiniCode\backend\approvals\manager.py`

#### [MEDIUM] Issue #20: `ApprovalSummary` always reports `approved=0, rejected=0`

**`models.py` lines 155-160:**
```python
@classmethod
def from_approvals(cls, approvals: list[ProductApproval]) -> ApprovalSummary:
    return cls(
        total=len(approvals),
        pending=len(approvals),  # All in the list are pending
        approved=0,
        rejected=0,
    )
```

The summary statistics are always wrong for historical reporting. They only reflect the current pending queue. The `approved` and `rejected` fields are dead code -- they can never be non-zero.

---

#### [LOW] Issue #26: Silent callback swallowing

**`manager.py` line 134:**
```python
except Exception:
    pass
```

If `_on_approval_update` raises, the approval is still created but the UI is never notified. The approval appears in the internal list but the frontend doesn't show it.

---

### 12. `tasks/` (manager + scheduler)

**Files:** `C:\Desktop\MiniCode\backend\tasks\manager.py`, `C:\Desktop\MiniCode\backend\tasks\scheduler.py`

The `TaskManager` is well-implemented with proper pruning, TTL-based eviction, and terminal state tracking. The `TaskScheduler` has a clean cron parser and persistence model.

**Key issue:** See Issue #5 above -- the scheduler is never actually started as a running service, making scheduled tasks non-functional.

---

### 13. `permissions/` (checker, context, profiles, rules, network)

**Files:** `C:\Desktop\MiniCode\backend\permissions\*.py`

The permission system is well-designed with a four-level model (AUTO/CONFIRM/DIFF_REVIEW/ALWAYS_DENY), path-based whitelist/blacklist, and wildcard matching.

#### [LOW] Issue #27: `inspect.signature` on every permission check

**`checker.py` lines 78-86:**
```python
def check_permission_level(checker, tool_name, args, *, context, tool):
    try:
        import inspect
        accepts_tool = "tool" in inspect.signature(checker.check).parameters
    except (TypeError, ValueError):
        accepts_tool = True
    ...
```

`inspect.signature` is called on every single permission check. This adds overhead to every tool call. The result should be cached per checker instance.

---

#### [LOW] Issue #28: Limited catastrophic command blocklist

**`checker.py` lines 33-47:** Only ~13 patterns are covered. Many dangerous commands are not blocked:
- `chmod -R 777 /`
- Python/Perl fork bombs
- `> /dev/sda` (only `/dev/sd` prefix is matched, missing NVMe drives `/dev/nvme*`)
- `iptables -F` (firewall flush)
- `systemctl stop firewalld`

---

### 14. `bootstrap/app.py` (220 lines)

**File:** `C:\Desktop\MiniCode\backend\bootstrap\app.py`

#### [MEDIUM] Issue #19: All startup failures silently degrade

**Lines 63-149:** Every subsystem (memory, skills, RAG, MCP, hooks) is wrapped in try/except that logs a warning and continues:
```python
try:
    self.file_memory = FileMemory()
except Exception as exc:
    logger.warning("File memory init failed: %s", exc)
```

If multiple subsystems fail, the application starts in a degraded state with no clear indication to the user. There is no aggregated startup health signal.

**Recommendation:** Collect startup failures and expose them via the health endpoint and the initial `session.restored` event.

---

### 15. `terminal/session.py` (400+ lines)

**File:** `C:\Desktop\MiniCode\backend\terminal\session.py`

The terminal session manager is well-implemented with proper process lifecycle, output buffering, and cross-platform support.

**Minor issues:**
- `_output_buffer` can theoretically grow beyond `_MAX_OUTPUT_BUFFER_CHARS` if flushes fail silently.
- Windows process killing via `taskkill /PID /T /F` doesn't handle edge cases where child processes spawn new children during the kill sequence.

---

### 16. `ws/handlers/` (conversation, session, misc, workspace, diff, preview)

**Files:** `C:\Desktop\MiniCode\backend\ws\handlers\*.py`

These handler modules are well-structured with consistent patterns:
- Each exports a `HANDLERS` dict mapping command names to async functions.
- Error handling is consistent with `AgentEvent.error()` responses.
- Conversation CRUD operations properly handle not-found cases.

**Key issues found in handlers:**
- See Issue #3 (terminal resize stub)
- See Issue #4 (terminal.exec permission bypass)
- See Issue #5 (scheduler fresh instantiation)

---

## End-to-End Flow Analysis

### Does the approval flow work end-to-end?

**Verdict: Mostly yes, with caveats.**

The approval flow (`_approval_handler` -> `_pending_approvals` future -> `_resolve_pending_approval`) is correctly implemented:
- Proper async future-based blocking with 5-minute timeout
- Cancellation on WebSocket disconnect
- Session-level caching for "remember for session" approvals
- Diff payload deferral for large patches

Caveats:
1. The 5-minute timeout has no proactive client notification (the UI may show a dead approval dialog).
2. The session approval cache never shrinks (memory leak).
3. Stale approvals are silently dropped (debug log only).

### Does the permission system correctly gate tool execution?

**Verdict: Yes, with one exception.**

The permission checker correctly evaluates tool names against mode, session overrides, deny rules, and filesystem constraints. The four-level model is consistently applied across the agent loop. The exception is `terminal.exec` which does not implement the CONFIRM approval flow.

### Are conversations properly persisted and restored?

**Verdict: Yes, but fragile under concurrency.**

Conversations are correctly persisted as three separate files (meta JSON, transcript JSONL, snapshot JSON). The LRU cache is functional. The restoration flow properly rebuilds context from snapshots and handles missing transcripts by reconstructing from snapshot history. The lack of file locking is the primary risk.

### Is the WebSocket session lifecycle correct?

**Verdict: Mostly yes.**

The connection generation pattern correctly handles reconnection (old generation events are dropped). The delayed cleanup (30s) allows reconnection. Run-task tracking per conversation is correct. The `_conversation_run_locks` memory leak and unbounded task creation are the primary concerns.

### Are there memory leaks?

**Verdict: Yes, several identified.**

| Data Structure | Location | Cleaned? |
|---|---|---|
| `_conversation_run_locks` | handler.py:154 | Never |
| `_session_approval_cache` | approval_runtime.py:21 | Never |
| `_approval_diff_cache` | handler.py:149 | Only on approval resolution |
| `_pending_approval_payloads` | handler.py:148 | Only on approval resolution |
| `_conversation_streams` | handler.py:161 | In run `finally` block (OK) |
| `_conversation_run_tasks` | handler.py:152 | In cleanup callback (OK) |

---

## Cross-Cutting Observations

### 253 bare `except Exception` blocks

The grep for `except Exception.*:` returned 253 matches across the codebase. While many are appropriate (logging and continuing), a significant number silently swallow errors with `pass` or only `logger.debug`. This makes debugging production issues extremely difficult.

### No structured logging or correlation IDs

Log messages use string formatting without correlation IDs. In a multi-session environment, it is impossible to correlate log entries to a specific user session or conversation without manually matching session IDs scattered across different log formats.

### Mixed sync/async patterns in ConversationRepository

The `ConversationRepository` is a synchronous class called from async handlers. File I/O happens on the event loop thread. For large transcripts (thousands of messages), this blocks the event loop for tens of milliseconds per operation.

---

## Recommendations Priority List

1. **Fix the scheduler** (Issue #5) -- Scheduled tasks are non-functional. Create a singleton scheduler during bootstrap and start its loop.
2. **Implement `terminal.resize`** (Issue #3) -- Terminal UX is broken after resize.
3. **Fix `terminal.exec` permissions** (Issue #4) -- Either implement CONFIRM flow or document ALWAYS_DENY behavior.
4. **Add file locking to ConversationRepository** (Issue #7) -- Prevent data corruption.
5. **Bound or clean up memory-leaking caches** (Issues #2, #6, #8, #9) -- Add TTL-based eviction or cleanup on conversation deletion.
6. **Add concurrency limit to WS command tasks** (Issue #1) -- Prevent event loop starvation.
7. **Emit `approval.cancelled` on timeout** -- Fix the dead approval dialog UX.
8. **Log exceptions in `_hydrate_snapshot`** (Issue #14) -- Fix the perpetual loading state.
9. **Move ConversationRepository I/O to `asyncio.to_thread`** (Issue #15) -- Prevent event loop stalls.
10. **Collect and expose startup failures** (Issue #19) -- Give users visibility into degraded state.
