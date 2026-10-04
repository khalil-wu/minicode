from __future__ import annotations

import pytest

from backend.ui.preferences import UIPreferencesStore


@pytest.mark.parametrize("updates", [{"to_dict": "not callable"}, {"compact_mode": "false"}, {"sidebar_width": True}, {"sidebar_width": -1}, {"message_font_size": "invalid"}])
def test_preference_update_rejects_invalid_request_fields_without_changing_disk(tmp_path, updates):
    store = UIPreferencesStore(tmp_path)
    store.update("owner", {"sidebar_width": 300})
    original = (tmp_path / "ui_prefs_owner.json").read_bytes()
    with pytest.raises(ValueError):
        store.update("owner", updates)
    assert (tmp_path / "ui_prefs_owner.json").read_bytes() == original


def test_partial_preference_updates_preserve_the_existing_fields_and_cross_store_visibility(tmp_path):
    first = UIPreferencesStore(tmp_path)
    first.update("owner", {"sidebar_width": 300})
    second = UIPreferencesStore(tmp_path)
    second.update("owner", {"compact_mode": True})
    assert first.get("owner").sidebar_width == 300
    assert first.get("owner").compact_mode is True


def test_invalid_preferences_file_is_reported_without_overwriting_it(tmp_path):
    path = tmp_path / "ui_prefs_owner.json"
    path.write_text('{"compact_mode": "false"}', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        UIPreferencesStore(tmp_path).update("owner", {"sidebar_width": 300})
    assert path.read_bytes() == original


def test_invalid_preference_owner_is_rejected_instead_of_reporting_a_successful_save(tmp_path):
    store = UIPreferencesStore(tmp_path)
    with pytest.raises(ValueError):
        store.update("../owner", {"sidebar_width": 300})
    assert not list(tmp_path.iterdir())
