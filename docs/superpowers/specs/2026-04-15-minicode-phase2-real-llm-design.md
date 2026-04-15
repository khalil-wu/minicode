# MiniCode Phase 2 Real LLM Integration Design

**Goal**

Replace the direct-response path of the current fake agent with a real Lucen/OpenAI Responses API integration while keeping the existing `/api/chat` response structure unchanged.

**Scope**

This phase does not implement model-driven tool calling. It only upgrades plain chat replies from `FakeLLM` to a real LLM client. Existing fake tools and tool-path tests remain as local agent infrastructure.

## 1. Why this phase exists

Phase 1 proved the backend agent skeleton with a deterministic fake model and fake tools. The next smallest useful step is to connect a real model without changing too many variables at once.

This phase is intentionally narrow:

1. Keep the current API contract stable
2. Keep the current loop shape stable
3. Add a real LLM adapter behind a small boundary
4. Avoid mixing “real model integration” with “real tool orchestration”

## 2. Chosen approach

Use a **minimal adapter layer**.

### Rejected alternatives

`Direct inline API call in loop`

- Fastest to type
- Bad boundary: loop, settings, network, and response parsing get mixed together

`Full abstraction with multiple interchangeable providers and strategy objects`

- Cleaner long-term
- Too much overhead for the current learning stage

### Selected option

Add a small `settings` module and a small `real_llm` module, then route only the plain chat path through the real client.

This keeps the current code understandable and keeps future refactors cheap.

## 3. Behavior boundary

The `/api/chat` response shape stays the same:

- `reply`
- `stopped_reason`
- `iterations`
- `tool_calls`

This phase changes only how `reply` is produced for plain chat input.

### Routing rule

- Plain chat messages use the real LLM
- Existing fake tool-trigger messages such as `use echo:` and `summarize:` may continue using the fake path for now

This split is deliberate. It avoids combining two problems in one phase:

1. real model connectivity
2. model-decided tool execution

## 4. Module boundaries

### 4.1 Local settings loader

File: `agent/settings.py`

Responsibilities:

- Read local environment configuration
- Validate required values for real LLM usage
- Keep secrets out of committed project files

Expected settings:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_REASONING_EFFORT`

The API key must come from environment variables or a locally ignored file.

### 4.2 Real LLM adapter

File: `agent/real_llm.py`

Responsibilities:

- Build the OpenAI client against the configured base URL
- Call the Responses API
- Extract the text response into a simple return shape for the agent loop

This module should not know anything about FastAPI routes or agent state mutation.

### 4.3 Agent loop integration

File: `agent/loop.py`

Responsibilities:

- Keep the loop contract unchanged
- Route plain chat messages to the real LLM adapter
- Keep the fake tool path working for existing tests and learning flow

This phase should modify the loop minimally rather than redesigning it.

## 5. Configuration strategy

This phase uses **local-only configuration**, not committed project configuration.

### Local source of truth

- Environment variables
- Optionally a locally ignored `.env`

### Explicitly not allowed

- Committing the API key into source files
- Committing the API key into TOML or JSON config tracked by Git

This keeps the repository safe while still making local development simple.

## 6. Error handling

This phase only needs three user-facing error categories.

### 6.1 Missing configuration

Example:

- `OPENAI_API_KEY` is absent

Behavior:

- Return a readable configuration error through the existing response structure
- Do not attempt a network request

### 6.2 Upstream request failure

Examples:

- authentication error
- timeout
- provider 4xx/5xx

Behavior:

- Catch the failure in the adapter or loop boundary
- Return a readable upstream failure message
- Mark the response as a non-completed stop reason

### 6.3 Empty model output

Example:

- Responses API succeeds but no usable text is extracted

Behavior:

- Return a clear fallback error instead of pretending the request succeeded

## 7. Testing strategy

This phase keeps tests mostly offline and deterministic.

### 7.1 Settings tests

File: `tests/agent/test_settings.py`

Purpose:

- Verify env variable loading and validation

### 7.2 Real client tests

File: `tests/agent/test_real_llm.py`

Purpose:

- Verify response parsing without requiring a real network call
- Mock the OpenAI client call boundary

### 7.3 Loop and API tests

Files:

- `tests/agent/test_loop.py`
- `tests/test_chat_api.py`

Purpose:

- Keep the response contract unchanged
- Verify direct chat can be routed through a real-client interface
- Keep fake tool tests intact

## 8. Git breakdown

This phase should be split into three commits:

1. `feat: add local llm settings loader`
2. `feat: add responses-api llm client`
3. `feat: route direct chat through real llm`

This order mirrors the architecture:

- config
- adapter
- integration

## 9. Success criteria

This phase is complete when:

1. Plain `/api/chat` messages can use the configured Lucen/OpenAI Responses API
2. The API response structure is unchanged
3. Secrets are not committed to the repository
4. Tests for settings, adapter behavior, and route contract pass
5. Fake tool-path behavior remains available for now

## 10. Next phase

After this phase, the next logical step is:

- allow the real model to decide whether to call a tool

That future phase should be separate, because it changes agent behavior much more than this one.
