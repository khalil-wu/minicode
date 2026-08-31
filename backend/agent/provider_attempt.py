"""State for one provider stream attempt within a logical turn."""

from __future__ import annotations

from dataclasses import dataclass


def provider_progress_id(iteration_id: str, owner_id: str = "") -> str:
    """Return the stable provider-progress identity for one logical turn.

    ``iteration_id`` is only unique inside one turn.  The optional owner is
    the run/turn identity and is supplied by the live runtime, so reconnecting
    or starting a later turn cannot overwrite an earlier turn's provider row.
    The empty-owner form remains valid for narrow legacy callers and fixtures.
    """

    clean_iteration = str(iteration_id or "").strip()
    clean_owner = str(owner_id or "").strip()
    if clean_owner:
        return f"provider:connection:{clean_owner}:{clean_iteration}"
    return f"provider:connection:{clean_iteration}"


@dataclass(slots=True)
class ProviderAttempt:
    iteration_id: str
    retry_index: int
    span_id: str
    started_at: int
    # One row represents the whole logical request/retry ladder.  It is
    # assigned by the turn owner, not regenerated for each HTTP attempt.
    progress_id: str = ""
    max_retries: int = 0
    first_byte_at: int | None = None
    first_event_reported: bool = False
    closed: bool = False

    @property
    def attempt_number(self) -> int:
        return self.retry_index + 1

    @property
    def retry_attempt(self) -> int:
        """User-visible retry ordinal (the initial request is zero)."""

        return self.retry_index
