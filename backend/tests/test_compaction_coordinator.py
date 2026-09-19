from backend.ws.compaction_coordinator import rebase_turn_admissions_after_compaction
import pytest


@pytest.mark.parametrize("annotated", [False, True])
def test_compaction_rebases_only_boundaries_in_retained_suffix(annotated) -> None:
    before = {
        "history": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": "keep answer"},
        ],
        "turn_admissions": {
            "old": {"history_start": 0, "history_end": 1, "run_id": "r1"},
            "keep": {"history_start": 2, "history_end": 3, "run_id": "r2"},
        },
    }
    after = {
        "history": [
            {"role": "user", "content": "summary"},
            {"role": "user", "content": "keep", **({"is_user_input": True} if annotated else {})},
            {"role": "assistant", "content": "keep answer"},
        ]
    }

    rebase_turn_admissions_after_compaction(before, after)

    assert after["turn_admissions"] == {
        "keep": {"history_start": 1, "history_end": 2, "run_id": "r2"}
    }
