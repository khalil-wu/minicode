from copy import deepcopy

import pytest

from backend.agent.lifecycle_errors import LifecycleStaleError
from backend.agent.run_context import RunContext
from backend.extensions import ExtensionLoader, ExtensionRunner
from backend.llm import base


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_empty_request_handler_chain_keeps_payload_and_checks_generation(tmp_path, monkeypatch, bound):
    runner = ExtensionRunner(cwd=tmp_path)
    runtime = runner.for_execution(RunContext()) if bound else runner
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": str(index)}]} for index in range(1000)]}

    def unexpected_copy(_value):
        raise AssertionError("The empty request hook chain copied the payload")

    monkeypatch.setattr(base, "deepcopy", unexpected_copy)
    result = await base.emit_provider_lifecycle_request({"_lifecycle_runtime": runtime}, payload)
    assert result["messages"] is payload["messages"]
    assert runner.runtime.execution_run_context is None
    runner.invalidate()
    with pytest.raises(LifecycleStaleError):
        await base.emit_provider_lifecycle_request({"_lifecycle_runtime": runtime}, payload)


@pytest.mark.asyncio
async def test_loaded_request_handlers_still_own_nested_payload_and_run_in_order(tmp_path):
    seen = []

    def factory(api):
        def mutate(event, ctx):
            event["payload"]["messages"][0]["content"][0]["text"] += " first"
            seen.append(event["payload"])

        def replace(event, ctx):
            assert event["payload"]["messages"][0]["content"][0]["text"] == "original first"
            return {**event["payload"], "model": "changed"}

        api.on("before_provider_request", mutate)
        api.on("before_provider_request", replace)

    loaded = await ExtensionLoader(cwd=tmp_path).load_factory(factory)
    payload = {"model": "original", "messages": [{"role": "user", "content": [{"type": "text", "text": "original"}]}]}
    expected = deepcopy(payload)
    try:
        for _ in range(2):
            result = await base.emit_provider_lifecycle_request({"_lifecycle_runtime": loaded.runner.for_execution(RunContext())}, payload)
            assert result["model"] == "changed"
            assert result["messages"][0]["content"][0]["text"] == "original first"
            assert payload == expected
        assert seen[0]["messages"] is not seen[1]["messages"]
    finally:
        loaded.runner.invalidate()


@pytest.mark.asyncio
async def test_request_handler_registered_after_empty_call_gets_its_own_payload(tmp_path):
    registrations = []
    loaded = await ExtensionLoader(cwd=tmp_path).load_factory(lambda api: registrations.append(api))
    payload = {"messages": [{"role": "user", "content": "original"}]}
    metadata = {"_lifecycle_runtime": loaded.runner}
    try:
        first = await base.emit_provider_lifecycle_request(metadata, payload)
        assert first["messages"] is payload["messages"]

        def mutate(event, ctx):
            event["payload"]["messages"][0]["content"] = "late hook"

        registrations[0].on("before_provider_request", mutate)
        second = await base.emit_provider_lifecycle_request(metadata, payload)
        assert second["messages"][0]["content"] == "late hook"
        assert payload["messages"][0]["content"] == "original"
    finally:
        loaded.runner.invalidate()


@pytest.mark.asyncio
async def test_custom_request_runtime_without_handler_inventory_keeps_nested_isolation():
    class Runtime:
        async def emit_before_provider_request(self, payload):
            payload["messages"][0]["content"] = "hook mutation"

    payload = {"messages": [{"role": "user", "content": "original"}]}
    result = await base.emit_provider_lifecycle_request({"_lifecycle_runtime": Runtime()}, payload)
    assert payload["messages"][0]["content"] == "original"
    assert result["messages"][0]["content"] == "hook mutation"
