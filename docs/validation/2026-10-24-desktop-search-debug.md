# Desktop D-SEARCH / D-DEBUG — 2026-10-24

## Evidence and scope

- Source checkout: `.tmp/codex-src/codex-rs/thread-store/src/local/search_threads.rs`; `.tmp/codex-src/codex-rs/rollout/src/search.rs` selects user/assistant visible text and excerpts with 48 characters before / 96 after a literal, case-insensitive match.
- Installed desktop consumers: `output/full-parity-audit-20261023/readable/webview/assets/app-initial-ad16eaadf81f.js` (`searchThreads`, archived `E$c`); `command-menu-dialog-e763aa398ab8.js` merges loaded metadata with async `contentMatch` snippets. Archive `E$c` uses `thread/search`, archived=true, cursor, updated sort and actual host/project ownership.
- Installed `app-shared-2feb3d4c6fcc.js` (`JU.allowDebugMenu`) permits internal build flavors, excludes prod; native `main-CT5a1yCd.js` adds Toggle Debug Panel only under that gate, separately from devtools permission. MiniCode uses its existing Vite DEV build context for this gate. No new internal settings flag or speculative Developer menu was added. No live gate value or new GUI verification is claimed.

## Implemented chain

- Session-bound `GET /api/conversations/search` resolves the connected session's repository, honors `global_search`, main-chat/archived/project/sort filters, and returns public conversation summaries, message IDs, snippets and a real continuation cursor. Cursor ownership includes session, durable store, query and filters. Reads run in the existing HTTP threadpool.
- Existing command palette retains immediate title/goal matches, merges real body hits and snippets, and uses the existing producing-message reveal/history path. Existing archived search retains immediate title/project matches and its independent read-only reader; archive project and sort changes reach the API. No new search button was introduced.
- Requests debounce 200 ms. Query/filter/session changes, disabling and closing abort old requests; late or foreign-session results cannot replace the current results. Failures stay visible as actual errors. Normal chat switching, drafts and editor buffers are not replaced by search responses.
- Complete run/debug consumers are gated in release/prod builds. A focused, owned real Inspector record remains readable through its existing details renderer without the aggregate runtime/control-plane/advanced diagnostic UI. Process/tool/error data are retained. Diff approval opens the actual diff panel without opening an unrelated Inspector tab.
- SidebarRight consumes `capabilityFeatureEnabled(runtimeCapabilities, 'goals')` (default true) for the goal descriptor, renderer and expand affordance. Disabling does not change `rightStackTab`, clear `activeGoal`, discard open-tab metadata or delete the owner editor draft. Re-enabling restores the existing draft.

## Boundaries and performance fixture

- UI batches contain at most 50 body results; API accepts 1–200 results. Each HTTP request scans at most `min(200, max(50, limit * 4))` candidate conversations. Empty scan pages advance their own cursor. The client follows cursors to fill only its remaining 50-result quota, then later scroll/disclosure consumers request the next batch.
- Indexed history uses one forward stream per scanned conversation, skips non-user/assistant rows and stops at the first actual match. Cold zstd history is decoded forward only as required, without repeatedly reopening/inflating it per history page or reading private provider snapshots. In-flight assistant replacement is authoritative. Legacy/damaged generations reuse the existing public history reader/recovery contract. No new global search cache was added.
- The 1,000-message cold compressed fixture checks the first and 998th message, exactly one stream open, exactly `position + 1` indexed reads, and prohibits provider-checkpoint loading. The bounded non-match fixture checks 50 / 100 / 104 cumulative scans over successive real cursors. A client abort cancels further requests and rendering; an already admitted threadpool scan may finish its bounded HTTP chunk.

## Validation handoff

Added/updated necessary fixtures after the implementation batch:

- `backend/tests/test_conversation_search_chain.py`
- `frontend/src.v2/hooks/useConversationSearch.test.tsx`
- `frontend/src.v2/overlays/CommandPalette.test.tsx`
- `frontend/src.v2/overlays/ArchivedTab.test.tsx`
- `frontend/src.v2/shell/SidebarRight.test.tsx`

These cover old body-only hits, hidden/private fields, current partial projection, archived/project/sort/session cursor isolation, bounded scans and compressed reads, cursor quota, cancellation/late results, actual failure display, draft preservation, explicit DEV=false gating with real failed-record details, and goal draft restoration.

Status: READY for Root's unified checks. This agent did not run tests, build, GUI or real model requests. No pass is claimed; actual runtime/visual verification remains unrun. Existing parity ledger files and user configuration were not changed.

First unified integration run (Root): 201 passed, 1 failed. The existing broad `project_public_conversation_summary` always emits `context_snapshot`, including for metadata-only input. Search now projects its explicit 11-field identity/order/owner contract directly from `ConversationSummary`, avoiding unrelated summary/context/control fields; the TypeScript search contract matches this shape. The original private-checkpoint/read-bound assertions remain, with an exact result-key assertion added. Focused regression is delegated to Root; this agent did not run it.
