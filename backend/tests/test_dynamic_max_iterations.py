from backend.agent.iteration_budget import (
    resolve_turn_max_iterations as _resolve_turn_max_iterations,
)
from backend.config import AgentSettings


def test_iteration_limit_always_respects_explicit_configuration() -> None:
    settings = AgentSettings(max_iterations=60)
    assert _resolve_turn_max_iterations(settings) == 60


def test_low_explicit_limit_is_preserved() -> None:
    settings = AgentSettings(max_iterations=5)
    assert _resolve_turn_max_iterations(settings) == 5


def test_default_host_limits_are_disabled_and_not_prompt_classified() -> None:
    settings = AgentSettings()

    assert settings.max_iterations == 0
    assert settings.max_tool_calls == 0
    assert settings.turn_error_budget == 0
    assert settings.max_turn_tokens == 0
    assert settings.max_turn_cost_usd == 0
    assert settings.max_turn_seconds == 0

    assert _resolve_turn_max_iterations(settings) == 0


def test_explicit_zero_and_negative_iteration_limits_are_disabled() -> None:
    assert _resolve_turn_max_iterations(AgentSettings(max_iterations=0)) == 0
    assert _resolve_turn_max_iterations(AgentSettings(max_iterations=-5)) == 0
