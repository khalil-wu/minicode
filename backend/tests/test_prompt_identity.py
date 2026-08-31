"""Contracts for MiniCode's single runtime identity."""

import json

import backend.config as config
from backend import config_helpers
from backend.agent.prompting import (
    PromptBuilderV2,
    build_stable_prompt,
    clear_system_prompt_sections,
)
from backend.agent.state import AgentState


def test_stable_prompt_has_one_minicode_identity() -> None:
    prompt = build_stable_prompt()

    assert "You are an agent for MiniCode" in prompt
    assert "Codex persona" not in prompt
    assert "You are Codex" not in prompt
    assert "Complete requested tasks fully" in prompt
    # The prompt points at MiniCode's own instruction file. It deliberately does
    # not name AGENTS.md: that file is still read for compatibility, but it is
    # ranked below `.minicode/INSTRUCTIONS.md` and is not what MiniCode writes.
    assert ".minicode/INSTRUCTIONS.md" in prompt
    assert "AGENTS.md" not in prompt
    assert "For casual conversation" in prompt
    assert "without inspecting the workspace or" in prompt
    assert "they do not by themselves imply a task" in prompt
    assert "Do not infer or continue an earlier task" in prompt
    assert "# User updates" in prompt
    assert "Before the first tool call" in prompt
    assert "Before each new tool phase" in prompt
    assert "After a meaningful result" in prompt
    assert "Chat Completions has no separate commentary channel" in prompt
    assert "Never emit a placeholder" in prompt
    assert "# Tone and style" in prompt
    assert "Only use emojis if the user explicitly requests them" in prompt
    assert "file_path:line_number" in prompt
    assert "Do not put a colon before a tool call" in prompt
    assert "run_command(run_in_background=true)" in prompt
    assert 'monitor(action="cancel", command_id=...)' in prompt
    assert "never clean up by process name" in prompt


def test_stable_prompt_keeps_public_claims_within_observed_evidence() -> None:
    prompt = build_stable_prompt()
    flattened = " ".join(prompt.split())

    assert "A search result, a search snippet, or no result is not" in flattened
    assert "Only describe a source as verified when this run actually opened or fetched" in flattened
    assert "Do not turn an absence of public reporting" in flattened
    assert "Do not characterize that unseen material as" in flattened


def test_subagent_prompt_does_not_ask_worker_to_update_the_user() -> None:
    prompt = build_stable_prompt(subagent=True)

    assert "# User updates" not in prompt
    assert "The caller" in prompt


def test_environment_cannot_switch_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_PROMPT_PERSONA", "codex")
    clear_system_prompt_sections()
    first = PromptBuilderV2().build(state=AgentState(user_message="hi")).render_system()

    monkeypatch.setenv("MINICODE_PROMPT_PERSONA", "minicode")
    clear_system_prompt_sections()
    second = PromptBuilderV2().build(state=AgentState(user_message="hi")).render_system()

    assert first == second
    assert "You are an agent for MiniCode" in first


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
