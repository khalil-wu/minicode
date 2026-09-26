"""``canonical_path_mapping_key`` migrates a legacy alias without re-resolving.

The read-hash map is consulted on the event loop for every new-path read, so a
miss must not cost one filesystem ``resolve()`` per stored entry.
"""

from __future__ import annotations

from pathlib import Path

import backend.atomic_io as atomic_io
from backend.atomic_io import canonical_file_path_key, canonical_path_mapping_key


def test_legacy_case_alias_is_migrated_to_the_canonical_key(tmp_path: Path) -> None:
    target = tmp_path / "Pkg" / "Mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    canonical = canonical_file_path_key(target)
    # The legacy spelling is str(resolve()) before normcase/normpath.
    legacy = str(target.resolve())
    mapping = {legacy: "hash-1"}

    resolved = canonical_path_mapping_key(mapping, target)

    assert resolved == canonical
    assert mapping == {canonical: "hash-1"}


def test_miss_does_not_resolve_every_stored_key(tmp_path: Path, monkeypatch) -> None:
    mapping = {f"/legacy/path/{index}.py": f"hash-{index}" for index in range(400)}
    calls = {"count": 0}
    original_resolve = Path.resolve

    def counting_resolve(self, *args, **kwargs):
        calls["count"] += 1
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    canonical_path_mapping_key(mapping, tmp_path / "fresh.py")

    # Only the queried path is resolved; stored keys are compared as strings.
    assert calls["count"] == 1


def test_distinct_paths_do_not_collide(tmp_path: Path) -> None:
    other = canonical_file_path_key(tmp_path / "a.py")
    mapping = {other: "hash-a"}
    resolved = canonical_path_mapping_key(mapping, tmp_path / "b.py")
    assert resolved == canonical_file_path_key(tmp_path / "b.py")
    assert mapping == {other: "hash-a"}
