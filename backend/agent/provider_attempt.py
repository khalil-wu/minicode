"""State for one provider stream attempt within a logical turn."""

from __future__ import annotations

from dataclasses import dataclass


def provider_progress_id(iteration_id: str) -> str:
    """Return the single provider-progress identity for one loop iteration."""

    return f"provider:connection:{iteration_id}"


@dataclass(slots=True)
class ProviderAttempt:
    iteration_id: str
    retry_index: int
    span_id: str
    started_at: int
    first_byte_at: int | None = None
    first_event_reported: bool = False
    closed: bool = False

    @property
    def attempt_number(self) -> int:
        return self.retry_index + 1
