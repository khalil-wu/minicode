"""Pure reduction helpers for provider-managed activity lifecycles."""

from __future__ import annotations

from backend.llm.base import ProviderActivityEvent


_PROVIDER_ACTIVITY_STATUS_RANK = {
    "": 0,
    "info": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}

_PROVIDER_PROGRESS_STATUS_RANK = {
    "": 0,
    "info": 0,
    "running": 1,
    "partial": 2,
    "completed": 3,
    "failed": 4,
}
_PROVIDER_PROGRESS_TERMINAL_STATUSES = frozenset(
    {"partial", "completed", "failed"}
)

_PROVIDER_PROGRESS_STATE_RANK = {
    "": 0,
    "connecting": 1,
    "reconnecting": 2,
    "responding": 3,
    "completed": 4,
    "failed": 4,
    "interrupted": 4,
}


def provider_activity_status_rank(status: object) -> int:
    return _PROVIDER_ACTIVITY_STATUS_RANK.get(
        str(status or "").strip().lower(),
        0,
    )


def provider_progress_status_rank(status: object) -> int:
    """Rank the public provider-request lifecycle monotonically."""

    return _PROVIDER_PROGRESS_STATUS_RANK.get(
        str(status or "").strip().lower(),
        0,
    )


def provider_progress_is_terminal(status: object) -> bool:
    return str(status or "").strip().lower() in _PROVIDER_PROGRESS_TERMINAL_STATUSES


def provider_progress_state_rank(state: object) -> int:
    """Rank a typed provider lifecycle within one retry attempt."""

    return _PROVIDER_PROGRESS_STATE_RANK.get(
        str(state or "").strip().lower(),
        0,
    )


def provider_progress_lifecycle_regressed(
    *,
    previous_status: object,
    incoming_status: object,
    previous_retry_attempt: int | None = None,
    incoming_retry_attempt: int | None = None,
    previous_provider_state: object = None,
    incoming_provider_state: object = None,
) -> bool:
    """Return whether a delayed frame would move a provider row backwards."""

    previous_rank = provider_progress_status_rank(previous_status)
    incoming_rank = provider_progress_status_rank(incoming_status)
    attempt_regressed = (
        previous_retry_attempt is not None
        and incoming_retry_attempt is not None
        and incoming_retry_attempt < previous_retry_attempt
    )
    state_regressed = (
        provider_progress_state_rank(previous_provider_state)
        > provider_progress_state_rank(incoming_provider_state)
        and (
            previous_retry_attempt is None
            or incoming_retry_attempt is None
            or previous_retry_attempt == incoming_retry_attempt
        )
    )
    return (
        previous_rank > incoming_rank
        or (
            provider_progress_is_terminal(previous_status)
            and not provider_progress_is_terminal(incoming_status)
        )
        or (previous_rank == incoming_rank and attempt_regressed)
        or state_regressed
    )


def merge_provider_activity_detail(*details: object) -> str:
    """Merge already-sanitized detail fragments without duplicating fields.

    Provider adapters deliberately expose summaries such as ``Server: ...`` or
    ``Arguments: N characters`` instead of raw code/input bodies. Lifecycle
    frames often repeat only a subset of those fields, so replacing the prior
    detail would make the terminal row less informative than the running row.
    """

    parts: list[str] = []
    seen: set[str] = set()
    for detail in details:
        for raw_part in str(detail or "").split(" · "):
            part = raw_part.strip()
            if not part or part in seen:
                continue
            seen.add(part)
            parts.append(part)
    return " · ".join(parts)


def reduce_provider_activity(
    previous: ProviderActivityEvent | None,
    incoming: ProviderActivityEvent,
) -> ProviderActivityEvent:
    """Return one cumulative, monotonic snapshot for a stable activity id."""

    if previous is None:
        return incoming

    previous_rank = provider_activity_status_rank(previous.status)
    incoming_rank = provider_activity_status_rank(incoming.status)
    keep_previous_lifecycle = previous_rank > incoming_rank

    return ProviderActivityEvent(
        id=incoming.id or previous.id,
        kind=incoming.kind or previous.kind,
        name=incoming.name or previous.name,
        status=(
            previous.status
            if keep_previous_lifecycle
            else incoming.status or previous.status
        ),
        message=(
            previous.message
            if keep_previous_lifecycle
            else incoming.message or previous.message
        ),
        detail=merge_provider_activity_detail(previous.detail, incoming.detail),
        count=incoming.count if incoming.count is not None else previous.count,
    )
