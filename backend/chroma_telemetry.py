"""Chroma telemetry adapters used by local-only MiniCode storage."""

from __future__ import annotations

from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override


class NoopProductTelemetryClient(ProductTelemetryClient):
    """Disable Chroma product telemetry without depending on PostHog behavior."""

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None
