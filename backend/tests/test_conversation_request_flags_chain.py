from __future__ import annotations

import pytest

from backend.services.conversation_payload_service import parse_conversation_clear_request, parse_conversation_create_request, parse_conversation_delete_request


@pytest.mark.parametrize("field", ["cleanup_worktree", "force", "client_resource_cleanup"])
@pytest.mark.parametrize("value", ["false", 1, [], None])
def test_delete_flags_reject_non_booleans_at_the_request_parser(field, value):
    with pytest.raises(ValueError, match=field):
        parse_conversation_delete_request({"conversation_id": "fixture", field: value})


@pytest.mark.parametrize("field", ["git_isolated", "side_chat"])
@pytest.mark.parametrize("value", ["false", 1, [], None])
def test_create_flags_do_not_turn_a_false_string_into_isolation_or_side_chat(field, value):
    with pytest.raises(ValueError, match=field):
        parse_conversation_create_request({"workspace_root": "C:/fixture", field: value})


def test_clear_preserve_plan_uses_the_same_request_flag_boundary():
    with pytest.raises(ValueError, match="preserve_plan"):
        parse_conversation_clear_request({"preserve_plan": "false"}, active_conversation_id="fixture")
    assert not parse_conversation_clear_request({}, active_conversation_id="fixture").preserve_plan


@pytest.mark.parametrize("value", [True, False])
def test_real_boolean_flags_keep_their_existing_meaning(value):
    deleted = parse_conversation_delete_request({"conversation_id": "fixture", "cleanup_worktree": value, "force": value, "client_resource_cleanup": value})
    assert deleted.cleanup_worktree is value
    assert deleted.force_cleanup is value
    assert deleted.client_resource_cleanup is value
    created = parse_conversation_create_request({"workspace_root": "C:/fixture", "git_isolated": value, "side_chat": value})
    assert created.git_isolated is value
    assert (created.conversation_type == "side_chat") is value


def test_absent_flags_keep_default_unisolated_conversation_and_no_worktree_cleanup():
    deleted = parse_conversation_delete_request({"conversation_id": "fixture"})
    assert not deleted.cleanup_worktree and not deleted.force_cleanup and not deleted.client_resource_cleanup
    assert not parse_conversation_create_request({}).git_isolated
