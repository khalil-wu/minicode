"""Contracts for MiniCode's single runtime identity."""

import json
from pathlib import Path
from unittest.mock import Mock

import backend.config as config
from backend import config_helpers
from backend.agent.prompting import (
    PromptBuilderV2,
    build_stable_prompt,
    clear_system_prompt_sections,
)
from backend.agent.state import AgentState
from backend.agent.codex_prompts import codex_model_instructions
from backend.agent.context import ContextBuilder
from backend.llm.base import LLMAdapter


_RESOURCES = Path(__file__).parents[1] / "agent" / "codex_prompt_resources"


def test_actual_adapter_model_selects_the_official_base_without_a_second_overlay() -> None:
    adapter = Mock(spec=LLMAdapter)
    adapter.model_id.return_value = "gpt-5.6-sol"
    adapter.model_instructions.return_value = "Obsolete custom restrictions"
    context = ContextBuilder(llm=adapter)
    first = context._build_prompt_parts(AgentState(user_message="inspect"), None)
    assert first.stable == codex_model_instructions("gpt-5.6-sol")
    assert "Obsolete custom restrictions" not in first.render_system()
    adapter.model_id.return_value = "gpt-6-astra"
    second = context._build_prompt_parts(AgentState(user_message="inspect"), None)
    assert second.stable == codex_model_instructions("gpt-6-astra")


def test_unknown_model_uses_verbatim_official_fallback() -> None:
    official = (_RESOURCES / "prompt.md").read_bytes().decode("utf-8")
    assert build_stable_prompt(model_slug="unregistered-custom-model") == official
    assert codex_model_instructions("custom-model") == official
    assert "You are an agent for MiniCode" not in official
    assert "AGENTS.md" in official


def test_registered_model_prefers_its_official_catalog_template() -> None:
    catalog = json.loads((_RESOURCES / "model-instructions.json").read_text(encoding="utf-8"))
    for slug in ("gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-sol"):
        assert build_stable_prompt(model_slug=slug) == catalog[slug]["instructions_template"]
        assert build_stable_prompt(model_slug=slug) != build_stable_prompt(model_slug="unregistered-custom-model")


def test_model_switch_changes_the_stable_prompt_without_custom_overlays() -> None:
    builder = PromptBuilderV2()
    state = AgentState(user_message="fix")
    first = builder.build_sections(state=state, model_slug="gpt-5.6-sol", model_instructions="Obsolete custom restrictions")
    second = builder.build_sections(state=state, model_slug="gpt-6-astra")
    assert first[0].content == codex_model_instructions("gpt-5.6-sol")
    assert second[0].content == codex_model_instructions("gpt-6-astra")
    assert not any(section.name == "model_instructions" for section in first)
    assert "Obsolete custom restrictions" not in PromptBuilderV2().build(state=state, model_slug="gpt-5.6-sol", model_instructions="Obsolete custom restrictions").render_system()


def test_current_catalog_uses_literal_templates_without_legacy_personality_substitution() -> None:
    catalog = json.loads((_RESOURCES / "model-instructions.json").read_text(encoding="utf-8"))
    messages = catalog["gpt-5.5"]
    template = messages["instructions_template"]
    for personality in ("friendly", "pragmatic"):
        assert codex_model_instructions("gpt-5.5", personality) == template


def test_explicit_personality_disable_matches_official_section_removal() -> None:
    template = codex_model_instructions("gpt-6.1-sol")
    before, section = template.split("# Personality", 1)
    following_heading = next(line for line in section.splitlines() if line.startswith("# "))
    remainder = section[section.index(following_heading):]
    assert codex_model_instructions("gpt-6.1-sol", "none") == before + remainder


def test_subagent_reporting_stays_outside_the_official_base() -> None:
    state = AgentState(user_message="inspect")
    state.prompt_context = {"subagent": "explore"}
    sections = PromptBuilderV2().build_sections(state=state, model_slug="gpt-5.6-sol")
    assert sections[0].content == codex_model_instructions("gpt-5.6-sol")
    assert any(section.name == "subagent_reporting" and section.layer == "context" for section in sections)


def test_environment_cannot_switch_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_PROMPT_PERSONA", "codex")
    clear_system_prompt_sections()
    first = PromptBuilderV2().build(state=AgentState(user_message="hi")).render_system()

    monkeypatch.setenv("MINICODE_PROMPT_PERSONA", "minicode")
    clear_system_prompt_sections()
    second = PromptBuilderV2().build(state=AgentState(user_message="hi")).render_system()

    assert first == second
    assert "You are a coding agent running in the Codex CLI" in first


def test_llm_settings_drop_legacy_prompt_persona(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"prompt_persona": "codex", "llm": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)

    payload = config.save_llm_settings({"prompt_persona": "minicode"})
    saved = json.loads(settings_file.read_text(encoding="utf-8"))

    assert "prompt_persona" not in saved
    assert "prompt_persona" not in payload


def test_prompt_identity_does_not_copy_host_directives() -> None:
    prompt = build_stable_prompt()

    for forbidden in (
        "::code-comment",
        "::git-push",
        "C:/Users/ago/.codex/skills",
        "<app-context>",
        "<skills_instructions>",
        "Codex desktop context",
    ):
        assert forbidden not in prompt, forbidden
