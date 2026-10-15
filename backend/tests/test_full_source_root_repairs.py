import json
from pathlib import Path

from backend.services.conversation_goal_service import prepare_goal_action
from backend.workspace.service import WorkspaceService
from backend.workspace.text_search import WorkspaceTextSearchRequest, search_workspace_text


def test_goal_refusal_preserves_full_current_goal_and_boundary_accepts():
    current = {"id": "goal-real", "text": "existing target", "status": "paused"}
    too_long = prepare_goal_action({"text": "中" * 4001}, conversation_id="audit", current_goal=current, source="frontend.ui")
    assert too_long.should_update is False
    assert too_long.next_goal is current
    assert too_long.outcome.level == "error"
    assert too_long.outcome.data["goal"] == current
    accepted = prepare_goal_action({"text": "中" * 4000}, conversation_id="audit", current_goal=current, source="frontend.ui")
    assert accepted.should_update
    assert accepted.next_goal["text"] == "中" * 4000


def _utf16_slice(content: str, span: dict[str, int]) -> str:
    encoded = content.encode("utf-16-le")
    return encoded[span["offset"] * 2:(span["offset"] + span["length"]) * 2].decode("utf-16-le")


def test_search_unicode_crlf_capture_identity_and_linear_wire_growth(tmp_path: Path):
    content = "前🙂foo\r\n" + "x" * 300_000
    (tmp_path / "中文 空格.txt").write_bytes(content.encode("utf-8"))
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)
    result = search_workspace_text(service, WorkspaceTextSearchRequest(query=r"(?P<emoji>🙂)(foo)|(x)", regex=True, limit=2000))
    file = result["files"][0]
    first = file["matches"][0]
    assert _utf16_slice(file["content"], first) == "🙂foo"
    assert _utf16_slice(file["content"], first["groups"][0]) == "🙂"
    assert first["groups"][2] is None
    assert first["named_groups"]["emoji"] == first["groups"][0]
    assert file["matches"][1]["line"] == 2
    assert file["matches"][1]["column"] == 1
    assert result["truncated"] and result["match_count"] == 2000
    assert max(len(match["snippet"]) for match in file["matches"]) <= 322
    assert all("text" not in match for match in file["matches"])
    # One source plus bounded snippets/spans, including thousands of same-line
    # matches. This catches duplicated line/group strings without a noisy timer.
    assert len(json.dumps(result, ensure_ascii=False)) < 2_000_000
