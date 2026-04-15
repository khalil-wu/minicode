# MiniCode Phase 2 Real LLM Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route plain chat requests through a real Lucen/OpenAI Responses API client while preserving the existing `/api/chat` response contract.

**Architecture:** Add a local settings loader, add a small real-LLM adapter around the Responses API, then integrate it into the existing loop without redesigning the tool path. Keep secrets in local-only configuration and keep tests deterministic by mocking the client boundary.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, pytest, OpenAI Python SDK, environment variables

---

## Planned File Structure

- Modify: `.gitignore` - Ignore local env/config files used for secrets.
- Modify: `pyproject.toml` - Add runtime dependency for the OpenAI Python SDK.
- Create: `agent/settings.py` - Read and validate local LLM configuration.
- Create: `agent/real_llm.py` - Call the Responses API and normalize text output.
- Modify: `agent/loop.py` - Route direct chat requests through the real client.
- Create: `tests/agent/test_settings.py` - Verify configuration loading behavior.
- Create: `tests/agent/test_real_llm.py` - Verify response parsing behavior with a mocked client.
- Modify: `tests/agent/test_loop.py` - Verify direct path integration with a stub real client while keeping tool-path behavior.
- Modify: `tests/test_chat_api.py` - Verify API output shape remains stable when direct chat uses the real path.

### Task 1: Add Local LLM Settings Loader

**Files:**
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Create: `agent/settings.py`
- Create: `tests/agent/test_settings.py`

- [ ] **Step 1: Write the failing settings tests**

```python
from agent.settings import SettingsError, load_llm_settings


def test_load_llm_settings_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://lucen.cc")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")

    settings = load_llm_settings()

    assert settings.api_key == "test-key"
    assert settings.base_url == "https://lucen.cc"
    assert settings.model == "gpt-5.4"
    assert settings.reasoning_effort == "high"


def test_load_llm_settings_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://lucen.cc")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")

    try:
        load_llm_settings()
    except SettingsError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected SettingsError when API key is missing")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agent/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.settings'`

- [ ] **Step 3: Write the minimal settings implementation**

```python
# agent/settings.py
from dataclasses import dataclass
import os


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str


def load_llm_settings() -> LLMSettings:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5.4").strip()
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "high").strip()

    if not api_key:
        raise SettingsError("Missing OPENAI_API_KEY")

    return LLMSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=reasoning_effort,
    )
```

```gitignore
# append to .gitignore
.env
config.local.toml
```

```toml
# append runtime dependency in pyproject.toml
"openai>=1.75,<2.0",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/agent/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml agent/settings.py tests/agent/test_settings.py
git commit -m "feat: add local llm settings loader"
```

### Task 2: Add Responses-API LLM Client

**Files:**
- Create: `agent/real_llm.py`
- Create: `tests/agent/test_real_llm.py`

- [ ] **Step 1: Write the failing real client tests**

```python
from types import SimpleNamespace

from agent.real_llm import RealLLMClient
from agent.settings import LLMSettings


def test_real_llm_returns_output_text() -> None:
    fake_response = SimpleNamespace(output_text="hello from model")

    class FakeResponses:
        def create(self, **kwargs):
            return fake_response

    class FakeOpenAI:
        def __init__(self):
            self.responses = FakeResponses()

    client = RealLLMClient(
        settings=LLMSettings(
            api_key="key",
            base_url="https://lucen.cc",
            model="gpt-5.4",
            reasoning_effort="high",
        ),
        openai_client=FakeOpenAI(),
    )

    reply = client.generate_reply("hello")

    assert reply == "hello from model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agent/test_real_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.real_llm'`

- [ ] **Step 3: Write the minimal client implementation**

