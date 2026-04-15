# MiniCode Phase 1 Backend Agent Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal FastAPI backend that exposes a `/api/chat` endpoint backed by a testable agent loop with a fake model and fake tools.

**Architecture:** Keep the first phase intentionally small and layered. FastAPI owns HTTP concerns, `schemas.py` owns request and response contracts, `agent/` owns loop state and behavior, and tests verify each layer independently before the endpoint is wired together.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, Uvicorn, pytest, httpx

---

## Planned File Structure

- Create: `.gitignore` - Ignore Python cache, virtual environments, pytest cache, and local environment files.
- Create: `pyproject.toml` - Store project metadata, runtime dependencies, and pytest configuration.
- Create: `app.py` - Create the FastAPI application and later expose `/api/chat`.
- Create: `schemas.py` - Define `ChatRequest`, `ChatResponse`, and `ToolCallRecord`.
- Create: `agent/__init__.py` - Mark `agent/` as a package.
- Create: `agent/state.py` - Define the mutable state carried through the loop.
- Create: `agent/fake_llm.py` - Define the deterministic fake model and its decision shape.
- Create: `agent/tools.py` - Define the tool registry and default fake tools.
- Create: `agent/loop.py` - Implement the minimal loop that calls the fake model and fake tools.
- Create: `tests/test_app_bootstrap.py` - Verify the FastAPI app boots successfully.
- Create: `tests/test_schemas.py` - Verify request and response model defaults and serialization.
- Create: `tests/agent/test_fake_llm.py` - Verify fake model decisions are deterministic.
- Create: `tests/agent/test_tools.py` - Verify tool registry behavior and fake tool outputs.
- Create: `tests/agent/test_loop.py` - Verify loop success, tool usage, and invalid tool exit.
- Create: `tests/test_chat_api.py` - Verify the API route returns the loop response as JSON.
- Create: `docs/step-by-step/01-project-skeleton.md` - Explain the first milestone in learner-friendly language.
- Create: `docs/step-by-step/02-schemas.md` - Explain the API contract milestone.
- Create: `docs/step-by-step/03-fake-llm-and-tools.md` - Explain the fake model and tools milestone.
- Create: `docs/step-by-step/04-agent-loop-endpoint.md` - Explain the loop and endpoint milestone.

### Task 1: Initialize The Backend Skeleton

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `app.py`
- Create: `agent/__init__.py`
- Create: `tests/test_app_bootstrap.py`
- Create: `docs/step-by-step/01-project-skeleton.md`

- [ ] **Step 1: Write the failing bootstrap test**

```python
from fastapi.testclient import TestClient

from app import app


def test_openapi_is_available() -> None:
    client = TestClient(app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "MiniCode Agent API"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_bootstrap.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Write the minimal project files**

```toml
# pyproject.toml
[project]
name = "minicode"
version = "0.1.0"
description = "MiniCode backend agent loop prototype"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115,<1.0",
  "uvicorn>=0.32,<1.0",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.28,<1.0",
  "pytest>=8.3,<9.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

```gitignore
# .gitignore
__pycache__/
.pytest_cache/
.venv/
venv/
*.pyc
*.pyo
*.pyd
.env
```

```python
# app.py
from fastapi import FastAPI


app = FastAPI(title="MiniCode Agent API")
```

```python
# agent/__init__.py
"""MiniCode agent package."""
```

- [ ] **Step 4: Run the bootstrap test to verify it passes**

Run: `python -m pytest tests/test_app_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Write the learning document for milestone 1**

```markdown
# 01 Project Skeleton

## 做了什么

- 初始化了 Python 后端项目的最小目录结构
- 创建了 `FastAPI` 应用入口 `app.py`
- 添加了项目依赖定义和基础测试配置

## 为什么先做这一步

Agent 项目一开始最容易乱的地方，是还没确定结构就急着写逻辑。先把应用入口、依赖和测试跑通，相当于先把施工现场搭好。

## 涉及文件

- `pyproject.toml`
- `.gitignore`
- `app.py`
- `agent/__init__.py`
- `tests/test_app_bootstrap.py`

## 这一步在 Agent 项目里的作用

它还没有真正实现 agent，但是它建立了所有后续能力的落点：Web 入口、Python 包结构、测试运行方式。
```

- [ ] **Step 6: Commit**

```bash
git add .gitignore pyproject.toml app.py agent/__init__.py tests/test_app_bootstrap.py docs/step-by-step/01-project-skeleton.md
git commit -m "chore: initialize backend project skeleton"
```

### Task 2: Add Chat Schemas

**Files:**
- Create: `schemas.py`
- Create: `tests/test_schemas.py`
- Create: `docs/step-by-step/02-schemas.md`

- [ ] **Step 1: Write the failing schema tests**

```python
from schemas import ChatRequest, ChatResponse, ToolCallRecord


