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


def provider_activity_status_rank(status: object) -> int:
    return _PROVIDER_ACTIVITY_STATUS_RANK.get(
        str(status or "").strip().lower(),
        0,
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
