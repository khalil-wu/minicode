from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys
from pathlib import Path

import pytest

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.context import ContextBuilder
from backend.hooks.manager import HookManager
from backend.hooks.models import HookEvent
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.tests.test_committed_tool_scheduling import WorkTool, committed, run_case


def make_hooks(tmp_path: Path, *, post_async: bool = False, hold_post: bool = False) -> HookManager:
    script = tmp_path / "observer.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "root = pathlib.Path(__file__).parent\n"
        "item = json.load(sys.stdin)\n"
        "with (root / 'hook-inputs.jsonl').open('a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps(item) + '\\n')\n"
        f"if item['hook_event_name'] == 'PostModelRequest' and {hold_post!r}:\n"
        "    (root / 'post-started').touch()\n"
        "    while not (root / 'release-post').exists(): time.sleep(.01)\n"
        "print(json.dumps({'continue': False, 'systemMessage': 'observer-must-not-control'}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command, shell = f"& {quote(sys.executable)} {quote(script)}", "powershell"
    else:
        command, shell = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", "bash"
    pre = {"type": "command", "command": command, "shell": shell}
    post = {**pre, "async": post_async}
    return HookManager.from_settings({"hooks": {
        "PreModelRequest": [{"matcher": "ignored", "hooks": [pre]}],
        "PostModelRequest": [{"matcher": "ignored", "hooks": [post]}],
    }}, workspace_root=tmp_path)


async def wait_path(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)


def inputs(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "hook-inputs.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_retry_reports_each_attempt_and_observer_output_cannot_veto(tmp_path):
    hooks = make_hooks(tmp_path)

    class Provider(LLMAdapter):
        calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            assert "observer-must-not-control" not in str(messages)
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.ERROR, content="server temporarily unavailable",
                                  raw={"provider_error_type": "server", "status_code": 500})
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, usage=UsageInfo(input_tokens=42, output_tokens=3,
                                  cache_read_input_tokens=7, reasoning_output_tokens=1))

        async def simple_chat(self, messages):
            return ""

    provider = Provider()
    state, _, _ = await run_case(tmp_path, provider, [], hooks=hooks)
    await hooks.drain_async_hooks()
    recorded = inputs(tmp_path)
    assert provider.calls == 2 and state.terminal_status == "completed"
    assert [entry["hook_event_name"] for entry in recorded] == [
        "PreModelRequest", "PostModelRequest", "PreModelRequest", "PostModelRequest"]
    assert recorded[1]["status"] == "failed" and recorded[1]["error"] and recorded[1]["token_usage"] is None
    assert recorded[3]["status"] == "completed" and recorded[3]["error"] is None
    assert recorded[3]["token_usage"] == {"input_tokens": 42, "cached_input_tokens": 7,
        "cache_write_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 45}
    assert set(recorded[0]) == {"session_id", "turn_id", "cwd", "transcript_path", "model", "hook_event_name"}


@pytest.mark.asyncio
@pytest.mark.parametrize("post_async", [False, True])
async def test_post_hook_orders_real_tool_dispatch(tmp_path, post_async):
    hooks = make_hooks(tmp_path, post_async=post_async, hold_post=True)
    tool = WorkTool("observe_tool")
    stream_ready, finish_stream = asyncio.Event(), asyncio.Event()

    class Provider(LLMAdapter):
        calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                stream_ready.set()
                yield committed(tool.name, "call")
                await finish_stream.wait()
                yield StreamEvent(type=StreamEventType.TOOL_CALL,
                                  tool_calls=[ToolCallEvent(id="call", name=tool.name, arguments={})])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    running = asyncio.create_task(run_case(tmp_path, Provider(), [tool], hooks=hooks))
    await asyncio.wait_for(stream_ready.wait(), 5)
    if post_async:
        await asyncio.wait_for(tool.started.wait(), 5)
    else:
        await asyncio.sleep(.1)
        assert not tool.started.is_set()
    finish_stream.set()
    await wait_path(tmp_path / "post-started")
    if not post_async:
        assert not tool.started.is_set()
    (tmp_path / "release-post").touch()
    state, _, _ = await running
    await hooks.drain_async_hooks()
    assert state.terminal_status == "completed" and tool.executions == 1


@pytest.mark.asyncio
async def test_cancel_during_post_preserves_unexecuted_call_and_completed_usage(tmp_path):
    hooks = make_hooks(tmp_path, hold_post=True)
    tool = WorkTool("never_dispatch")
    cancel = asyncio.Event()

    class Provider(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            call = ToolCallEvent(id="cancelled-call", name=tool.name, arguments={"input": "do not execute"},
                                 native_type="custom_tool_call", native_name=tool.name)
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[call],
                              tool_calls_final=False, tool_calls_committed=True,
                              provider_items=[{"type": "custom_tool_call", "call_id": call.id,
                                               "name": tool.name, "input": "do not execute"}])
            yield StreamEvent(type=StreamEventType.TOOL_CALL,
                              tool_calls=[call])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls",
                              usage=UsageInfo(input_tokens=42))

        async def simple_chat(self, messages):
            return ""

    running = asyncio.create_task(run_case(tmp_path, Provider(), [tool], cancel_event=cancel, hooks=hooks))
    await wait_path(tmp_path / "post-started")
    cancel.set()
    state, ctx, events = await running
    assert state.terminal_status == "cancelled" and tool.executions == 0
    assert any(message.tool_call_id == "cancelled-call" and "aborted before dispatch" in message.content
               for message in ctx._history if message.role == "tool")
    assert any(event.type == "tool_result" and event.data.get("id") == "cancelled-call"
               and event.data.get("status") == "cancelled" for event in events)
    history = ExecutionJournal("schedule-test", base_dir=tmp_path / "journals").reconstruct_history()
    assistant = next(item for item in history if item.get("tool_calls"))
    assert assistant["tool_calls"][0]["arguments"] == {"input": "do not execute"}
    assert assistant["tool_calls"][0]["native_type"] == "custom_tool_call"
    assert any(item.get("tool_call_id") == "cancelled-call" and "aborted before dispatch" in item["content"]
               and item["is_error"] is True for item in history if item.get("role") == "tool")
    restored = ContextBuilder()
    restored.load_snapshot({"history": history})
    assert any(message.tool_call_id == "cancelled-call" and message.is_error
               for message in restored._history if message.role == "tool")
    assert inputs(tmp_path)[1]["status"] == "completed"
    assert inputs(tmp_path)[1]["token_usage"]["total_tokens"] == 42
    (tmp_path / "release-post").touch()
    await hooks.drain_async_hooks()


