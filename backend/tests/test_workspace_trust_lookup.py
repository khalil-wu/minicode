from __future__ import annotations

import json
import os
from pathlib import Path

from backend.workspace import trust


def test_current_root_is_resolved_before_unrelated_history_and_ledger_revocation_is_live(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    child = root / "src"
    child.mkdir(parents=True)
    other = tmp_path / "unrelated"
    other.mkdir()
    ledger = tmp_path / "trusted.json"
    ledger.write_text(json.dumps({"roots": [*[str(other / str(i)) for i in range(500)], str(root)]}), encoding="utf8")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    original = Path.resolve
    resolved = []

    def observe(path, *args, **kwargs):
        resolved.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", observe)
    assert trust.is_workspace_trusted(child)
    assert resolved == [child, root]
    ledger.write_text(json.dumps({"roots": [str(other)]}), encoding="utf8")
    assert not trust.is_workspace_trusted(child)
    assert trust.is_workspace_trusted(other)
    ledger.write_text(json.dumps({"roots": []}), encoding="utf8")
    assert not trust.is_workspace_trusted(other)


def test_shared_prefix_does_not_authorize_a_sibling_and_deleted_directory_is_not_trusted(tmp_path, monkeypatch):
    root, sibling = tmp_path / "project", tmp_path / "project-other"
    root.mkdir(); sibling.mkdir()
    ledger = tmp_path / "trusted.json"
    ledger.write_text(json.dumps([str(root)]), encoding="utf8")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    assert trust.is_workspace_trusted(root)
    assert not trust.is_workspace_trusted(sibling)
    root.rmdir()
    assert not trust.is_workspace_trusted(root)
    assert trust.trusted_workspace_roots() == ()


def test_existing_alias_ledger_uses_its_current_target_and_never_trusts_lexical_escape(tmp_path, monkeypatch):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    alias = tmp_path / "alias"

    def link(path, target):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(path))
        else:
            path.symlink_to(target, target_is_directory=True)

    link(alias, allowed)
    ledger = tmp_path / "trusted.json"
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    ledger.write_text(json.dumps([str(alias)]), encoding="utf8")
    assert trust.is_workspace_trusted(allowed)
    alias.rmdir() if os.name == "nt" else alias.unlink()
    link(alias, outside)
    assert not trust.is_workspace_trusted(allowed)
    assert trust.is_workspace_trusted(outside)
    ledger.write_text(json.dumps([str(allowed)]), encoding="utf8")
    escape = allowed / "escape"
    link(escape, outside)
    assert not trust.is_workspace_trusted(escape)


def test_inventory_keeps_order_deduplication_and_existing_invalid_ledger_semantics(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    ledger = tmp_path / "trusted.json"
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    ledger.write_text(json.dumps([None, "", str(first), str(second), str(first), str(tmp_path / "missing")]), encoding="utf8")
    assert trust.trusted_workspace_roots() == (first.resolve(), second.resolve())
    ledger.write_text("invalid json", encoding="utf8")
    assert not trust.is_workspace_trusted(first)
