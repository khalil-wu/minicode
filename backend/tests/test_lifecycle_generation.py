import asyncio

import pytest

from backend.agent.lifecycle_generation import LifecycleGenerationState


def test_generation_publish_and_current_fence() -> None:
    state = LifecycleGenerationState("conversation-1")
    runtime = object()
    state.publish(
        runtime=runtime,
        loader=object(),
        registry=object(),
        workspace_key="workspace",
        fingerprint="fingerprint",
        result=object(),
        model_runtime=object(),
        model_registry=object(),
    )

    assert state.runtime is runtime
    assert state.is_current(runtime)
    assert not state.is_current(object())
    assert "runner" not in state


def test_generation_retirement_waits_for_owner_task() -> None:
    async def scenario() -> None:
        state = LifecycleGenerationState("conversation-1")
        release = asyncio.Event()
        owner = asyncio.create_task(release.wait())
        state.retire(
            runtime=object(),
            loader=object(),
            model_runtime=object(),
            reason="reload",
            clear_loader_cache=False,
            defer_until=owner,
        )

        assert state.take_ready_retirements() == []
        release.set()
        await owner
        ready = state.take_ready_retirements()
        assert len(ready) == 1
        assert state.take_ready_retirements() == []

    asyncio.run(scenario())


def test_generation_shutdown_fence_rejects_publication() -> None:
    state = LifecycleGenerationState("conversation-1")
    state.fence_shutdown()

    try:
        state.publish(
            runtime=object(),
            loader=object(),
            registry=object(),
            workspace_key="",
            fingerprint="",
            result=object(),
            model_runtime=object(),
            model_registry=object(),
        )
    except RuntimeError as exc:
        assert "shutdown" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("shutdown fence accepted a new generation")


def test_noncanonical_generation_state_is_rejected() -> None:
    from backend.ws.agent_runner import SessionAgentRunnerMixin

    class Host(SessionAgentRunnerMixin):
        pass

    host = Host()
    host._extension_runtime_states = {
        "conversation-1": {"runner": object()}
    }

    with pytest.raises(TypeError, match="not canonical"):
        host._extension_runtime_state("conversation-1")


def test_generation_owner_requires_conversation_id() -> None:
    from backend.ws.agent_runner import SessionAgentRunnerMixin

    class Host(SessionAgentRunnerMixin):
        pass

    with pytest.raises(ValueError, match="conversation id"):
        Host()._extension_runtime_state("")


def test_generation_state_does_not_depend_on_extension_loader() -> None:
    import backend.agent.lifecycle_generation as module

    assert not hasattr(module, "ExtensionLoader")
