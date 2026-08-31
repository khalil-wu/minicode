from __future__ import annotations

import json

import pytest

import backend.config as config_module
from backend.agent.checkpoint import MAX_CHECKPOINT_BYTES, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.tool_execution import _execution_exception_result
from backend.config import add_permission_content_rule
from backend.llm.json_repair import repair_tool_json


def test_json_repair_never_trims_provider_bytes() -> None:
    raw = '{"command":"rm -rf /tmp/x --dry-run" trailing'

    assert repair_tool_json(raw) is None


@pytest.mark.parametrize(
    "rule",
    [
        "run_command(bash:*)",
        "run_command(C:\\Windows\\System32\\cmd.exe:*)",
        "run_command(/usr/bin/env:*)",
    ],
)
def test_backend_rejects_wrapper_permission_prefixes(monkeypatch, tmp_path, rule: str) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_module, "SETTINGS_FILE", settings_path)

    with pytest.raises(ValueError, match="wrapper"):
        add_permission_content_rule(rule)


def test_context_snapshot_bounds_work_before_checkpoint_serialization() -> None:
    context = ContextBuilder()
    for index in range(20):
        context.append_user(f"message-{index}:" + ("x" * 100))

    snapshot = context.export_snapshot(max_messages=5, max_chars=120)

    # The bound applies to the complete serialized message group, not only its
    # content string. At 120 chars even one schema-complete message cannot fit.
    assert snapshot["history"] == []
    assert all(not item["content"].startswith("message-15:") for item in snapshot["history"])


def test_checkpoint_is_compact_and_globally_bounded(tmp_path) -> None:
    huge = "x" * 100_000
    path = save_checkpoint(
        session_id="bounded",
        user_message=huge,
        iterations=1,
        reply=huge,
        messages=[{"role": "tool", "content": huge, "nested": [huge] * 400}] * 200,
        tool_calls=[],
        active_skills=[huge] * 400,
        disabled_tools={huge},
        stopped_reason="interrupted",
        last_mutation_index=0,
        base_dir=tmp_path,
    )

    raw = path.read_bytes()
    assert len(raw) <= MAX_CHECKPOINT_BYTES
    assert b"\n  " not in raw
    assert json.loads(raw)["checksum"]


def test_exception_message_is_model_visible() -> None:
    """cc toolExecution surfaces the exception message to the model (it needs
    it to self-correct); the full chain stays in developer_detail."""
    exc = OSError("disk quota exceeded")
    result = _execution_exception_result(exc)

    assert "disk quota exceeded" in result.content
    assert result.is_error is True
