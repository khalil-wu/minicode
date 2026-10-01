from backend.api.models import ChatResponse


def test_rest_response_preserves_actual_pending_cleanup_evidence() -> None:
    receipt = {
        "resource_kind": "lifecycle", "resource_id": "observer:1",
        "reason": "cancelled", "requested": True, "acknowledged": False,
        "completed": False, "timed_out": True, "pending": 1,
    }
    response = ChatResponse(
        reply="", stopped_reason="cancelled", status="cancelled", iterations=1,
        lifecycle_cleanup_receipts={"observer:1": receipt},
        lifecycle_cleanup_pending_count=1,
    ).model_dump()
    assert response["lifecycle_cleanup_pending_count"] == 1
    assert response["lifecycle_cleanup_receipts"]["observer:1"] == receipt


def test_rest_response_without_started_borrowers_has_empty_cleanup() -> None:
    response = ChatResponse(reply="", stopped_reason="startup_failed", iterations=0).model_dump()
    assert response["lifecycle_cleanup_pending_count"] == 0
    assert response["lifecycle_cleanup_receipts"] == {}