def test_chat_request_defaults() -> None:
    request = ChatRequest(message="hello")

    assert request.message == "hello"
    assert request.max_iterations == 3


def test_chat_response_serializes_tool_calls() -> None:
    response = ChatResponse(
        reply="done",
        stopped_reason="completed",
        iterations=2,
        tool_calls=[
            ToolCallRecord(
                tool_name="echo",
                tool_input={"text": "hi"},
                tool_output="hi",
                status="success",
            )
        ],
    )

    payload = response.model_dump()

    assert payload["reply"] == "done"
    assert payload["tool_calls"][0]["tool_name"] == "echo"
    assert payload["tool_calls"][0]["status"] == "success"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schemas'`

- [ ] **Step 3: Write the schema models**

```python
# schemas.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str | None = None
    status: Literal["success", "error"] = "success"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    max_iterations: int = Field(default=3, ge=1, le=10)


class ChatResponse(BaseModel):
    reply: str
    stopped_reason: Literal[
        "completed",
        "tool_error",
        "invalid_model_action",
        "max_iterations",
    ]
    iterations: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
```

- [ ] **Step 4: Run the schema tests to verify they pass**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Write the learning document for milestone 2**

```markdown
# 02 Schemas

## 做了什么

- 定义了聊天请求模型 `ChatRequest`
- 定义了聊天响应模型 `ChatResponse`
- 定义了工具调用记录模型 `ToolCallRecord`

## 为什么这一步很重要

在 agent 项目里，很多混乱来自“数据到底长什么样”没有先说清楚。先把 schema 定好，可以把后续 loop、API、测试都绑定到同一份契约上。

## 涉及文件

- `schemas.py`
- `tests/test_schemas.py`

## 这一步在 Agent 项目里的作用

它给后端规定了统一的数据语言。后面不管是 loop 还是 API，都围绕这一套输入输出结构展开。
```

- [ ] **Step 6: Commit**

```bash
git add schemas.py tests/test_schemas.py docs/step-by-step/02-schemas.md
git commit -m "feat: add chat request and response schemas"
```

### Task 3: Implement The Fake LLM And Fake Tools

**Files:**
- Create: `agent/fake_llm.py`
- Create: `agent/tools.py`
- Create: `tests/agent/test_fake_llm.py`
- Create: `tests/agent/test_tools.py`
- Create: `docs/step-by-step/03-fake-llm-and-tools.md`

- [ ] **Step 1: Write the failing fake model and tool tests**

```python
# tests/agent/test_fake_llm.py
from agent.fake_llm import FakeLLM


def test_fake_llm_requests_echo_tool() -> None:
    llm = FakeLLM()

    decision = llm.decide(user_message="use echo: hello", tool_outputs=[])

    assert decision.action == "tool_call"
    assert decision.tool_name == "echo"
    assert decision.tool_input == {"text": "hello"}


def test_fake_llm_responds_after_tool_output() -> None:
    llm = FakeLLM()

    decision = llm.decide(
        user_message="use echo: hello",
        tool_outputs=["hello"],
    )

    assert decision.action == "respond"
    assert decision.response_text == "I used a tool and got: hello"
```

```python
# tests/agent/test_tools.py
from agent.tools import build_default_tool_registry


def test_echo_tool_returns_original_text() -> None:
    registry = build_default_tool_registry()

    result = registry.execute("echo", {"text": "hi"})

    assert result == "hi"


def test_summarize_tool_returns_word_count_preview() -> None:
    registry = build_default_tool_registry()

    result = registry.execute(
        "summarize_text",
        {"text": "one two three four five six"},
    )

    assert result == "Summary(6 words): one two three four five"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_fake_llm.py tests/agent/test_tools.py -v`
Expected: FAIL with `ModuleNotFoundError` for `agent.fake_llm` and `agent.tools`

- [ ] **Step 3: Write the fake model and tool registry**

```python
# agent/fake_llm.py
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ModelDecision:
    action: Literal["respond", "tool_call"]
    response_text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)


class FakeLLM:
    def decide(self, user_message: str, tool_outputs: list[str]) -> ModelDecision:
        lowered = user_message.lower()

        if tool_outputs:
            return ModelDecision(
                action="respond",
                response_text=f"I used a tool and got: {tool_outputs[-1]}",
            )

        if lowered.startswith("use echo:"):
            text = user_message.split(":", 1)[1].strip()
            return ModelDecision(
                action="tool_call",
                tool_name="echo",
                tool_input={"text": text},
            )

        if lowered.startswith("summarize:"):
            text = user_message.split(":", 1)[1].strip()
            return ModelDecision(
                action="tool_call",
                tool_name="summarize_text",
                tool_input={"text": text},
            )

        if lowered.startswith("use missing tool:"):
            return ModelDecision(
                action="tool_call",
                tool_name="missing_tool",
                tool_input={"text": user_message},
            )

        return ModelDecision(
            action="respond",
            response_text=f"Direct response: {user_message}",
        )
```

