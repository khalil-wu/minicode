"""Composite conversation/workspace ownership for persisted runtime data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, order=True)
class OwnerScope:
    conversation_id: str = ""
    workspace_root: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "workspace_root": self.workspace_root,
        }


def canonical_workspace_root(value: str | Path | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        resolved = str(Path(text).resolve(strict=False))
    except OSError:
        try:
            resolved = os.path.abspath(text)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("invalid workspace_root") from exc
    except (RuntimeError, ValueError) as exc:
        raise ValueError("invalid workspace_root") from exc
    return os.path.normcase(resolved)


def make_owner_scope(
    conversation_id: str | None,
    workspace_root: str | Path | None,
) -> OwnerScope:
    return OwnerScope(
        conversation_id=str(conversation_id or "").strip(),
        workspace_root=canonical_workspace_root(workspace_root),
    )


def normalize_owner_scopes(
    raw_scopes: Any,
    *,
    conversation_id: str = "",
    conversation_ids: Iterable[str] = (),
    workspace_root: str | Path | None = None,
    strict: bool = False,
) -> tuple[OwnerScope, ...]:
    """Parse v1 composite grants, falling back to the legacy owner fields."""

    scopes: list[OwnerScope] = []
    if strict and not isinstance(raw_scopes, list):
        raise ValueError("owner_scopes must be a list")
    if isinstance(raw_scopes, list):
        for raw in raw_scopes:
            if not isinstance(raw, Mapping):
                if strict:
                    raise ValueError("owner scope entries must be objects")
                continue
            if strict and (
                not isinstance(raw.get("conversation_id", ""), str)
                or not isinstance(raw.get("workspace_root", ""), str)
            ):
                raise ValueError("owner scope fields must be strings")
            scope = make_owner_scope(
                raw.get("conversation_id") or "",
                raw.get("workspace_root"),
            )
            if scope.conversation_id or scope.workspace_root:
                scopes.append(scope)
            elif strict:
                raise ValueError("owner scope entries may not be empty")

    if not scopes and not strict:
        owners = [str(value or "").strip() for value in conversation_ids]
        if not any(owners):
            owners = [str(conversation_id or "").strip()]
        owners = list(dict.fromkeys(owner for owner in owners if owner))
        canonical_workspace = canonical_workspace_root(workspace_root)
        if owners:
            scopes.extend(
                OwnerScope(conversation_id=owner, workspace_root=canonical_workspace)
                for owner in owners
            )
        elif canonical_workspace:
            scopes.append(OwnerScope(workspace_root=canonical_workspace))

    return tuple(dict.fromkeys(scopes))


def owner_scope_matches(
    scopes: Iterable[OwnerScope],
    conversation_id: str | None,
    workspace_root: str | Path | None,
) -> bool:
    """Require one exact composite grant; never cross-product separate owners."""

    try:
        requested = make_owner_scope(conversation_id, workspace_root)
    except (TypeError, ValueError):
        return False
    normalized = tuple(scopes)
    if not normalized:
        # Ownerless legacy records may remain usable by explicitly unscoped
        # maintenance/test callers, but a real conversation/workspace request
        # must never inherit them as public data.
        return not requested.conversation_id and not requested.workspace_root
    for scope in normalized:
        # A conversation is mandatory; workspace_root may be the exact empty
        # value for a projectless conversation. Always compare both fields so
        # a projectless grant cannot be reused after that conversation is bound
        # to a checkout.
        if not scope.conversation_id:
            continue
        if (
            requested.conversation_id == scope.conversation_id
            and requested.workspace_root == scope.workspace_root
        ):
            return True
    return False


def grant_owner_scope(
    scopes: Iterable[OwnerScope],
    *,
    source_conversation_id: str,
    target_conversation_id: str,
    target_workspace_root: str | Path | None = None,
) -> tuple[OwnerScope, ...]:
    """Grant a clone/fork/merge the source scope's cwd unless one is explicit."""

    current = tuple(scopes)
    source = str(source_conversation_id or "").strip()
    target = str(target_conversation_id or "").strip()
    if not source or not target or source == target:
        return current
    source_scopes = [scope for scope in current if scope.conversation_id == source]
    if not source_scopes:
        return current
    explicit_workspace = canonical_workspace_root(target_workspace_root)
    additions = [
        OwnerScope(
            conversation_id=target,
            workspace_root=explicit_workspace or scope.workspace_root,
        )
        for scope in source_scopes
    ]
    return tuple(dict.fromkeys((*current, *additions)))


def remove_conversation_scopes(
    scopes: Iterable[OwnerScope],
    conversation_id: str,
) -> tuple[OwnerScope, ...]:
    owner = str(conversation_id or "").strip()
    return tuple(scope for scope in scopes if scope.conversation_id != owner)
