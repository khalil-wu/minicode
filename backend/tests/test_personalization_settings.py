from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from backend.api import routes_llm
from backend.api.models import PersonalizationUpdateRequest


def test_personalization_settings_round_trip_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # MiniCode owns its own instruction file: the route now stores user
    # instructions in ``<minicode config home>/INSTRUCTIONS.md`` behind
    # ``USER_INSTRUCTIONS_FILE``/``INSTRUCTIONS_MAX_BYTES``. The old
    # ``.codex/AGENTS.md`` + ``USER_AGENTS_FILE``/``AGENTS_MD_MAX_BYTES``
    # names were a foreign-harness leftover and no longer exist.
    instructions_file = tmp_path / ".minicode" / "INSTRUCTIONS.md"
    clear_cache = []
    monkeypatch.setattr(routes_llm, "USER_INSTRUCTIONS_FILE", instructions_file)
    monkeypatch.setattr(routes_llm, "clear_guideline_cache", lambda: clear_cache.append(True))

    initial = routes_llm.get_personalization_settings_api(Response())
    assert initial == {
        "instructions": "",
        "path": str(instructions_file),
        "exists": False,
        "max_bytes": routes_llm.INSTRUCTIONS_MAX_BYTES,
    }

    saved = routes_llm.update_personalization_settings_api(
        PersonalizationUpdateRequest(instructions="先复用现有结构\r\n再统一测试"),
        Response(),
    )
    assert instructions_file.read_text(encoding="utf-8") == "先复用现有结构\n再统一测试"
    assert saved["instructions"] == "先复用现有结构\n再统一测试"
    assert saved["exists"] is True

    cleared = routes_llm.update_personalization_settings_api(
        PersonalizationUpdateRequest(instructions="  \n"),
        Response(),
    )
    assert cleared["instructions"] == ""
    assert cleared["exists"] is False
    assert not instructions_file.exists()
    assert clear_cache == [True, True]


def test_personalization_settings_reject_oversized_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes_llm, "USER_INSTRUCTIONS_FILE", tmp_path / "INSTRUCTIONS.md")
    monkeypatch.setattr(routes_llm, "INSTRUCTIONS_MAX_BYTES", 8)

    with pytest.raises(HTTPException) as exc_info:
        routes_llm.update_personalization_settings_api(
            PersonalizationUpdateRequest(instructions="九个字节以上"),
            Response(),
        )

    assert exc_info.value.status_code == 422
    assert not routes_llm.USER_INSTRUCTIONS_FILE.exists()
