from __future__ import annotations

from backend.agent.context import ContextBuilder
from backend.llm.capabilities import ProviderCapabilities


class _RecordingSkillExecutor:
    def __init__(self) -> None:
        self.flavors: list[str] = []

    def build_layer1_summary(
        self,
        *,
        max_chars: int | None = None,
        context_window_tokens: int | None = None,
    ) -> str:
        del max_chars, context_window_tokens
        self.flavors.append("minicode")
        return "catalog:minicode"


class _CapabilityLLM:
    def __init__(self, *, provider: str, wire_api: str) -> None:
        self.capabilities = ProviderCapabilities(
            provider=provider,
            wire_api=wire_api,
        )


def test_context_builder_keeps_canonical_catalog_for_anthropic_transport() -> None:
    executor = _RecordingSkillExecutor()
    builder = ContextBuilder(
        skill_executor=executor,
        llm=_CapabilityLLM(provider="custom", wire_api="anthropic"),
    )

    assert builder._build_skill_catalog() == "catalog:minicode"
    assert executor.flavors == ["minicode"]


def test_context_builder_uses_minicode_catalog_without_provider() -> None:
    executor = _RecordingSkillExecutor()
    builder = ContextBuilder(skill_executor=executor)

    assert builder._build_skill_catalog() == "catalog:minicode"
    assert executor.flavors == ["minicode"]


def test_context_builder_keeps_canonical_catalog_for_pi_transport() -> None:
    executor = _RecordingSkillExecutor()
    builder = ContextBuilder(
        skill_executor=executor,
        llm=_CapabilityLLM(provider="pi", wire_api="pi"),
    )

    assert builder._build_skill_catalog() == "catalog:minicode"
    assert executor.flavors == ["minicode"]
