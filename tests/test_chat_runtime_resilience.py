import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from backend.llm.base import LLMAdapter
from backend.main import app


def _receive_next_non_task_update(ws, *, max_attempts: int = 20) -> dict[str, object]:
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if payload.get("type") == "task.update":
            continue
        return payload
    raise AssertionError("did not receive a non task.update websocket event in time")


def test_cost_tracker_module_is_ascii_safe_and_importable() -> None:
    path = Path("backend/llm/cost_tracker.py")
    raw = path.read_bytes()

    assert b"\x00" not in raw

    spec = importlib.util.spec_from_file_location("test_cost_tracker_module", path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tracker = module.CostTracker.get_instance()
    tracker.reset()
    cost = tracker.record_usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        elapsed_sec=1.5,
        cost_usd=0.5,
    )

    assert cost == 0.5
    assert tracker.get_summary()["total_cost_usd"] == 0.5
    assert tracker.get_summary()["total_duration_sec"] == 1.5


def test_websocket_surfaces_unexpected_agent_run_failure(monkeypatch) -> None:
    async def _failing_agent_loop(*args, **kwargs):
        if False:
            yield None
        raise RuntimeError("boom from test")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _failing_agent_loop)

    # The shared conftest double returns a bare ``object()`` for the session
    # adapter. That no longer satisfies MiniCode's adapter contract: resolving
    # the run's thinking level now *requires* ``apply_reasoning_policy`` and
    # fails closed with a TypeError otherwise
    # (backend/llm/model_selection.py::apply_model_thinking_level). With a bare
    # object the run dies at LLM initialization
    # (done reason ``llm_initialization_failed``) and never reaches the runner
    # patched above, so the unexpected-run-failure path under test was never
    # exercised. Supply a conforming adapter instead.
    class _NeverCalledAdapter(LLMAdapter):
        async def stream_chat(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("the patched runner must be used instead")
            yield  # noqa: PLE0101 - keep this an async generator

        async def simple_chat(self, messages) -> str:  # pragma: no cover
            raise AssertionError("the patched runner must be used instead")

    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NeverCalledAdapter(),
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_agent_failure") as ws:
            assert _receive_next_non_task_update(ws)["type"] == "mcp_status"
            assert _receive_next_non_task_update(ws)["type"] == "llm.model.updated"

            ws.send_json({"type": "user_message", "content": "trigger failure"})

            error_event = None
            done_event = None
            for _ in range(20):
                payload = _receive_next_non_task_update(ws)
                if payload.get("type") == "error":
                    error_event = payload
                elif payload.get("type") == "done":
                    done_event = payload
                if error_event is not None and done_event is not None:
                    break

    assert error_event is not None
    assert "internal runtime processing failed" in str(error_event["message"])
    assert "boom from test" not in str(error_event["message"])
    assert done_event is not None
    assert done_event["status"] == "failed"
    assert done_event["reason"] == "runtime_error"