```python
# agent/real_llm.py
from openai import OpenAI

from agent.settings import LLMSettings, load_llm_settings


class RealLLMClient:
    def __init__(
        self,
        settings: LLMSettings | None = None,
        openai_client: OpenAI | None = None,
    ) -> None:
        self.settings = settings or load_llm_settings()
        self.client = openai_client or OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
        )

    def generate_reply(self, message: str) -> str:
        response = self.client.responses.create(
            model=self.settings.model,
            input=message,
            reasoning={"effort": self.settings.reasoning_effort},
        )

        text = getattr(response, "output_text", "") or ""
        text = text.strip()
        if not text:
            raise RuntimeError("Model returned empty output")
        return text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/agent/test_real_llm.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/real_llm.py tests/agent/test_real_llm.py
git commit -m "feat: add responses-api llm client"
```

### Task 3: Route Direct Chat Through Real LLM

**Files:**
- Modify: `agent/loop.py`
- Modify: `tests/agent/test_loop.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write the failing integration tests**

```python
# add to tests/agent/test_loop.py
from agent.fake_llm import FakeLLM, ModelDecision


class StubRealLLM:
    def generate_reply(self, message: str) -> str:
        return f"REAL: {message}"


def test_run_agent_loop_uses_real_llm_for_plain_chat() -> None:
    response = run_agent_loop(
        message="hello",
        max_iterations=3,
        real_llm=StubRealLLM(),
    )

    assert response.reply == "REAL: hello"
    assert response.stopped_reason == "completed"
    assert response.iterations == 1


def test_run_agent_loop_keeps_fake_tool_path() -> None:
    response = run_agent_loop(
        message="use echo: hi",
        max_iterations=3,
        real_llm=StubRealLLM(),
    )

    assert response.reply == "I used a tool and got: hi"
    assert response.tool_calls[0].tool_name == "echo"
```

```python
# replace test in tests/test_chat_api.py
from fastapi.testclient import TestClient

import app as app_module


class StubRealLLM:
    def generate_reply(self, message: str) -> str:
        return f"REAL: {message}"


def test_post_chat_returns_agent_response(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DEFAULT_REAL_LLM", StubRealLLM())
    client = TestClient(app_module.app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "max_iterations": 3},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "REAL: hello"
    assert response.json()["stopped_reason"] == "completed"
    assert response.json()["tool_calls"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_loop.py tests/test_chat_api.py -v`
Expected: FAIL because `run_agent_loop()` does not yet accept `real_llm` and the API does not inject it

- [ ] **Step 3: Write the minimal integration**

```python
# key changes to agent/loop.py
from agent.real_llm import RealLLMClient


def run_agent_loop(
    message: str,
    max_iterations: int,
    llm: FakeLLM | None = None,
    tool_registry: ToolRegistry | None = None,
    real_llm: RealLLMClient | None = None,
) -> ChatResponse:
    active_llm = llm or FakeLLM()
    active_registry = tool_registry or build_default_tool_registry()
    active_real_llm = real_llm
    state = AgentState(user_message=message, max_iterations=max_iterations)

    if not (
        message.lower().startswith("use echo:")
        or message.lower().startswith("summarize:")
        or message.lower().startswith("use missing tool:")
    ):
        try:
            reply = (active_real_llm or RealLLMClient()).generate_reply(message)
        except Exception as exc:
            return ChatResponse(
                reply=f"LLM request failed: {exc}",
                stopped_reason="tool_error",
                iterations=1,
                tool_calls=[],
            )

        return ChatResponse(
            reply=reply,
            stopped_reason="completed",
            iterations=1,
            tool_calls=[],
        )

    # keep the existing fake tool loop below
```

```python
# key changes to app.py
from agent.real_llm import RealLLMClient


DEFAULT_REAL_LLM = RealLLMClient()


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return run_agent_loop(
        message=request.message,
        max_iterations=request.max_iterations,
        real_llm=DEFAULT_REAL_LLM,
    )
```

- [ ] **Step 4: Run full verification**

Run: `python -m pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py agent/loop.py tests/agent/test_loop.py tests/test_chat_api.py
git commit -m "feat: route direct chat through real llm"
```