@pytest.mark.asyncio
async def test_cancel_during_stream_retains_deferred_call_and_observer(tmp_path):
    hooks = make_hooks(tmp_path)
    tool = WorkTool("cancel_before_response")
    cancel, emitted = asyncio.Event(), asyncio.Event()

    class Provider(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            yield committed(tool.name, "stream-call")
            emitted.set()
            await asyncio.Event().wait()

        async def simple_chat(self, messages):
            return ""

    running = asyncio.create_task(run_case(tmp_path, Provider(), [tool], cancel_event=cancel, hooks=hooks))
    await asyncio.wait_for(emitted.wait(), 5)
    cancel.set()
    state, ctx, events = await running
    await hooks.drain_async_hooks()
    assert state.terminal_status == "cancelled" and tool.executions == 0
    assert inputs(tmp_path)[1]["status"] == "cancelled"
    assert any(message.tool_call_id == "stream-call" and "aborted before dispatch" in message.content
               for message in ctx._history if message.role == "tool")
    assert any(event.type == "tool_result" and event.data.get("id") == "stream-call"
               and event.data.get("status") == "cancelled" for event in events)


@pytest.mark.asyncio
async def test_cancelled_provider_emits_cancelled_observer(tmp_path):
    hooks = make_hooks(tmp_path)
    cancel, started = asyncio.Event(), asyncio.Event()

    class Provider(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    running = asyncio.create_task(run_case(tmp_path, Provider(), [], cancel_event=cancel, hooks=hooks))
    await asyncio.wait_for(started.wait(), 5)
    cancel.set()
    state, _, _ = await running
    await hooks.drain_async_hooks()
    assert state.terminal_status == "cancelled"
    assert inputs(tmp_path)[1]["status"] == "cancelled" and inputs(tmp_path)[1]["token_usage"] is None


@pytest.mark.asyncio
async def test_memory_consolidation_runs_policy_observers_and_stop_but_not_user_hooks(tmp_path):
    hooks = make_hooks(tmp_path)
    for event in (HookEvent.PRE_MODEL_REQUEST, HookEvent.POST_MODEL_REQUEST):
        managed = copy.copy(hooks.hooks[event][0])
        managed.entry_id += "-policy"
        managed.source = "policy"
        hooks.hooks[event].append(managed)

    stop_script = tmp_path / "stop.py"
    stop_script.write_text(
        "import json, pathlib, sys\n"
        "data=json.load(sys.stdin)\n"
        "with (pathlib.Path(__file__).parent / 'managed-stop').open('a') as out: out.write('called\\n')\n"
        "print(json.dumps({'decision':'block', 'reason':'managed rejection'}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = f"& {quote(sys.executable)} {quote(stop_script)}"
    else:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(stop_script))}"
    user_stop = copy.copy(hooks.hooks[HookEvent.PRE_MODEL_REQUEST][0])
    user_stop.entry_id = "user-stop"
    user_stop.command = command
    managed_stop = copy.copy(user_stop)
    managed_stop.entry_id = "policy-stop"
    managed_stop.source = "policy"
    hooks.hooks[HookEvent.STOP] = [user_stop, managed_stop]

    class Provider(LLMAdapter):
        calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    provider = Provider()
    state, _, events = await run_case(tmp_path, provider, [], hooks=hooks,
                                     metadata={"internal_session_source": "memory_consolidation"})
    await hooks.drain_async_hooks()
    assert provider.calls == 1 and state.terminal_status == "failed"
    assert [item["hook_event_name"] for item in inputs(tmp_path)] == ["PreModelRequest", "PostModelRequest"]
    assert (tmp_path / "managed-stop").read_text().splitlines() == ["called"]
    assert any(event.type == "error" and "rejected by a Stop hook" in event.data["message"] for event in events)