```python
# agent/tools.py
from collections.abc import Callable
from typing import Any


ToolHandler = Callable[[dict[str, Any]], str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        self._tools[name] = handler

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(self, name: str, payload: dict[str, Any]) -> str:
        return self._tools[name](payload)


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register("echo", lambda payload: str(payload.get("text", "")))

    def summarize_text(payload: dict[str, Any]) -> str:
        text = str(payload.get("text", "")).strip()
        words = text.split()
        preview = " ".join(words[:5]) if words else "(empty)"
        return f"Summary({len(words)} words): {preview}"

    registry.register("summarize_text", summarize_text)

    return registry
```

- [ ] **Step 4: Run the fake model and tool tests to verify they pass**

Run: `python -m pytest tests/agent/test_fake_llm.py tests/agent/test_tools.py -v`
Expected: PASS

- [ ] **Step 5: Write the learning document for milestone 3**

```markdown
# 03 Fake LLM And Tools

## 做了什么

- 实现了一个可预测的 `FakeLLM`
- 实现了 `ToolRegistry`
- 添加了两个模拟工具：`echo` 和 `summarize_text`

## 为什么不一开始接真模型

如果第一步就接真实 LLM，很多问题会混在一起：网络、密钥、提示词、随机性。先用假模型，可以把注意力集中在 agent loop 的结构上。

## 涉及文件

- `agent/fake_llm.py`
- `agent/tools.py`
- `tests/agent/test_fake_llm.py`
- `tests/agent/test_tools.py`

## 这一步在 Agent 项目里的作用

它让“模型决策”和“工具执行”第一次变成可单独测试的模块，这是后面拼出完整 loop 的前提。
```

- [ ] **Step 6: Commit**

```bash
git add agent/fake_llm.py agent/tools.py tests/agent/test_fake_llm.py tests/agent/test_tools.py docs/step-by-step/03-fake-llm-and-tools.md
git commit -m "feat: implement fake tool registry and fake llm"
```

### Task 4: Wire The Agent Loop And Chat Endpoint

**Files:**
- Create: `agent/state.py`
- Create: `agent/loop.py`
- Create: `tests/agent/test_loop.py`
- Create: `tests/test_chat_api.py`
- Create: `docs/step-by-step/04-agent-loop-endpoint.md`
- Modify: `app.py`

- [ ] **Step 1: Write the failing loop tests**

```python
from agent.loop import run_agent_loop


def test_run_agent_loop_returns_direct_response() -> None:
    response = run_agent_loop(message="hello", max_iterations=3)

    assert response.reply == "Direct response: hello"
    assert response.stopped_reason == "completed"
    assert response.iterations == 1
    assert response.tool_calls == []


def test_run_agent_loop_executes_tool_then_responds() -> None:
    response = run_agent_loop(message="use echo: hi", max_iterations=3)

    assert response.reply == "I used a tool and got: hi"
    assert response.stopped_reason == "completed"
    assert response.iterations == 2
    assert response.tool_calls[0].tool_name == "echo"
    assert response.tool_calls[0].tool_output == "hi"


def test_run_agent_loop_stops_on_invalid_tool_request() -> None:
    response = run_agent_loop(message="use missing tool: hi", max_iterations=3)

    assert response.stopped_reason == "invalid_model_action"
    assert response.reply == "Tool 'missing_tool' is not registered."
    assert response.iterations == 1
```

- [ ] **Step 2: Run the loop tests to verify they fail**

Run: `python -m pytest tests/agent/test_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.loop'`

- [ ] **Step 3: Write the agent state and loop implementation**

```python
# agent/state.py
from dataclasses import dataclass, field

from schemas import ToolCallRecord


@dataclass
class AgentState:
    user_message: str
    max_iterations: int
    iterations: int = 0
    reply: str = ""
    stopped_reason: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
```

