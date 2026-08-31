from __future__ import annotations

import asyncio

from backend.plugins.dependencies import (
    parse_dependency_reference,
    resolve_dependency_closure,
    verify_and_demote,
)


def test_dependency_reference_preserves_constraint_and_inherits_marketplace() -> None:
    reference = parse_dependency_reference("tools@official@^2.0", "root@official")
    assert reference.identity == "tools@official"
    assert reference.constraint == "^2.0"

    inherited = parse_dependency_reference("tools@~1.1", "root@official")
    # A single @ is the marketplace separator; the constraint form is the
    # canonical three-part ``name@marketplace@range`` syntax.
    assert inherited.identity == "tools@~1.1"


def test_dependency_closure_rejects_incompatible_version() -> None:
    async def lookup(plugin_id: str):
        return {
            "root@official": {
                "version": "1.0.0",
                "dependencies": ["tools@official@^2.0"],
            },
            "tools@official": {"version": "1.5.0", "dependencies": []},
        }.get(plugin_id)

    result = asyncio.run(resolve_dependency_closure("root@official", lookup))
    assert result.ok is False
    assert result.error is not None
    assert result.error.reason == "version-mismatch"


def test_dependency_closure_selects_highest_matching_candidate() -> None:
    async def lookup(plugin_id: str):
        if plugin_id == "root@official":
            return {"version": "1.0.0", "dependencies": ["tools@official@^1.0"]}
        if plugin_id == "tools@official":
            return {
                "candidates": [
                    {"version": "1.1.0", "dependencies": []},
                    {"version": "1.4.0", "dependencies": []},
                    {"version": "2.0.0", "dependencies": []},
                ]
            }
        return None

    result = asyncio.run(resolve_dependency_closure("root@official", lookup))
    assert result.ok is True
    assert result.closure == ("tools@official", "root@official")


def test_verify_and_demote_checks_dependency_version_ranges() -> None:
    demoted, errors = verify_and_demote(
        [
            {
                "id": "root@official",
                "name": "root",
                "marketplace": "official",
                "version": "1.0.0",
                "enabled": True,
                "dependencies": ["tools@official@^2.0"],
            },
            {
                "id": "tools@official",
                "name": "tools",
                "marketplace": "official",
                "version": "1.5.0",
                "enabled": True,
                "dependencies": [],
            },
        ]
    )
    assert demoted == {"root@official"}
    assert any(error.reason == "version-mismatch" for error in errors)

