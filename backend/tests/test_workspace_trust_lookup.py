from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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


def linked_workspace(tmp_path, monkeypatch):
    repo, checkout = tmp_path / "repo", tmp_path / "pool" / "linked"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, stdin=subprocess.DEVNULL,
            check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    git("init", "-b", "main")
    git("-c", "user.name=Trust fixture", "-c", "user.email=trust@example.invalid", "commit", "--allow-empty", "-m", "base")
    git("worktree", "add", "-b", "linked", str(checkout))
    ledger = tmp_path / "trusted.json"
    ledger.write_text(json.dumps([str(repo)]), encoding="utf-8")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", ledger)
    gitdir = Path((checkout / ".git").read_text().strip()[7:].strip())
    return repo, checkout, gitdir, ledger


def test_registered_worktree_inherits_current_primary_trust_without_adding_a_ledger_entry(tmp_path, monkeypatch):
    repo, checkout, gitdir, ledger = linked_workspace(tmp_path, monkeypatch)
    nested = checkout / "src"
    nested.mkdir()
    assert trust.is_workspace_trusted(checkout)
    assert trust.is_workspace_trusted(nested)
    assert json.loads(ledger.read_text()) == [str(repo)]
    ledger.write_text("[]")
    assert not trust.is_workspace_trusted(checkout)
    ledger.write_text(json.dumps([str(repo)]))
    assert trust.is_workspace_trusted(checkout)
    (gitdir / "gitdir").write_text(str(tmp_path / "other" / ".git"))
    assert not trust.is_workspace_trusted(checkout)


@pytest.mark.parametrize("corruption", ["forged-checkout", "wrong-common", "oversize", "link", "linked-directory-alias"])
def test_worktree_trust_requires_original_regular_metadata_and_matching_backlinks(tmp_path, monkeypatch, corruption):
    repo, checkout, gitdir, _ = linked_workspace(tmp_path, monkeypatch)
    if corruption == "forged-checkout":
        forged = tmp_path / "forged"
        forged.mkdir()
        (forged / ".git").write_bytes((checkout / ".git").read_bytes())
        assert not trust.is_workspace_trusted(forged)
        assert trust.is_workspace_trusted(checkout)
        return
    if corruption == "wrong-common":
        other = tmp_path / "other"
        other.mkdir()
        (gitdir / "commondir").write_text(str(other))
    elif corruption == "oversize":
        (checkout / ".git").unlink()
        (checkout / ".git").write_text("gitdir: " + str(gitdir) + " " * (64 * 1024))
    elif corruption == "link":
        pointer = checkout / ".git"
        original = checkout / "actual-pointer"
        pointer.rename(original)
        try:
            pointer.symlink_to(original)
        except OSError:
            pytest.skip("Account cannot create a file symlink")
    else:
        alias = tmp_path / "directory-alias"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(gitdir), str(alias))
        else:
            alias.symlink_to(gitdir, target_is_directory=True)
        (checkout / ".git").unlink()
        (checkout / ".git").write_text("gitdir: " + str(alias))
    assert not trust.is_workspace_trusted(checkout)
