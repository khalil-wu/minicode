"""update_plan tests for the canonical MiniCode turn-plan notification."""

import asyncio

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.plan_tool import UpdatePlanTool


def _run(plan, explanation=""):
    captured: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        captured.append((event_type, data))

    ctx = ToolExecutionContext(permission=PermissionContext(), session_id="sess1", emit_event=emit)
    args = {"plan": plan}
    if explanation:
        args["explanation"] = explanation
    result = asyncio.run(UpdatePlanTool().execute(args, ctx))
    return result, captured


def test_update_plan_emits_full_snapshot_with_mapped_statuses():
    result, captured = _run(
        [
            {"step": "设计", "status": "completed"},
            {"step": "实现", "status": "in_progress"},
            {"step": "测试", "status": "pending"},
        ],
        explanation="go",
    )

    assert not result.is_error
    assert len(captured) == 1
    event_type, data = captured[0]
    assert event_type == "turn.plan.updated"
    assert data["thread_id"] == ""
    assert data["turn_id"] == ""
    assert data["explanation"] == "go"
    assert data["plan"] == [
        {"step": "设计", "status": "completed"},
        {"step": "实现", "status": "in_progress"},
        {"step": "测试", "status": "pending"},
    ]


def test_update_plan_marks_completed_when_all_done():
    _, captured = _run([{"step": "a", "status": "completed"}, {"step": "b", "status": "completed"}])
    _, data = captured[0]
    assert data["plan"] == [
        {"step": "a", "status": "completed"},
        {"step": "b", "status": "completed"},
    ]


def test_update_plan_marks_not_started_plan_as_draft():
    result, captured = _run([{"step": "a", "status": "pending"}, {"step": "b", "status": "pending"}])
    assert not result.is_error
    _, data = captured[0]
    assert data["plan"] == [
        {"step": "a", "status": "pending"},
        {"step": "b", "status": "pending"},
    ]


def test_update_plan_rejects_plan_mode_lifecycle_fields():
    captured: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        captured.append((event_type, data))

    ctx = ToolExecutionContext(permission=PermissionContext(), session_id="sess1", emit_event=emit)
    result = asyncio.run(
        UpdatePlanTool().execute(
            {
                "status": "accepted",
                "plan": [
                    {"step": "a", "status": "pending"},
                    {"step": "b", "status": "pending"},
                ],
            },
            ctx,
        )
    )
    assert result.is_error
    assert "unsupported fields" in result.content
    assert captured == []


def test_update_plan_preserves_minicode_payload_with_multiple_in_progress():
    result, captured = _run(
        [{"step": "a", "status": "in_progress"}, {"step": "b", "status": "in_progress"}]
    )
    assert not result.is_error
    assert result.content == "Plan updated"
    assert captured[0][1]["plan"] == [
        {"step": "a", "status": "in_progress"},
        {"step": "b", "status": "in_progress"},
    ]


def test_update_plan_accepts_empty_clear_and_rejects_malformed_payloads():
    empty_result, empty_events = _run([])
    assert not empty_result.is_error
    assert empty_events[0][1]["plan"] == []
    assert _run([{"step": "x", "status": "bogus"}])[0].is_error
    assert _run([{"step": "x"}])[0].is_error
    assert _run([{"step": 1, "status": "pending"}])[0].is_error


def test_update_plan_accepts_blank_step_exactly_like_minicode_serde_contract():
    result, captured = _run([{"step": "", "status": "pending"}])

    assert not result.is_error
    assert captured[0][1]["plan"][0]["step"] == ""


def test_update_plan_preserves_explanation_exactly_like_minicode_notification():
    result, captured = _run(
        [{"step": "a", "status": "pending"}],
        explanation="  because the scope changed  ",
    )

    assert not result.is_error
    assert captured[0][1]["explanation"] == "  because the scope changed  "