```python
# agent/loop.py
from agent.fake_llm import FakeLLM
from agent.state import AgentState
from agent.tools import ToolRegistry, build_default_tool_registry
from schemas import ChatResponse, ToolCallRecord


def run_agent_loop(
    message: str,
    max_iterations: int,
    llm: FakeLLM | None = None,
    tool_registry: ToolRegistry | None = None,
) -> ChatResponse:
    active_llm = llm or FakeLLM()
    active_registry = tool_registry or build_default_tool_registry()
    state = AgentState(user_message=message, max_iterations=max_iterations)

    while state.iterations < state.max_iterations:
        state.iterations += 1
        decision = active_llm.decide(
            user_message=state.user_message,
            tool_outputs=state.tool_outputs,
        )

        if decision.action == "respond":
            state.reply = decision.response_text or ""
            state.stopped_reason = "completed"
            break

        if decision.action != "tool_call" or not decision.tool_name:
            state.reply = "Model returned an invalid action."
            state.stopped_reason = "invalid_model_action"
            break

        if not active_registry.has_tool(decision.tool_name):
            state.reply = f"Tool '{decision.tool_name}' is not registered."
            state.stopped_reason = "invalid_model_action"
            break

        try:
            output = active_registry.execute(
                decision.tool_name,
                decision.tool_input,
            )
        except Exception as exc:
            state.tool_calls.append(
                ToolCallRecord(
                    tool_name=decision.tool_name,
                    tool_input=decision.tool_input,
                    tool_output=str(exc),
                    status="error",
                )
            )
            state.reply = f"Tool '{decision.tool_name}' failed: {exc}"
            state.stopped_reason = "tool_error"
            break

        state.tool_calls.append(
            ToolCallRecord(
                tool_name=decision.tool_name,
                tool_input=decision.tool_input,
                tool_output=output,
                status="success",
            )
        )
        state.tool_outputs.append(output)
    else:
        state.reply = "Agent stopped after reaching the iteration limit."
        state.stopped_reason = "max_iterations"

    return ChatResponse(
        reply=state.reply,
        stopped_reason=state.stopped_reason or "max_iterations",
        iterations=state.iterations,
        tool_calls=state.tool_calls,
    )
```

- [ ] **Step 4: Run the loop tests to verify they pass**

Run: `python -m pytest tests/agent/test_loop.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing API smoke test**

```python
from fastapi.testclient import TestClient

from app import app


def test_post_chat_returns_agent_response() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "use echo: hi", "max_iterations": 3},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "I used a tool and got: hi"
    assert response.json()["stopped_reason"] == "completed"
    assert response.json()["tool_calls"][0]["tool_name"] == "echo"
```

- [ ] **Step 6: Run the API smoke test to verify it fails**

Run: `python -m pytest tests/test_chat_api.py -v`
Expected: FAIL with `assert 404 == 200`

- [ ] **Step 7: Update the FastAPI app to expose `/api/chat`**

```python
# app.py
from fastapi import FastAPI

from agent.loop import run_agent_loop
from schemas import ChatRequest, ChatResponse


app = FastAPI(title="MiniCode Agent API")


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return run_agent_loop(
        message=request.message,
        max_iterations=request.max_iterations,
    )
```

- [ ] **Step 8: Run the full test suite to verify it passes**

Run: `python -m pytest -v`
Expected: PASS for `tests/test_app_bootstrap.py`, `tests/test_schemas.py`, `tests/agent/test_fake_llm.py`, `tests/agent/test_tools.py`, `tests/agent/test_loop.py`, and `tests/test_chat_api.py`

- [ ] **Step 9: Write the learning document for milestone 4**

```markdown
# 04 Agent Loop Endpoint

## 做了什么

- 定义了 `AgentState`
- 实现了 `run_agent_loop()`
- 把 loop 接到了 `POST /api/chat`
- 为 loop 和 API 都补上了测试

## 为什么这一步是核心

前面三步都是在准备零件，这一步第一次把零件串起来，让项目具备了真正的 agent 闭环：输入、模型决策、工具执行、结果返回。

## 涉及文件

- `agent/state.py`
- `agent/loop.py`
- `app.py`
- `tests/agent/test_loop.py`
- `tests/test_chat_api.py`

## 这一步在 Agent 项目里的作用

从这里开始，MiniCode 不再只是一些模块，而是一个真正能工作的最小 agent 后端。
```

- [ ] **Step 10: Commit**

```bash
git add app.py agent/state.py agent/loop.py tests/agent/test_loop.py tests/test_chat_api.py docs/step-by-step/04-agent-loop-endpoint.md
git commit -m "feat: wire minimal agent loop endpoint"
```

## Self-Review Notes

### Spec coverage

- Backend-only FastAPI service: covered by Tasks 1 and 4.
- Request and response contracts: covered by Task 2.
- Fake tools and fake model: covered by Task 3.
- Agent state and minimal loop: covered by Task 4.
- Error paths for invalid model action and tool failure structure: covered by Task 4 loop logic.
- Tests for direct reply, tool usage, invalid tool, and API smoke path: covered by Task 4 plus Tasks 1 to 3.
- Step-by-step learning docs per milestone: covered by all four tasks.

### Placeholder scan

- No placeholder markers remain in the executable steps.
- Every task has exact file paths, code snippets, commands, and commit messages.

### Type consistency

- `ChatRequest.max_iterations` is used consistently in `app.py` and `run_agent_loop()`.
- `ToolCallRecord` fields match the loop logic and response assertions.
- `run_agent_loop(message, max_iterations)` matches both loop tests and API usage.
