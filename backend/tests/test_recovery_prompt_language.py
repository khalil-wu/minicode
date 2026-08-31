"""Internal recovery prompts must be stable English, not the UI's language.

These strings are sent to the model, not shown to the user. Hardcoding Chinese
pushes an English conversation toward Chinese replies, and it varies the prompt
prefix in a way that costs provider cache hits. UI-facing copy (``user_summary``,
progress labels) is deliberately excluded — that is where localisation belongs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
MODEL_PROMPT_SOURCES = (
    AGENT_DIR / "loop.py",
    AGENT_DIR / "answer_recovery.py",
    AGENT_DIR / "answer_acceptance.py",
    AGENT_DIR / "final_answer_orchestrator.py",
)
_CJK = re.compile(r"[一-鿿]")

# Calls that place text directly into the model's message history.
_MODEL_VISIBLE_CALLS = ("ctx.append_user(", "append_user_context(")


def _model_visible_prompt_lines() -> list[tuple[Path, int, str]]:
    """Lines belonging to model-visible prompt calls, with their locations."""
    collected: list[tuple[Path, int, str]] = []
    for path in MODEL_PROMPT_SOURCES:
        source = path.read_text(encoding="utf-8").splitlines()
        depth = 0
        for number, line in enumerate(source, 1):
            if depth == 0 and any(call in line for call in _MODEL_VISIBLE_CALLS):
                depth = line.count("(") - line.count(")")
                collected.append((path, number, line))
                continue
            if depth > 0:
                collected.append((path, number, line))
                depth += line.count("(") - line.count(")")
    return collected


def test_model_visible_prompts_are_english() -> None:
    offenders = [
        (path, number, line.strip())
        for path, number, line in _model_visible_prompt_lines()
        if _CJK.search(line)
    ]

    assert not offenders, (
        "model-visible recovery prompts must be stable English; found CJK at: "
        + "; ".join(
            f"{path.name}:{number} {text}" for path, number, text in offenders
        )
    )
